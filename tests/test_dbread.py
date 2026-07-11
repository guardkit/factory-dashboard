"""S2 live query layer (dbread): honest LAGGING on an un-projected DB, live panels off projected
rows, P6 dot demotion, and P5's stale-mirror-is-LAGGING (never fake TRUE-EMPTY)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.projector.consumers import apply_message, write_heartbeat
from backend.readmodel import dbread
from backend.readmodel.viewmodels import PanelState

from tests.nats_util import envelope, make_projected_db

NOW = datetime(2026, 7, 11, 14, 13, 34, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


# --- un-projected DB: honest LAGGING, never fake-live green ------------------


def test_empty_db_renders_lagging_with_asof_chip(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)  # no projector heartbeat has been written
    panel = dbread.agents_panel(conn, NOW)
    assert panel.state is PanelState.LAGGING
    assert panel.as_of is not None  # the chip is NEVER omitted (coach-checked)


def test_page_chrome_banner_when_projector_stale(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    chrome = dbread.page_chrome(conn, NOW)
    assert chrome.projector_state == "stale"
    assert chrome.banner is not None and chrome.banner.kind == "projector_stalled"


def test_page_chrome_live_when_heartbeat_fresh(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    write_heartbeat(conn, NOW)
    chrome = dbread.page_chrome(conn, NOW)
    assert chrome.projector_state == "live" and chrome.banner is None


# --- live panels off projected rows -----------------------------------------


def test_live_roster_dual_source(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    write_heartbeat(conn, NOW)
    apply_message(conn, "fleet.heartbeat.forge-orch",
                  envelope("agent_heartbeat", {"agent_id": "forge-orch", "status": "ready", "queue_depth": 2},
                           timestamp=NOW), NOW)
    apply_message(conn, "$KV.agent-registry.jarvis", b'{"name": "jarvis", "status": "ready"}', NOW)
    apply_message(conn, "fleet.register", envelope("agent_register", {"agent_id": "forge", "name": "forge"},
                                                   timestamp=NOW), NOW)
    panel = dbread.agents_panel(conn, NOW)
    assert panel.state is PanelState.LIVE
    by_kind = {a.source_kind: a for a in panel.agents}
    assert by_kind["kv"].liveness.startswith("kv ")
    assert "no heartbeat feed" in by_kind["register_only"].liveness


def test_build_board_renders_wave_only_progress(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    write_heartbeat(conn, NOW)
    apply_message(conn, "pipeline.build-started.FEAT-51B0",
                  envelope("build_started", {"feature_id": "FEAT-51B0", "build_id": "b1", "wave_total": 6},
                           timestamp=NOW), NOW)
    apply_message(conn, "pipeline.build-progress.FEAT-51B0",
                  envelope("build_progress", {"feature_id": "FEAT-51B0", "build_id": "b1", "wave": 4,
                                              "wave_total": 6, "overall_progress_pct": 64.0, "elapsed_seconds": 60},
                           timestamp=NOW), NOW)
    panel = dbread.build_board(conn, NOW)
    assert panel.state is PanelState.LIVE
    assert panel.rows[0].journey.progress_line == "wave 4/6 · 64%"


def test_serving_dot_demotes_to_unknown_when_stale(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    stale = _iso(NOW - timedelta(hours=2))
    conn.execute("INSERT INTO service_health (service, status, detail, checked_at) VALUES ('litellm','ok','ok',?)",
                 (stale,))
    panel = dbread.serving(conn, NOW)
    litellm = next(s for s in panel.services if s.name == "litellm")
    assert litellm.dot == "unknown"
    assert "unknown — last ok" in litellm.detail  # last-known-ok never masquerades as currently-ok


def test_planning_stale_mirror_is_lagging_not_true_empty(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    write_heartbeat(conn, NOW)  # projector alive...
    old = _iso(NOW - timedelta(hours=1))  # ...but the mirror pass is stale
    conn.execute(
        "INSERT INTO consumer_watermarks (stream, consumer, last_stream_seq, last_event_at, updated_at) "
        "VALUES ('FORGE_MIRROR','mirror',1,?,?)",
        (old, old),
    )
    panel = dbread.planning(conn, NOW)
    assert panel.state is PanelState.LAGGING  # never a fake "genuinely no runs" TRUE-EMPTY


def test_panel_view_and_home_view_open_read_only(tmp_path: Path) -> None:
    db_path, conn = make_projected_db(tmp_path)
    write_heartbeat(conn, NOW)
    # the composed views open their own mode=ro connection and never raise on a sparse DB
    home = dbread.home_view(db_path, NOW)
    assert home.chrome.projector_state == "live"
    single = dbread.panel_view(db_path, "p6", NOW)
    assert single.panel_id == "p6"
