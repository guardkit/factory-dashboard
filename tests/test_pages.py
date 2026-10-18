"""Every page on real (copied) data: titles first, plain words, delivery read right, stale banner."""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from backend.app import create_app
from backend.projector.forge_mirror import mirror_once
from backend.projector.store import iso, open_rw, write_heartbeat
from starlette.testclient import TestClient

from tests import forge_fake
from tests.conftest import LOOPBACK_URL, login
from tests.forge_fake import ForgeFake, delivered_feature, store_from

TENANTS_TEST = Path(__file__).resolve().parent / "fixtures" / "tenants_test.yaml"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path, ForgeFake]]:
    """A factory with one delivered feature, one declined, one building, one waiting for the merge
    decision, a failed planning run and a mission whose merge row reuses a feature's code."""
    monkeypatch.setattr(forge_fake, "T0", datetime.now(UTC) - timedelta(hours=3))
    fake = ForgeFake(tmp_path / "forge.db")
    delivered_feature(fake, 1, text="Search box that filters notes by title. It updates as you type.")
    fake.planning("c2", "Show a Clear button.")
    fake.build("b2", "FEAT-0002", "c2")
    fake.merge_offer("b2", 81)
    fake.merge_decision("b2", 82, "reject")
    fake.planning("c3", "Count the notes <script>alert(1)</script>.")
    fake.build("b3", "FEAT-0003", "c3", status="RUNNING", queued=100, started=100, completed=None)
    fake.planning("c4", "Delete a note.")
    fake.build("b4", "FEAT-0004", "c4")
    fake.merge_offer("b4", 90)
    fake.planning("c5", "Sort the notes.", state="FAILED", error="The planner gave up. Long story.")
    fake.mission("m-1", "Mark notes as done", "mc", status="NOT_MERGED", closed=170)
    fake.planning("mc-A-a1", "Add a done flag.")
    fake.build("bA", "FEAT-000A", "mc-A-a1", mission_id="m-1", mission_role="feature")
    fake.merge_decision("bA", 81)
    fake.merged("bA", 82, mission_branch=True)
    fake.build("bM", "FEAT-000A", "mc", queued=160, started=160, completed=160, mission_id="m-1",
               mission_role="mission-merge")
    fake.merge_offer("bM", 161)
    fake.merge_decision("bM", 162, "reject")
    store = tmp_path / "readmodel.db"
    store_from(fake, store).close()
    app = create_app(db_path=store, tenants_path=TENANTS_TEST)
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        login(client)
        yield client, store, fake


INTERNAL_SHORTHAND = ("⌗", "ADR-", "S1 / D0", "WS2", "A-4", "IN-5", "BUILD_QUEUED", "GATED", "merge unverified",
                      "projector", "feed pending", "FEED PENDING", "⟨", "graduation", "M-D2")


def _text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def test_every_page_renders_and_speaks_plain_english(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    for path in ("/", "/features", "/features?days=all", "/features/FEAT-0001", "/features/c5",
                 "/projects/acme/notes", "/delivered", "/reports", "/reports?view=export", "/issues", "/fleet"):
        r = client.get(path)
        assert r.status_code == 200, path
        visible = _text(r.text)
        for word in INTERNAL_SHORTHAND:
            assert word not in visible, (path, word)


def test_titles_lead_and_codes_follow(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    body = client.get("/features").text
    assert "Search box that filters notes by title." in body
    title_at = body.index("Search box that filters notes by title.")
    assert body.index("(FEAT-0001)") > title_at


def test_delivery_is_read_from_the_merge_step(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    today = datetime.now(UTC).date()
    window = f"from={today - timedelta(days=1)}&to={today + timedelta(days=1)}"
    body = client.get(f"/delivered?{window}").text
    assert "1 feature merged and checked" in body
    assert "Search box that filters notes by title." in body
    assert "Show a Clear button." not in body          # declined: built, not merged
    assert "Add a done flag." not in body              # on a mission branch that was not merged
    assert "1 built, not merged" in body
    assert "1e581663e8" in body                        # the commit that was checked and published


def test_now_shows_only_what_is_really_in_progress(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    body = client.get("/").text
    in_progress = body[body.index('data-panel="p2"'):body.index('data-panel="p3"')]
    assert "In progress (2)" in in_progress
    assert "Count the notes" in in_progress and "Delete a note." in in_progress
    assert "Search box" not in in_progress and "Show a Clear button." not in in_progress
    needs = body[body.index('data-panel="p4"'):body.index('data-panel="p2"')]
    assert "Waiting for the merge decision" in needs and "Delete a note." in needs


def test_a_feature_page_answers_the_three_questions(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    body = client.get("/features/FEAT-0004").text
    assert "How far through" in body and "On track" in body and "Issues (1)" in body
    assert "Waiting for the merge decision" in body
    assert 'journey-station--current" title="merge decision"' in body
    assert "Thresholds re-derived on" in body


def test_mission_merge_is_named_as_the_missions_merge(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    body = html.unescape(client.get("/").text)
    assert "mission m-1's merge" in body
    features = client.get("/features").text
    assert features.count("(FEAT-000A)") == 1  # one feature, not two


def test_project_names_with_a_slash_open(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    r = client.get("/projects/acme/notes")
    assert r.status_code == 200
    assert "acme/notes" in r.text and "Search box that filters notes by title." in r.text
    assert 'href="/projects/acme/notes"' in client.get("/features").text


def test_request_text_renders_inert(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    body = client.get("/features/FEAT-0003").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_stale_data_banner_when_the_factory_record_cannot_be_read(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, store, _ = world
    assert "banner--stale" not in client.get("/").text
    conn = open_rw(store)
    old = datetime.now(UTC) - timedelta(minutes=10)
    conn.execute("UPDATE mirror_status SET last_success_at=? WHERE source='forge'", (iso(old),))
    body = html.unescape(client.get("/features").text)
    assert "banner--stale" in body
    assert "the dashboard cannot reach the factory's record" in body
    assert "Search box that filters notes by title." in body  # last known data stays
    assert client.get("/healthz").json()["factory_record"] == "stale"


def test_banner_when_the_feed_has_stopped(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, store, _ = world
    conn = open_rw(store)
    write_heartbeat(conn, datetime.now(UTC) - timedelta(minutes=5))
    body = html.unescape(client.get("/issues").text)
    assert "The dashboard's data feed is not running." in body
    assert client.get("/healthz").json()["feed"] == "stopped"


def test_healthz_needs_no_login_and_says_little(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    client.post("/logout")
    assert client.get("/features", follow_redirects=False).status_code == 303  # signed out
    data = client.get("/healthz").json()
    assert set(data) == {"factory_record", "feed", "last_read_at"} and data["factory_record"] == "fresh"


def test_issues_page_lists_open_and_closed(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    body = client.get("/issues").text
    assert "Planning failed" in body and "The planner gave up." in body
    assert "Waiting for the merge decision" in body


def test_unknown_feature_says_so(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    body = client.get("/features/FEAT-NOPE").text
    assert "No feature called FEAT-NOPE" in body


def test_live_update_fragments(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    for panel in ("p2", "p3", "p4", "p5", "p6", "p7", "proj"):
        r = client.get(f"/fragments/{panel}")
        assert r.status_code == 200 and f'data-panel="{panel}"' in r.text, panel


def test_the_delivered_window_survives_a_live_update(world: tuple[TestClient, Path, ForgeFake]) -> None:
    """17 October evaluation: the date window was ignored. The page's live-update refetch dropped it."""
    client, _, _ = world
    body = client.get("/fragments/p7?from=2026-01-01&to=2026-01-31").text
    assert "0 features merged and checked from 2026-01-01 to 2026-01-31." in body


SSE_JS = Path(__file__).resolve().parent.parent / "frontend" / "static" / "sse.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_refetch_address_carries_the_pages_window() -> None:
    """Runs sse.js's own URL builder (under node, no browser) with the page's query in place."""
    script = (
        "global.window = {location: {search: '?from=2026-10-01&to=2026-10-18'}};"
        f"const m = require({json.dumps(str(SSE_JS))});"
        "console.log(JSON.stringify([m.fragmentUrl('p7'), m.fragmentUrl('p2', '')]));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(out.stdout) == ["/fragments/p7?from=2026-10-01&to=2026-10-18", "/fragments/p2"]
    assert "fragmentUrl(panel)" in SSE_JS.read_text(encoding="utf-8")  # the refetch uses it


def test_a_cancelled_feature_is_not_called_on_track(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    body = client.get("/features/FEAT-0002").text
    assert "How it ended" in body and "built, not merged" in body
    row = client.get("/features").text
    declined = row[row.index("Show a Clear button."):]
    declined = declined[:declined.index("</tr>")]
    assert "on track" not in declined and "built, not merged" in declined


def test_the_issues_page_says_how_many_old_failures_are_left_out(
    world: tuple[TestClient, Path, ForgeFake], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, fake = world
    old = forge_fake.T0 - timedelta(days=30)
    monkeypatch.setattr(forge_fake, "T0", old)
    fake.planning("old-1", "An old failure.", state="FAILED", error="x")
    fake.planning("old-2", "Another old failure.", state="FAILED", error="y")
    mirror_once(open_rw(store), fake.path)
    body = client.get("/issues").text
    assert "2 older failures with no later attempt are not listed (opened more than 14 days ago)." in body
    assert "An old failure." not in body
