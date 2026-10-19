"""Copy Forge's build record into the dashboard's store, read-only, every 15 seconds.

Design, 17 October 2026, requirement 1: Forge's record is the dashboard's single source of truth,
so the feed copies all of it that the pages read: builds, stage rows, planning runs, planning
events, queue items, missions and each mission's plan (its features and what each needs), and each
build's publication record (whether it reached main; read by the summary file's `merges`). It does
this in one short read transaction and closes Forge's database between passes, so it never holds a
read open long enough to get in Forge's way.

- Forge's database is opened URI `mode=ro` from a read-only mount; a write on that handle fails.
- Builds, planning runs, queue items and missions change in place, so each pass replaces them.
- Stage rows and planning events are only ever added, so each pass adds the rows after the last
  id already copied. If Forge's newest id is lower than ours (its record was restored or
  replaced), the copy of that table is thrown away and read again in full.
- A pass that cannot read Forge records the error and keeps the last good copy; the pages then
  show the stale-data banner with the time of the last good read. It never crashes the feed.
- Every successful pass adds one to `mirror_status.read_count`. The board counts passes with it
  when it confirms an answer sent from the dashboard (design of 19 October 2026): only successful
  reads are counted, so a stopped feed never confirms or clears anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.projector.store import iso, record_change

# The feed's container mounts Forge's ledger volume read-only here (docker-compose.yml).
DEFAULT_FORGE_DB_PATH = "/forge-state/forge.db"
MIRROR_INTERVAL_SECS = 15
# Live-update notifications older than this are pruned; a client that missed them refetches all.
CHANGE_LOG_KEEP = timedelta(hours=1)

# Every page section that reads the copy is told to refresh when the copy changes.
MIRROR_PANELS: frozenset[str] = frozenset({"p2", "p3", "p4", "p5", "p7", "proj"})

# Columns copied, by table. Forge's own names; a column Forge's schema lacks (an older ledger) is
# copied as NULL rather than failing the pass.
_REPLACED: dict[str, tuple[str, tuple[str, ...], str]] = {
    # dashboard table: (forge table, columns, primary key)
    "forge_builds": ("builds", (
        "build_id", "feature_id", "repo", "branch", "project", "status", "triggered_by",
        "originating_user", "correlation_id", "queued_at", "started_at", "completed_at", "error",
        "terminal_class", "mission_id", "mission_role", "pending_approval_request_id", "target_branch"),
        "build_id"),
    # What the merge word did: the build's publication record, which says whether it is on main.
    "forge_publication_records": ("publication_records", (
        "build_id", "feature_id", "repo", "target_branch", "j_commit", "result", "decided_at", "updated_at"),
        "build_id"),
    "forge_planning_runs": ("planning_runs", (
        "correlation_id", "state", "originating_user", "expected_approver", "request_text",
        "target_repo", "triggered_by", "defer_count", "paused_at", "escalated_at", "queued_at",
        "started_at", "completed_at", "error", "pending_approval_request_id", "parent_request_id"),
        "correlation_id"),
    "forge_queue": ("work_queue", (
        "id", "sentence", "target_repo", "kind", "status", "originating_user", "correlation_id",
        "queued_at", "admitted_at", "closed_at", "closed_reason"), "id"),
    "forge_missions": ("missions", (
        "id", "title", "target_repo", "status", "correlation_id", "created_at", "closed_at",
        "closed_reason", "plan_request_id", "originating_user", "approved_by", "approved_at",
        # A stop asked for and not yet finished (Forge's missions section 11). A ledger from before
        # stopping lacks both, so they are copied as NULL and the mission shows as it did.
        "stop_requested_at", "stop_requested_by"), "id"),
    # A mission's plan, in Forge's own order (its rowid, copied as forge_order).
    "forge_mission_features": ("mission_features", (
        "forge_order", "mission_id", "key", "sentence", "no_sign_in"), "rowid"),
    "forge_mission_needs": ("mission_needs", (
        "forge_order", "mission_id", "key", "needs_key"), "rowid"),
}
#: Copied as Forge's rowid: the order Forge filed the rows in, which its own queries use.
_ROWID_COLUMN = "forge_order"
_APPENDED: dict[str, tuple[str, tuple[str, ...]]] = {
    "forge_stage_log": ("stage_log", (
        "id", "build_id", "stage_label", "target_identifier", "status", "gate_mode", "started_at",
        "completed_at", "details_json")),
    "forge_planning_events": ("planning_run_events", (
        "id", "correlation_id", "stage_label", "status", "gate_mode", "actor_identity",
        "details_json", "recorded_at")),
}


def forge_db_path() -> Path:
    """Where Forge's record is mounted. `FACTORY_FORGE_DB_PATH` overrides the compose default."""
    return Path(os.environ.get("FACTORY_FORGE_DB_PATH", DEFAULT_FORGE_DB_PATH))


def connect_forge_ro(path: Path) -> sqlite3.Connection:
    """Open Forge's database URI `mode=ro`. A write attempt on this handle fails."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class _Snapshot:
    replaced: dict[str, list[tuple[object, ...]]]
    appended: dict[str, list[tuple[object, ...]]]
    forge_max_ids: dict[str, int]


def _columns(forge: sqlite3.Connection, table: str) -> set[str] | None:
    rows = forge.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(r[1]) for r in rows} if rows else None


def _select(table: str, wanted: Sequence[str], have: set[str]) -> str:
    cols = ", ".join(f"rowid AS {c}" if c == _ROWID_COLUMN else c if c in have else f"NULL AS {c}"
                     for c in wanted)
    return f"SELECT {cols} FROM {table}"


def _read_forge(forge: sqlite3.Connection, after_ids: dict[str, int]) -> _Snapshot:
    """One read transaction over Forge's tables: the whole of each replaced table, and the rows
    after `after_ids` of each appended table."""
    snap = _Snapshot({}, {}, {})
    forge.execute("BEGIN")
    try:
        for dash_table, (table, cols, key) in _REPLACED.items():
            have = _columns(forge, table)
            if have is None:  # e.g. a ledger from before missions existed
                snap.replaced[dash_table] = []
                continue
            snap.replaced[dash_table] = [
                tuple(r) for r in forge.execute(f"{_select(table, cols, have)} ORDER BY {key}")
            ]
        for dash_table, (table, cols) in _APPENDED.items():
            have = _columns(forge, table)
            if have is None:
                snap.appended[dash_table] = []
                snap.forge_max_ids[dash_table] = 0
                continue
            max_id = forge.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
            snap.forge_max_ids[dash_table] = int(max_id)
            after = after_ids[dash_table] if after_ids[dash_table] <= max_id else 0
            snap.appended[dash_table] = [
                tuple(r) for r in forge.execute(
                    f"{_select(table, cols, have)} WHERE id > ? ORDER BY id", (after,)
                )
            ]
    finally:
        forge.execute("COMMIT")
    return snap


def _local_max_ids(dash: sqlite3.Connection) -> dict[str, int]:
    return {
        t: int(dash.execute(f"SELECT COALESCE(MAX(id), 0) FROM {t}").fetchone()[0]) for t in _APPENDED
    }


def _hash(snap: _Snapshot) -> str:
    h = hashlib.sha256()
    for table in sorted(snap.replaced):
        h.update(table.encode())
        for row in snap.replaced[table]:
            h.update(repr(row).encode())
    return h.hexdigest()


def _write(dash: sqlite3.Connection, snap: _Snapshot, local_max: dict[str, int]) -> bool:
    """Apply a snapshot in one write transaction. Returns True when anything changed."""
    new_hash = _hash(snap)
    row = dash.execute("SELECT content_hash FROM mirror_status WHERE source='forge'").fetchone()
    replaced_changed = row is None or row[0] != new_hash
    appended_changed = False
    dash.execute("BEGIN IMMEDIATE")
    try:
        if replaced_changed:
            for dash_table, rows in snap.replaced.items():
                cols = _REPLACED[dash_table][1]
                dash.execute(f"DELETE FROM {dash_table}")
                dash.executemany(
                    f"INSERT INTO {dash_table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    rows,
                )
        for dash_table, rows in snap.appended.items():
            cols = _APPENDED[dash_table][1]
            if snap.forge_max_ids[dash_table] < local_max[dash_table]:
                # Forge's record was replaced by an older or different one: start this table again.
                dash.execute(f"DELETE FROM {dash_table}")
                appended_changed = True
            if rows:
                dash.executemany(
                    f"INSERT OR REPLACE INTO {dash_table} ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' * len(cols))})",
                    rows,
                )
                appended_changed = True
        dash.execute(
            """INSERT INTO mirror_status (source, content_hash) VALUES ('forge', ?)
               ON CONFLICT(source) DO UPDATE SET content_hash=excluded.content_hash""",
            (new_hash,),
        )
        dash.execute("COMMIT")
    except BaseException:
        dash.execute("ROLLBACK")
        raise
    return replaced_changed or appended_changed


def _stamp(dash: sqlite3.Connection, now: datetime, *, ok: bool, error: str | None) -> None:
    if ok:
        dash.execute(
            """INSERT INTO mirror_status (source, last_attempt_at, last_success_at, last_error, read_count)
               VALUES ('forge', ?, ?, NULL, 1)
               ON CONFLICT(source) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
                   last_success_at=excluded.last_success_at, last_error=NULL,
                   read_count=COALESCE(mirror_status.read_count, 0) + 1""",
            (iso(now), iso(now)),
        )
    else:
        dash.execute(
            """INSERT INTO mirror_status (source, last_attempt_at, last_error) VALUES ('forge', ?, ?)
               ON CONFLICT(source) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
                   last_error=excluded.last_error""",
            (iso(now), error),
        )


def mirror_once(dash: sqlite3.Connection, forge_path: Path, now: datetime | None = None) -> bool:
    """One pass. Returns True when the copy changed (and records a live-update notification).

    A missing or unreadable Forge database is recorded as a failed read, never as an empty
    factory: the last good copy stays and the stale-data banner shows when it was read."""
    now = now or datetime.now(UTC)
    if not forge_path.exists():
        _stamp(dash, now, ok=False, error=f"the factory's record is not at {forge_path}")
        return False
    try:
        forge = connect_forge_ro(forge_path)
        try:
            snap = _read_forge(forge, _local_max_ids(dash))
        finally:
            forge.close()  # closed between passes, so no read stays open on Forge's database
    except sqlite3.Error as exc:
        _stamp(dash, now, ok=False, error=f"could not read the factory's record: {exc}")
        return False
    changed = _write(dash, snap, _local_max_ids(dash))
    _stamp(dash, now, ok=True, error=None)
    if changed:
        record_change(dash, set(MIRROR_PANELS), [], now)
    return changed


def prune_change_log(dash: sqlite3.Connection, now: datetime) -> None:
    dash.execute("DELETE FROM change_log WHERE at < ?", (iso(now - CHANGE_LOG_KEEP),))


async def mirror_loop(dash: sqlite3.Connection, *, interval: int = MIRROR_INTERVAL_SECS) -> None:  # pragma: no cover
    path = forge_db_path()
    while True:
        now = datetime.now(UTC)
        # A failed pass is recorded by mirror_once; anything unexpected must not stop the loop.
        with contextlib.suppress(Exception):
            mirror_once(dash, path, now)
            prune_change_log(dash, now)
        await asyncio.sleep(interval)
