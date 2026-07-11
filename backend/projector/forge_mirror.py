"""Read-only forge SQLite mirror (WAL-courtesy — design §4.7 / fence 4).

Mirrors forge durable state into the read model on a periodic pass: `planning_runs` → P5
`planning_mirror`, and `stage_log` → P3 `stage_events` (the SQLite backfill/reconcile that carries
GATED where no bus producer exists — drift 5). The ledger bootstrap from forge `builds` is S3's, not
here.

**Fence 4 discipline (verbatim):** URI `mode=ro`; autocommit; per-query transactions <100 ms;
the forge connection is CLOSED between passes — a long-lived read transaction would block forge's
WAL checkpointing (the factory caring about the dashboard, which is forbidden).

**The mirror path is EXPLICIT CONFIG** (`FACTORY_FORGE_DB_PATH`), pinned on this host to the
forge-prod bind-mount — NEVER the `~/.forge` default, which is a stale dev DB written by a
`langgraph dev` process (capability note drift 1).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from backend.projector.projections.base import iso

# The forge-prod bind-mount on this host. NEVER ~/.forge (a stale dev DB — drift 1).
DEFAULT_FORGE_DB_PATH = "/home/richardwoollcott/forge-prod-state/.forge/forge.db"
MIRROR_INTERVAL_SECS = 30


def forge_db_path() -> Path:
    """The configured forge DB path. Explicit env override; the pinned prod bind-mount default."""
    return Path(os.environ.get("FACTORY_FORGE_DB_PATH", DEFAULT_FORGE_DB_PATH))


def connect_forge_ro(path: Path) -> sqlite3.Connection:
    """Open the forge DB URI `mode=ro` (fence 4). A write attempt on this handle MUST fail."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _feature_id_from(row: sqlite3.Row) -> str | None:
    cols = set(row.keys())
    details = row["details_json"] if "details_json" in cols else None
    if details:
        try:
            parsed = json.loads(details)
            if isinstance(parsed, dict) and parsed.get("feature_id"):
                return str(parsed["feature_id"])
        except (ValueError, TypeError):
            pass
    if "target_identifier" in cols and row["target_identifier"]:
        return str(row["target_identifier"])
    build_id = row["build_id"] if "build_id" in cols else ""
    if build_id and build_id.startswith("build-"):
        # build-FEAT-XXXX-<ts> -> FEAT-XXXX
        parts = build_id.split("-")
        if len(parts) >= 3:
            return f"{parts[1]}-{parts[2]}"
    return None


def mirror_once(dash: sqlite3.Connection, forge_path: Path, now: datetime | None = None) -> set[str]:
    """One mirror pass. Opens the forge DB `mode=ro`, mirrors planning + stage rows, CLOSES it,
    and stamps the FORGE_MIRROR watermark. Returns the panel ids that changed."""
    now = now or datetime.now(UTC)
    panels: set[str] = set()
    if not forge_path.exists():
        _stamp_pass(dash, now)  # a missing forge DB is honestly recorded as a pass with no rows
        return panels

    forge = connect_forge_ro(forge_path)
    try:
        panels |= _mirror_planning(dash, forge, now)
        panels |= _mirror_stage_log(dash, forge, now)
    finally:
        forge.close()  # closed between passes — WAL-courtesy (fence 4)
    _stamp_pass(dash, now)
    return panels


def _mirror_planning(dash: sqlite3.Connection, forge: sqlite3.Connection, now: datetime) -> set[str]:
    rows = forge.execute(
        """SELECT correlation_id, state, originating_user, expected_approver, request_text,
                  target_repo, defer_count, escalated_at, handoff_branch, handoff_path,
                  queued_at, started_at, completed_at, error
             FROM planning_runs"""
    ).fetchall()
    changed = False
    for r in rows:
        dash.execute(
            """INSERT INTO planning_mirror (correlation_id, state, originating_user, expected_approver,
                   request_text, target_repo, queued_at, started_at, completed_at, handoff_branch,
                   handoff_path, defer_count, escalated_at, error, mirrored_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(correlation_id) DO UPDATE SET
                   state=excluded.state, originating_user=excluded.originating_user,
                   expected_approver=excluded.expected_approver, request_text=excluded.request_text,
                   target_repo=excluded.target_repo, queued_at=excluded.queued_at,
                   started_at=excluded.started_at, completed_at=excluded.completed_at,
                   handoff_branch=excluded.handoff_branch, handoff_path=excluded.handoff_path,
                   defer_count=excluded.defer_count, escalated_at=excluded.escalated_at,
                   error=excluded.error, mirrored_at=excluded.mirrored_at""",
            (r["correlation_id"], r["state"], r["originating_user"], r["expected_approver"],
             r["request_text"], r["target_repo"], r["queued_at"], r["started_at"], r["completed_at"],
             r["handoff_branch"], r["handoff_path"], r["defer_count"], r["escalated_at"], r["error"],
             iso(now)),
        )
        changed = True
    return {"p5"} if changed else set()


def _mirror_stage_log(dash: sqlite3.Connection, forge: sqlite3.Connection, now: datetime) -> set[str]:
    rows = forge.execute(
        """SELECT build_id, stage_label, target_kind, target_identifier, status, gate_mode,
                  coach_score, duration_secs, completed_at, details_json
             FROM stage_log"""
    ).fetchall()
    changed = False
    for r in rows:
        feature_id = _feature_id_from(r)
        completed = r["completed_at"]
        # Idempotent + de-duped against the bus projection (same feature/stage/completed/status).
        dup = dash.execute(
            "SELECT 1 FROM stage_events WHERE feature_id=? AND stage_label=? AND completed_at=? AND status=?",
            (feature_id, r["stage_label"], completed, r["status"]),
        ).fetchone()
        if dup is not None:
            continue
        dash.execute(
            """INSERT INTO stage_events (build_id, feature_id, stage_label, target_kind, status,
                   gate_mode, coach_score, duration_secs, completed_at, origin)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'forge_sqlite')""",
            (r["build_id"], feature_id, r["stage_label"], r["target_kind"], r["status"],
             r["gate_mode"], r["coach_score"], r["duration_secs"], completed),
        )
        changed = True
    return {"p3"} if changed else set()


def _stamp_pass(dash: sqlite3.Connection, now: datetime) -> None:
    """Record the forge-mirror pass time as a watermark (drives the §5.6 'forge mirror ⟨ts⟩' chip
    and P5's mirror-freshness gate — a stale/never-run mirror renders LAGGING, never TRUE-EMPTY)."""
    dash.execute(
        """INSERT INTO consumer_watermarks (stream, consumer, last_stream_seq, last_event_at, updated_at)
           VALUES ('FORGE_MIRROR', 'mirror', 1, ?, ?)
           ON CONFLICT(stream, consumer) DO UPDATE SET
               last_stream_seq=consumer_watermarks.last_stream_seq+1,
               last_event_at=excluded.last_event_at, updated_at=excluded.updated_at""",
        (iso(now), iso(now)),
    )


async def mirror_loop(dash: sqlite3.Connection, *, interval: int = MIRROR_INTERVAL_SECS) -> None:  # pragma: no cover
    path = forge_db_path()
    while True:
        # a mirror hiccup is honestly reflected by a stale FORGE_MIRROR watermark, never a crash
        with contextlib.suppress(sqlite3.Error):
            mirror_once(dash, path)
        await asyncio.sleep(interval)
