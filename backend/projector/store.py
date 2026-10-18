"""The feed's writing side of the dashboard store: the one read-write connection, timestamps,
the heartbeat and live-update notifications.

The feed (`python -m backend.projector`) is the only process that writes the copy of Forge's
record; every web request opens the store read-only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def open_rw(db_path: Path) -> sqlite3.Connection:
    """The feed's read-write connection (autocommit, WAL). Never used by a web request."""
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def parse_dt(value: object) -> datetime | None:
    """An ISO-8601 string (or a datetime) as an aware UTC datetime; None for anything else."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def record_change(conn: sqlite3.Connection, panels: set[str], scope_keys: list[str], now: datetime) -> None:
    """Append one live-update notification per changed page section (no row data)."""
    scope_json = json.dumps(scope_keys) if scope_keys else None
    for panel in sorted(panels):
        conn.execute(
            "INSERT INTO change_log (panel, scope_keys, at) VALUES (?, ?, ?)",
            (panel, scope_json, iso(now)),
        )


def write_heartbeat(conn: sqlite3.Connection, now: datetime | None = None) -> None:
    """The feed says it is alive every 10 seconds, whether or not Forge's record changed."""
    now = now or datetime.now(UTC)
    conn.execute(
        """INSERT INTO service_health (service, status, detail, checked_at)
           VALUES ('projector', 'ok', 'feed heartbeat', ?)
           ON CONFLICT(service) DO UPDATE SET
               status='ok', detail='feed heartbeat', checked_at=excluded.checked_at""",
        (iso(now),),
    )
