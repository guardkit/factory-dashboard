"""Every page on real (copied) data: titles first, plain words, delivery read right, stale banner."""

from __future__ import annotations

import html
import json
import os
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
from backend.readmodel import dbread
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
                      "projector", "feed pending", "FEED PENDING", "⟨", "graduation", "M-D2", "Forge")


def _text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def test_every_page_renders_and_speaks_plain_english(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    for path in ("/", "/features", "/features?days=all", "/features/FEAT-0001", "/features/FEAT-0003",
                 "/features/c5", "/projects", "/projects/acme/notes", "/delivered", "/reports", "/reports?view=export",
                 "/issues", "/fleet", "/fragments/freshness"):
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
    assert 'journey-station--waiting" title="merge decision"' in body  # waiting on a person
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
    words = re.sub(r"<[^>]+>", "", body)  # each date sits in a no-wrap span
    assert "0 features merged and checked from 2026-01-01 to 2026-01-31." in words


SSE_JS = Path(__file__).resolve().parent.parent / "frontend" / "static" / "sse.js"
# Set to 1 only where node genuinely cannot be installed; otherwise a missing node fails the test.
ALLOW_NO_NODE = "FACTORY_DASH_ALLOW_NO_NODE"


def _node() -> str:
    found = shutil.which("node") or next(
        (str(p) for p in (Path.home() / ".local" / "bin" / "node",) if p.exists()), None)
    if found:
        return found
    if os.environ.get(ALLOW_NO_NODE) == "1":
        pytest.skip(f"node is not installed and {ALLOW_NO_NODE}=1 allows skipping")
    pytest.fail(f"node is needed to test frontend/static/sse.js; install it or set {ALLOW_NO_NODE}=1")


def test_the_refetch_address_carries_the_pages_window() -> None:
    """Runs sse.js under node with a stub page on /delivered?from=…&to=…: the live-update refetch
    must request the fragment with the page's window (the cause of "the date window is ignored")."""
    script = (
        "const calls = [];"
        "global.window = {location: {search: '?from=2026-10-01&to=2026-10-18'},"
        "  htmx: {ajax: (method, url) => { calls.push([method, url]); return Promise.resolve(); }}};"
        f"const m = require({json.dumps(str(SSE_JS))});"
        "global.document = {querySelector: () => ({classList: {add() {}, remove() {}}})};"
        "m.refetch('p7');"
        "console.log(JSON.stringify({calls, plain: m.fragmentUrl('p2', '')}));"
    )
    out = subprocess.run([_node(), "-e", script], capture_output=True, text=True, check=True, timeout=30)
    result = json.loads(out.stdout)
    assert result["calls"] == [["GET", "/fragments/p7?from=2026-10-01&to=2026-10-18"]]
    assert result["plain"] == "/fragments/p2"


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


# --- Codex round 1, R2: the read time and the stale banner update on an open page ----------------

OPERATOR_PAGES = ("/", "/features", "/features/FEAT-0001", "/features/FEAT-NOPE", "/projects",
                  "/projects/acme/notes", "/issues", "/reports", "/delivered", "/fleet")


def test_every_page_polls_the_freshness_element(world: tuple[TestClient, Path, ForgeFake]) -> None:
    """One element per page, polled every 30 s, whether or not the page has live-update sections."""
    client, _, _ = world
    for path in OPERATOR_PAGES:
        body = client.get(path).text
        assert body.count('id="freshness"') == 1, path
        element = body[body.index('id="freshness"') - 30:body.index('id="freshness"') + 200]
        assert 'hx-get="/fragments/freshness"' in element and 'hx-trigger="every 30s"' in element, path
        assert 'hx-swap="outerHTML"' in element, path
        assert body.count('class="read-asof"') == 1, path


def _freshness(client: TestClient) -> str:
    r = client.get("/fragments/freshness")
    assert r.status_code == 200
    assert r.text.count('id="freshness"') == 1 and 'hx-trigger="every 30s"' in r.text  # it keeps polling
    return html.unescape(r.text)


def test_freshness_healthy_idle_mirror_failing_and_recovery_without_reload(
    world: tuple[TestClient, Path, ForgeFake], tmp_path: Path,
) -> None:
    client, store, fake = world
    page = client.get("/features").text            # the page stays open from here on
    assert "banner--stale" not in page
    conn = open_rw(store)

    # Healthy and idle: a pass that changes nothing records no live update, and still no banner.
    assert mirror_once(conn, fake.path) is False
    fragment = _freshness(client)
    assert "banner--stale" not in fragment and "asof-chip--live" in fragment and 'class="read-asof"' in fragment

    # The feed keeps running but cannot read the factory's record: the open page's poll shows it.
    write_heartbeat(conn, datetime.now(UTC))
    assert mirror_once(conn, tmp_path / "gone.db") is False
    old = datetime.now(UTC) - timedelta(minutes=10)
    conn.execute("UPDATE mirror_status SET last_success_at=? WHERE source='forge'", (iso(old),))
    fragment = _freshness(client)
    assert 'class="banner banner--stale"' in fragment
    assert "the dashboard cannot reach the factory's record" in fragment
    assert "asof-chip--stale" in fragment

    # Recovery: the next good pass, and the next poll drops the banner and shows the new read time.
    mirror_once(conn, fake.path)
    fragment = _freshness(client)
    assert "banner--stale" not in fragment and "asof-chip--live" in fragment


def test_freshness_says_when_the_feed_itself_stopped(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, store, _ = world
    write_heartbeat(open_rw(store), datetime.now(UTC) - timedelta(minutes=5))
    assert "The dashboard's data feed is not running." in _freshness(client)


def test_the_paused_banner_gives_way_to_the_stale_banner() -> None:
    """Runs sse.js under node with a stub banner slot. The paused banner never shows beside the
    stale-data banner, and says what to do in plain words."""
    script = (
        "function slot(names) {"
        "  const kids = [];"
        "  const s = {kids, querySelector(sel) { const c = sel.slice(1);"
        "      return kids.find(k => k.className.split(' ').includes(c)) || null; },"
        "    appendChild(el) { kids.push(el); el.remove = () => kids.splice(kids.indexOf(el), 1); }};"
        "  names.forEach(n => s.appendChild({className: 'banner ' + n}));"
        "  return s;"
        "}"
        "let current = slot([]);"
        "global.window = {location: {search: '', reload() {}}};"
        "global.document = {addEventListener() {}, querySelector: sel => sel === '.banner-slot' ? current : null,"
        "  createElement: () => ({className: '', textContent: '', setAttribute() {}, addEventListener() {}})};"
        f"const m = require({json.dumps(str(SSE_JS))});"
        "const out = {};"
        "m.setPaused(true); m.setPaused(true);"
        "out.alone = current.kids.map(k => [k.className, k.textContent || '']);"
        "m.setPaused(false); out.cleared = current.kids.length;"
        "current = slot(['banner--stale']); m.setPaused(true);"
        "out.withStale = current.kids.map(k => k.className);"
        "current = slot([]); m.setPaused(true); current.appendChild({className: 'banner banner--stale'});"
        "m.setPaused(true); out.staleLater = current.kids.map(k => k.className);"
        "console.log(JSON.stringify(out));"
    )
    out = subprocess.run([_node(), "-e", script], capture_output=True, text=True, check=True, timeout=30)
    result = json.loads(out.stdout)
    text = "Live updates paused. Showing the last data received. Select this message to reload."
    assert result["alone"] == [["banner banner--sse_paused", text]]
    assert result["cleared"] == 0
    assert result["withStale"] == ["banner banner--stale"]
    assert result["staleLater"] == ["banner banner--stale"]
    # And if the stale banner arrives through the poll while the paused one shows, the page hides it.
    css = (SSE_JS.parent / "style.css").read_text(encoding="utf-8")
    rule = next(line for line in css.splitlines() if ".banner--sse_paused { display: none; }" in line)
    assert rule.startswith(".banner-slot:has(.banner--stale")
    assert ':not([data-feed="cost"])' in rule  # a stale cost feed never hides "live updates paused"


# --- Codex round 1, R3: a project page never hides its older unresolved failures ------------------


def test_a_project_whose_only_failure_is_old_says_so(
    world: tuple[TestClient, Path, ForgeFake], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, fake = world
    monkeypatch.setattr(forge_fake, "T0", forge_fake.T0 - timedelta(days=30))
    fake.planning("old-1", "An old failure.", state="FAILED", error="x", repo="acme/old")
    mirror_once(open_rw(store), fake.path)
    body = html.unescape(client.get("/projects/acme/old").text)
    assert '<span class="num">1</span> open issue</li>' in body
    assert "1 older failure with no later attempt is not listed (opened more than 14 days ago)." in body
    assert "No open issues for this project." not in body
    assert "0 open issues" not in _text(body)
    listing = client.get("/projects").text
    row = listing[listing.index('href="/projects/acme/old"'):]
    row = row[:row.index("</tr>")]
    assert re.findall(r'<td class="num">(\d+)</td>', row) == ["1", "0", "0", "1"]


# --- Codex round 1, R4: a failed mission merge check shows on Issues, the project and the report ---


def test_a_failed_mission_merge_shows_everywhere_issues_are_shown(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, store, fake = world
    fake.mission("m-2", "Tag notes", "mc2", status="MERGING")
    fake.planning("mc2-A-a1", "Add tags.")
    fake.build("bT", "FEAT-000T", "mc2-A-a1", mission_id="m-2", mission_role="feature")
    fake.merge_decision("bT", 81)
    fake.merged("bT", 82, mission_branch=True)
    fake.build("bM2", "FEAT-000T", "mc2", queued=150, started=150, completed=151, mission_id="m-2",
               mission_role="mission-merge")
    fake.merge_offer("bM2", 152)
    fake.merge_decision("bM2", 153)
    fake.merge_failed("bM2", 154, "The mission branch moved after its last landing. Kept.")
    before = client.get("/features?days=all").text.count('<tr class="feature-list__row')
    mirror_once(open_rw(store), fake.path)

    issues = html.unescape(client.get("/issues").text)
    open_part = issues[:issues.index("Closed in the last 7 days")]
    assert "Merge check failed" in open_part and "mission m-2's merge (Tag notes)" in open_part
    assert "The mission branch moved after its last landing." in open_part
    project = html.unescape(client.get("/projects/acme/notes").text)
    assert "mission m-2's merge (Tag notes)" in project
    report = html.unescape(client.get("/reports").text)
    assert "Merge check failed: mission m-2's merge (Tag notes)" in report
    # One more feature (FEAT-000T), not two: the mission's merge row is still not a feature.
    assert client.get("/features?days=all").text.count('<tr class="feature-list__row') == before + 1


# --- requests from the redesign session (18 October 2026) ---------------------------------------


def test_the_feed_panel_names_the_factorys_record(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    body = html.unescape(client.get("/fleet").text)
    assert "The factory's record was last read at" in body and "Forge" not in _text(body)


def test_waiting_stations_and_open_waits_are_marked(world: tuple[TestClient, Path, ForgeFake]) -> None:
    _client, store, _ = world
    found = dbread.feature_page(store, "FEAT-0004")       # waiting for the merge decision
    assert found is not None
    _chrome, page = found
    states = {st.name: st.glyph_state for st in page.journey.stations}
    assert states["merge decision"] == "waiting" and page.journey.current_label.startswith("Waiting")
    assert [(w.open, w.ended_label) for w in page.waits] == [(True, "waiting now")]
    found = dbread.feature_page(store, "FEAT-0003")       # building, nobody waited on
    assert found is not None
    assert "waiting" not in {st.glyph_state for st in found[1].journey.stations}
    found = dbread.feature_page(store, "FEAT-0001")       # delivered: every wait closed
    assert found is not None and found[1].waits and not any(w.open for w in found[1].waits)
    assert all(w.ended_label != "waiting now" for w in found[1].waits)


def test_the_projects_page_lists_each_project(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    body = client.get("/projects").text
    assert 'href="/projects/acme/notes"' in body and 'aria-current="page"' in body
    row = body[body.index('href="/projects/acme/notes"'):]
    row = row[:row.index("</tr>")]
    features, in_progress, delivered, open_issues = (int(n) for n in re.findall(r'<td class="num">(\d+)</td>', row))
    assert (features, in_progress, delivered) == (6, 2, 1)
    project = html.unescape(client.get("/projects/acme/notes").text)
    assert f'<span class="num">{open_issues}</span> open issues' in project


# --- Codex round 2, R2: content and its read time refresh together -------------------------------

FRESHNESS_JS = SSE_JS.parent / "freshness.js"


def _version(client: TestClient, path: str = "/fragments/freshness") -> str:
    found = re.search(r'id="freshness" data-version="([^"]+)"', client.get(path).text)
    assert found is not None, path
    return found.group(1)


def test_the_data_version_follows_changes_staleness_and_recovery(
    world: tuple[TestClient, Path, ForgeFake], tmp_path: Path,
) -> None:
    client, store, fake = world
    conn = open_rw(store)
    first = _version(client)
    assert _version(client, "/features") == first          # a page carries the version it was built from

    assert mirror_once(conn, fake.path) is False            # healthy and idle: same version
    assert _version(client) == first

    fake.planning("c9", "Archive a note.", state="RUNNING", end=None)
    assert mirror_once(conn, fake.path) is True             # a record changed: a new version
    changed = _version(client)
    assert changed != first

    write_heartbeat(conn, datetime.now(UTC))
    mirror_once(conn, tmp_path / "gone.db")                 # the record cannot be read
    conn.execute("UPDATE mirror_status SET last_success_at=? WHERE source='forge'",
                 (iso(datetime.now(UTC) - timedelta(minutes=10)),))
    stale = _version(client)
    assert stale != changed and ".stale." in stale

    mirror_once(conn, fake.path)                             # recovery
    recovered = _version(client)
    assert recovered != stale and ".fresh." in recovered


def test_the_page_copy_of_the_freshness_element_is_swapped_with_the_content(
    world: tuple[TestClient, Path, ForgeFake],
) -> None:
    """In a full page it is marked out of band, so a content refresh replaces it in the same swap;
    the polled copy is not, so a poll replaces only itself."""
    client, _, _ = world
    page = client.get("/features").text
    element = page[page.index('id="freshness"'):]
    assert 'hx-swap-oob="true"' in element[:element.index(">")]
    assert "hx-swap-oob" not in client.get("/fragments/freshness").text
    # The chat drawer is outside the refreshed content, so an open chat survives a refresh.
    main = page[page.index('<main id="main"'):page.index("</main>")]
    assert "chat-drawer" not in main and "chat-drawer" in page


def test_the_report_shows_its_figures_again_after_recovery(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, store, fake = world
    conn = open_rw(store)
    conn.execute("UPDATE mirror_status SET last_success_at=? WHERE source='forge'",
                 (iso(datetime.now(UTC) - timedelta(minutes=10)),))
    body = client.get("/reports").text
    assert "Figures withheld" in body and _version(client).count(".stale.") == 1
    write_heartbeat(conn, datetime.now(UTC))
    mirror_once(conn, fake.path)
    body = client.get("/reports").text
    assert "Figures withheld" not in body and 'class="report-headline__line"' in body


def test_a_poll_with_newer_data_refreshes_the_content_instead() -> None:
    """Runs freshness.js under node with a stub page (not a real browser; test_browser.py is)."""
    script = (
        "const calls = [];"
        "let shown = 'v1';"
        "global.window = {location: {href: 'http://x/features?days=30'},"
        "  htmx: {ajax: (method, url, ctx) => { calls.push([method, url, ctx]);"
        "    return new Promise(r => setTimeout(r, 10)); }}};"
        "global.document = {addEventListener() {}, getElementById: id => id === 'freshness'"
        "  ? {getAttribute: () => shown} : id === 'main' ? {} : null};"
        f"const m = require({json.dumps(str(FRESHNESS_JS))});"
        "const poll = v => ({detail: {target: {id: 'freshness'}, shouldSwap: true,"
        "  serverResponse: '<div class=\"freshness\" id=\"freshness\" data-version=\"' + v + '\">'}});"
        "const same = poll('v1'); m.onBeforeSwap(same);"
        "const other = {detail: {target: {id: 'main'}, shouldSwap: true, serverResponse: 'data-version=\"v9\"'}};"
        "m.onBeforeSwap(other);"
        "const newer = poll('v2'); m.onBeforeSwap(newer);"
        "const again = poll('v3'); m.onBeforeSwap(again);"   # while the refresh is still running
        "setTimeout(() => { const later = poll('v4'); m.onBeforeSwap(later);"
        "  console.log(JSON.stringify({same: same.detail.shouldSwap, other: other.detail.shouldSwap,"
        "    newer: newer.detail.shouldSwap, again: again.detail.shouldSwap, later: later.detail.shouldSwap,"
        "    calls})); }, 50);"
    )
    out = subprocess.run([_node(), "-e", script], capture_output=True, text=True, check=True, timeout=30)
    result = json.loads(out.stdout)
    assert result["same"] is True                  # same data: the poll's new read time goes in
    assert result["other"] is True                 # a content refresh is not interfered with
    assert result["newer"] is False and result["again"] is False and result["later"] is False
    target = {"target": "#main", "select": "#main", "swap": "outerHTML"}
    assert result["calls"] == [["GET", "http://x/features?days=30", target]] * 2  # one at a time


# --- Codex round 3, R2: every card showing the factory record's read time is marked as such -------


def test_factory_read_times_are_marked_and_other_sources_are_not(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    body = client.get("/").text
    chips = re.findall(r'<span class="asof-chip[^>]*>', body)
    factory = [c for c in chips if 'data-source="factory"' in c]
    assert len(factory) >= 4                                  # the top time and the factory cards
    services = body[body.index('data-panel="p6"'):]
    services_chip = re.search(r'<span class="asof-chip[^>]*>', services)
    assert services_chip is not None and "data-source" not in services_chip.group(0)  # its own time
    # The freshness element carries the latest successful read, the time the cards are kept at.
    fragment = client.get("/fragments/freshness").text
    read_at = re.search(r'id="freshness"[^>]*data-read-at="([^"]+)"', fragment)
    top = re.search(r'data-source="factory"[^>]*data-asof="([^"]+)"', fragment)
    assert read_at is not None and top is not None and read_at.group(1) == top.group(1)


# --- Codex round 1, R3: the all-projects report has no client-safe document ----------------------


def test_the_operator_export_url_shows_no_document(world: tuple[TestClient, Path, ForgeFake]) -> None:
    client, _, _ = world
    body = client.get("/reports?view=export").text   # no client chosen: the all-projects report
    assert "Choose a client first" in body
    assert 'class="report-export"' not in body and "data-print" not in body
    assert "Search box that filters notes by title." not in body
    assert "view=export" not in client.get("/reports").text
