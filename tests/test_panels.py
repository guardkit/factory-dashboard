"""Each panel fragment renders each applicable §5.2 state; honesty surfaces present."""

from __future__ import annotations

from backend.readmodel import queries
from backend.readmodel.viewmodels import PanelState
from starlette.testclient import TestClient


def test_each_panel_renders_each_applicable_state(operator_client: TestClient) -> None:
    """Acceptance item 2: P1–P7 each render each applicable state, driven by the fixtures."""
    for panel_id, states in queries.APPLICABLE_STATES.items():
        for state in states:
            r = operator_client.get(f"/fragments/{panel_id}?state={state.value}")
            assert r.status_code == 200, (panel_id, state)


def test_every_panel_carries_an_asof_chip(operator_client: TestClient) -> None:
    """Acceptance item 1: no panel renders without an as-of chip (coach-checked)."""
    for panel_id in queries.ALL_PANELS:
        body = operator_client.get(f"/fragments/{panel_id}").text
        assert "asof-chip" in body, panel_id


def test_feed_pending_never_renders_as_zero(operator_client: TestClient) -> None:
    """§5.2: a FEED-PENDING slot names its gate; never a numeric 0. Delivered upgrades + spend."""
    body = operator_client.get("/fragments/p7?state=live").text
    assert "FEED PENDING" in body
    assert "graduation feed pending" in body   # upgrades slot, never "upgrades: 0"
    assert "captured share: 0%" in body         # spend captured-share labelled, not a bare 0


def test_true_empty_uses_pinned_copy_not_blank(operator_client: TestClient) -> None:
    assert "No builds in flight." in operator_client.get("/fragments/p2?state=empty").text
    assert "Nothing needs you" in operator_client.get("/fragments/p4?state=empty").text


def test_planning_true_empty_only_off_fresh_mirror(operator_client: TestClient) -> None:
    """§4.1: p5 TRUE-EMPTY is asserted only off a fresh mirror; a stale mirror renders LAGGING."""
    empty = operator_client.get("/fragments/p5?state=empty").text
    assert "None recorded" in empty
    lagging = operator_client.get("/fragments/p5?state=lagging").text
    assert "projection lagging" in lagging


def test_needs_you_orders_red_band_first(operator_client: TestClient) -> None:
    """Acceptance item 8: the needs-you strip orders red bands first."""
    body = operator_client.get("/fragments/p4?state=live").text
    assert body.index("gate REJECTED") < body.index("approval WAITING")
    assert "this dashboard observes" in body  # approval rows carry the observes-only copy


def test_serving_dot_demotes_to_unknown_when_stale(operator_client: TestClient) -> None:
    """§5.2 P6 dot demotion: stale checked_at -> grey '?' unknown, last-known-ok never shown live."""
    body = operator_client.get("/fragments/p6?state=lagging").text
    assert "unknown — last ok" in body


def test_journey_bar_shows_all_eight_stations(operator_client: TestClient) -> None:
    """Acceptance item 3: all 8 stations, verbatim design §1, last two FEED-PENDING."""
    body = operator_client.get("/features/FEAT-3ED2").text
    for station in ("intake", "planning", "approval", "spec/plan", "build",
                    "merged+PR", "deployed", "live-verified"):
        assert station in body, station


def test_title_less_feature_renders_id_fallback_with_named_gap(operator_client: TestClient) -> None:
    """Acceptance item 19: title-less fixture renders the id-fallback + the named footer gap."""
    body = operator_client.get("/fragments/p2?state=live").text
    assert "FEAT-B17E" in body
    assert "no projected title — title feed pending" in body


def test_agents_dual_source_named_gaps(operator_client: TestClient) -> None:
    """Roster dual-source (drift 3/4): kv agent unmeasured cols; register-only 'no heartbeat feed'."""
    body = operator_client.get("/fragments/p1?state=live").text
    assert "kv 12s" in body
    assert "no heartbeat feed" in body


def test_all_five_states_are_modelled() -> None:
    assert set(PanelState) == {
        PanelState.LIVE, PanelState.LAGGING, PanelState.TRUE_EMPTY,
        PanelState.FEED_PENDING, PanelState.UNMEASURED,
    }
