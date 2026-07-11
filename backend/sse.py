"""SSE push contract (design §6 / DDR-DASH-002) — notification-only, rendered as `panel_update`.

The push channel carries NO row data — only `{panel, scope_keys, at}` — so tenancy is trivially
safe (the re-fetch goes back through the tenant-bound `mode=ro` query layer). The `id:` is the
`change_log.seq` (the read-DB change counter, design §6). A reconnecting client sends
`Last-Event-ID`; the server replays missed notifications cheaply, OR — if the client's id predates
the retained window — emits a one-shot refetch-all of its subscribed panels (the safe D1 default,
ux §6.3). This module is pure (no network, no async) so the replay/fallback logic is unit-testable;
the streaming loop lives on the `/events` endpoint.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

DEFAULT_PANELS: tuple[str, ...] = ("p1", "p2", "p3", "p4", "p5", "p6", "p7", "proj")


@dataclass(frozen=True)
class PanelUpdate:
    seq: int
    panel: str
    scope_keys: list[str]
    at: str

    def to_sse(self) -> str:
        data = json.dumps({"panel": self.panel, "scope_keys": self.scope_keys, "at": self.at})
        return f"id: {self.seq}\nevent: panel_update\ndata: {data}\n\n"


def _scope(scope_keys: str | None) -> list[str]:
    if not scope_keys:
        return []
    try:
        parsed = json.loads(scope_keys)
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def max_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM change_log").fetchone()
    return int(row[0])


def min_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MIN(seq), 0) FROM change_log").fetchone()
    return int(row[0])


def events_since(conn: sqlite3.Connection, last_id: int, panels: set[str]) -> list[PanelUpdate]:
    """Change-log rows after `last_id` for the subscribed panels — the cheap replay path."""
    rows = conn.execute(
        "SELECT seq, panel, scope_keys, at FROM change_log WHERE seq > ? ORDER BY seq", (last_id,)
    ).fetchall()
    return [PanelUpdate(int(s), p, _scope(sk), a) for s, p, sk, a in rows if p in panels]


def refetch_all(conn: sqlite3.Connection, panels: set[str]) -> list[PanelUpdate]:
    """One-shot refetch-all fallback: a `panel_update` per subscribed panel at the current head id
    (used when Last-Event-ID predates the retained change-log window)."""
    head = max_seq(conn)
    return [PanelUpdate(head, panel, [], "") for panel in sorted(panels)]


def replay_or_refetch(conn: sqlite3.Connection, last_id: int, panels: set[str]) -> list[PanelUpdate]:
    """The reconnect decision (ux §6.3): if the client's last id is still within the retained
    window, replay the exact missed notifications; otherwise refetch-all its subscribed panels."""
    if last_id <= 0:
        return refetch_all(conn, panels)
    lo = min_seq(conn)
    if lo > 0 and last_id < lo - 1:  # its id fell out of the retained window
        return refetch_all(conn, panels)
    return events_since(conn, last_id, panels)


def parse_panels(raw: str | None) -> set[str]:
    if not raw:
        return set(DEFAULT_PANELS)
    wanted = {p.strip() for p in raw.split(",") if p.strip()}
    return wanted & set(DEFAULT_PANELS) or set(DEFAULT_PANELS)
