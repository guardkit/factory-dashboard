"""Projector-side stalled-dwell monitor — closes the S2 coach finding (ux §10.6 case 3).

The F-5 MANUFACTURE path (`readmodel.freshness.should_manufacture_stalled`) existed only as an
isolated unit-tested predicate; nothing invoked it, so a dwell breach with a FRESH watermark never
manufactured the `stalled` issue end-to-end. This module wires it in a periodic evaluation running
in the SAME cadence class as the 10 s heartbeat loop (ux §5.6 signal 1). The projector is the SOLE
writer of `readmodel.db` (design §7 / M-D4), so the register write lives here, never on a web path.

Each pass walks the in-flight builds, computes each build's stage dwell against the norms table
(`stage_dwell` red threshold, design §1 Q2 — `>120m`), and applies the §5.6 F-5 three-signal
layering THROUGH the existing predicate (never a fork of its logic):

- projector heartbeat stale ⇒ the whole projection is suspect ⇒ NO stalled verdict (signal 1);
- feeding-stream (PIPELINE) watermark stale ⇒ quiet-or-wedged is indistinguishable ⇒ NO stalled
  verdict, the panel reports "projection lagging" instead (signal 2 / F-5 verbatim);
- dwell breached WHILE the watermark stayed fresh ⇒ the silence is real ⇒ manufacture `stalled`.

Issues are written idempotently: opened once per breach (`opened_at` stays stable across
re-evaluations — no duplicate rows), and closed when the run's events resume (dwell falls back
below the threshold) or the build leaves the in-flight set. A stale/suspect projection NEVER closes
a standing stalled issue (we cannot tell — leave the last honest verdict in place).

No JetStream API usage, no publish, no new tables — a periodic read + register upsert on the
projector's own rw connection (fences 1/2/4).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from backend.projector.projections.base import iso, parse_dt, record_change
from backend.readmodel import freshness as fr
from backend.readmodel import viewmodels as vm

# Same cadence class as the heartbeat loop (10 s) — a dwell breach surfaces within one tick of the
# projector's liveness heartbeat, never faster than the events it reasons about.
DWELL_EVAL_SECS = 10

# In-flight = a build a dwell verdict can be at stake for (design §1 / P2 board query).
_IN_FLIGHT = ("QUEUED", "RUNNING", "PAUSED")

# The register panel a stalled open/close nudges over SSE (the in-flight board the run lives on).
_PANEL = "p2"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class DwellEval:
    """What one evaluation pass changed (opened/closed stalled issue ids + touched feature scopes)."""

    opened: set[str] = field(default_factory=set)
    closed: set[str] = field(default_factory=set)
    scope_keys: set[str] = field(default_factory=set)


def _heartbeat_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT checked_at FROM service_health WHERE service='projector'").fetchone()
    return parse_dt(row[0]) if row and row[0] else None


def _pipeline_watermark(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute(
        "SELECT last_event_at FROM consumer_watermarks WHERE stream='PIPELINE' AND consumer='dashboard_ro'"
    ).fetchone()
    return parse_dt(row[0]) if row and row[0] else None


def _parse_threshold_secs(text: str | None) -> int | None:
    """Parse a norms red-band string (e.g. '>120m', '>4h') to seconds. First integer wins;
    an 'h' suffix means hours, anything else (or bare) means minutes (the dwell norm is in minutes)."""
    if not text:
        return None
    m = re.search(r"(\d+)\s*([mh]?)", text)
    if not m:
        return None
    n = int(m.group(1))
    return n * 3600 if m.group(2) == "h" else n * 60


def _dwell_threshold_secs(conn: sqlite3.Connection) -> int:
    """The stage-dwell red threshold from the norms table (design §1 Q2), with the design constant
    (`freshness.STALLED_DWELL_SECS`, 120 min) as the fallback if the norm is missing/unparseable."""
    row = conn.execute("SELECT red FROM norms WHERE metric='stage_dwell'").fetchone()
    parsed = _parse_threshold_secs(row[0]) if row else None
    return parsed if parsed is not None else fr.STALLED_DWELL_SECS


def _last_activity(
    conn: sqlite3.Connection, build_id: str, feature_id: str, started_at: object, queued_at: object
) -> datetime | None:
    """When this build last showed stage activity — the entry time of its current stage. The newest
    of: its latest completed stage event, its start, its queue time. Dwell = now - this."""
    candidates = [parse_dt(started_at), parse_dt(queued_at)]
    row = conn.execute(
        "SELECT completed_at FROM stage_events WHERE build_id=? OR feature_id=? "
        "ORDER BY completed_at DESC LIMIT 1",
        (build_id, feature_id),
    ).fetchone()
    if row and row[0]:
        candidates.append(parse_dt(row[0]))
    times = [t for t in candidates if t is not None]
    return max(times) if times else None


def _open_stalled(
    conn: sqlite3.Connection, issue_id: str, feature_id: str, build_id: str, dwell_secs: float, now: datetime
) -> bool:
    """Idempotent open. Never a duplicate row (issue_id PK) and never resets `opened_at` while the
    breach stands (opened once per breach). A previously-closed row re-opens as a NEW breach."""
    row = conn.execute("SELECT closed_at FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
    detail = f"stage dwell {vm.fmt_age(dwell_secs)} with no event"
    if row is None:
        conn.execute(
            """INSERT INTO issues (issue_id, scope_type, scope_id, kind, opened_at, closed_at, detail, source_ref)
               VALUES (?, 'feature', ?, 'stalled', ?, NULL, ?, ?)""",
            (issue_id, feature_id, iso(now), detail, f"builds/{build_id}"),
        )
        return True
    if row[0] is not None:  # was closed → a fresh breach re-opens it (new opened_at)
        conn.execute(
            "UPDATE issues SET opened_at=?, closed_at=NULL, detail=? WHERE issue_id=?",
            (iso(now), detail, issue_id),
        )
        return True
    return False  # already open: leave opened_at stable, write nothing (true idempotence)


def _close_stalled(conn: sqlite3.Connection, issue_id: str, now: datetime) -> bool:
    """Close a standing stalled issue. No-op if it is absent or already closed."""
    row = conn.execute(
        "SELECT 1 FROM issues WHERE issue_id=? AND kind='stalled' AND closed_at IS NULL", (issue_id,)
    ).fetchone()
    if row is None:
        return False
    conn.execute("UPDATE issues SET closed_at=? WHERE issue_id=?", (iso(now), issue_id))
    return True


def evaluate_dwell(conn: sqlite3.Connection, now: datetime | None = None) -> DwellEval:
    """One F-5 dwell pass over the in-flight builds. Returns what changed; appends the SSE change
    rows for any touched feature scope. Pure over the DB (no network) — unit-testable without a broker."""
    now = now or _now()
    result = DwellEval()

    alive = fr.projector_alive(_heartbeat_at(conn), now)
    wm_fresh = fr.watermark_fresh(_pipeline_watermark(conn), now)
    threshold = _dwell_threshold_secs(conn)

    builds = conn.execute(
        f"""SELECT build_id, feature_id, started_at, queued_at
              FROM builds WHERE status IN ({','.join('?' * len(_IN_FLIGHT))})""",
        _IN_FLIGHT,
    ).fetchall()

    scanned: set[str] = set()
    for build_id, feature_id, started_at, queued_at in builds:
        issue_id = f"stalled:{build_id}"
        scanned.add(issue_id)
        last = _last_activity(conn, build_id, feature_id, started_at, queued_at)
        dwell_secs = fr.age_secs(last, now)
        if dwell_secs is None:
            continue
        breach = fr.should_manufacture_stalled(
            dwell_secs=dwell_secs,
            projector_is_alive=alive,
            stream_watermark_fresh=wm_fresh,
            dwell_threshold_secs=threshold,
        )
        if breach:
            if _open_stalled(conn, issue_id, feature_id, build_id, dwell_secs, now):
                result.opened.add(issue_id)
                result.scope_keys.add(feature_id)
        # The run's events resumed (a live, fresh projection observing dwell back below threshold) —
        # clear the standing issue. A SUPPRESSED verdict (stale heartbeat/watermark) never closes it.
        elif alive and wm_fresh and dwell_secs < threshold and _close_stalled(conn, issue_id, now):
            result.closed.add(issue_id)
            result.scope_keys.add(feature_id)

    # Close stalled issues whose build has left the in-flight set (graduated/terminal) — the run
    # ended, so the stall is moot. Not driven by suppression; a completed build is a forward fact.
    for issue_id, scope_id in conn.execute(
        "SELECT issue_id, scope_id FROM issues WHERE kind='stalled' AND closed_at IS NULL"
    ).fetchall():
        if issue_id in scanned:
            continue
        if _close_stalled(conn, issue_id, now):
            result.closed.add(issue_id)
            if scope_id:
                result.scope_keys.add(scope_id)

    if result.opened or result.closed:
        record_change(conn, {_PANEL}, sorted(result.scope_keys), now)
    return result


async def dwell_loop(conn: sqlite3.Connection, *, interval_secs: int = DWELL_EVAL_SECS) -> None:
    """Run the dwell evaluation on the heartbeat cadence. A poison pass never wedges the loop."""
    while True:
        await asyncio.sleep(interval_secs)
        with contextlib.suppress(Exception):
            evaluate_dwell(conn)
