"""S2 SSE push (design §6): notification-only panel_update, id = change counter, Last-Event-ID
replay, and the refetch-all fallback when the client's id predates the retained window."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from backend import sse
from starlette.testclient import TestClient

from tests.nats_util import make_projected_db


def _log(conn: sqlite3.Connection, panel: str, at: str = "2026-07-11T14:13:00+00:00") -> None:
    conn.execute("INSERT INTO change_log (panel, scope_keys, at) VALUES (?, NULL, ?)", (panel, at))


def test_events_since_filters_by_subscribed_panels(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    for panel in ("p1", "p2", "p2"):
        _log(conn, panel)
    updates = sse.events_since(conn, 0, {"p2"})
    assert [u.panel for u in updates] == ["p2", "p2"]
    assert all(u.scope_keys == [] for u in updates)  # notification-only: no row data


def test_replay_returns_only_missed_after_last_id(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    for panel in ("p1", "p2", "p3"):
        _log(conn, panel)
    missed = sse.replay_or_refetch(conn, 1, {"p1", "p2", "p3"})
    assert [u.seq for u in missed] == [2, 3]  # cheap replay of exactly the missed notifications


def test_refetch_all_when_last_id_predates_retained_window(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    for _ in range(4):
        _log(conn, "p2")
    conn.execute("DELETE FROM change_log WHERE seq < 4")  # trim the retained window; min seq now 4
    result = sse.replay_or_refetch(conn, 1, {"p1", "p2"})  # id 1 fell out of the window
    assert {u.panel for u in result} == {"p1", "p2"}       # one refetch-all per subscribed panel
    assert all(u.seq == sse.max_seq(conn) for u in result)


def test_sse_event_format_is_notification_only() -> None:
    upd = sse.PanelUpdate(seq=7, panel="p2", scope_keys=["FEAT-1"], at="2026-07-11T14:13:00+00:00")
    text = upd.to_sse()
    assert text.startswith("id: 7\n")
    assert "event: panel_update\n" in text
    assert '"panel": "p2"' in text and '"scope_keys": ["FEAT-1"]' in text


def test_parse_panels_defaults_and_filters() -> None:
    assert sse.parse_panels(None) == set(sse.DEFAULT_PANELS)
    assert sse.parse_panels("p2,p3") == {"p2", "p3"}
    assert sse.parse_panels("bogus") == set(sse.DEFAULT_PANELS)  # unknown -> safe default


def test_events_endpoint_streams_panel_update(operator_client: TestClient) -> None:
    """The /events endpoint (once=1) emits a refetch-all panel_update on a fresh subscription."""
    r = operator_client.get("/events?once=1&panels=p2")
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "event: panel_update" in r.text


def test_events_endpoint_401_unauthenticated(client: TestClient) -> None:
    assert client.get("/events?once=1", follow_redirects=False).status_code == 401


def test_events_endpoint_403_for_client_tenant(clientco_client: TestClient) -> None:
    assert clientco_client.get("/events?once=1", follow_redirects=False).status_code == 403
