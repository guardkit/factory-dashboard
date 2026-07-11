"""feat_3ed2 renders the ux §4.2 checklist exactly (acceptance item 4)."""

from __future__ import annotations

from starlette.testclient import TestClient


def test_feat_3ed2_renders_the_acceptance_checklist(operator_client: TestClient) -> None:
    body = operator_client.get("/features/FEAT-3ED2").text
    # verdict [G] + "on the measured signals", computed from the seeded norms
    assert "on the measured signals" in body
    assert "[G]" in body
    # turns/ceiling rows present with — and the A-4 reason
    assert "turns per task" in body
    assert "SDK ceiling hits" in body
    assert "A-4" in body
    # context-counters line renders un-banded
    assert "0/11 task failures · 74m55s wall" in body
    # 0 machine-visible issues + the two named coverage gaps
    assert "0 machine-visible" in body
    assert "No open issues in the register for this feature." in body
    assert "A-5" in body and "A-7" in body
    # Spend FEED-PENDING with captured share 0%
    assert "SPEND" in body and "captured share: 0%" in body
    # src: forge_sqlite badge visible (short form ⌗sq)
    assert "⌗sq" in body
    # dual as-of: mirror for Q1, events for stage history
    assert "mirror" in body and "events" in body


def test_feature_page_deploy_stations_are_feed_pending(operator_client: TestClient) -> None:
    body = operator_client.get("/features/FEAT-3ED2").text
    assert "NOT INSTRUMENTED" in body
    assert "deployed" in body and "live-verified" in body


def test_unknown_feature_renders_honest_placeholder(operator_client: TestClient) -> None:
    r = operator_client.get("/features/FEAT-NOPE")
    assert r.status_code == 200
    assert "No projected state for this feature yet" in r.text
