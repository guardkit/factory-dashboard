"""Cost on the pages' view models: the join to Forge's record, stages, projects by week, the Now
page, the weekly report, and the honesty rules (not measured, not available, stale), all through
the real feed code over a fake Forge record and a fake spend log."""

from __future__ import annotations

import html as html_lib
import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from backend import db
from backend.app import create_app
from backend.projector.cost_import import import_once
from backend.projector.forge_mirror import mirror_once
from backend.projector.store import write_heartbeat
from backend.readmodel import dbread
from backend.readmodel.norms import Norms, load_norms
from backend.readmodel.viewmodels import SpendSlot
from starlette.testclient import TestClient

from tests import forge_fake
from tests.conftest import LOOPBACK_URL, login
from tests.forge_fake import REPO, ForgeFake, delivered_feature, store_from
from tests.spend_fake import IN_PRICE, OUT_PRICE, FakeSpend
from tests.test_pages import INTERNAL_SHORTHAND

T = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)    # a Sunday; tagging started on the Saturday
NOW = T + timedelta(minutes=200)
TENANTS_TEST = Path(__file__).resolve().parent / "fixtures" / "tenants_test.yaml"
OLD_PROJECT = "acme/old"       # a project whose only work was before calls were tagged


def _cost(prompt: int, completion: int) -> float:
    return prompt * IN_PRICE + completion * OUT_PRICE


@pytest.fixture
def factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ForgeFake:
    """One delivered feature (c-1), a mission part on its mission's branch (mc-A-a1), and a feature
    built two days earlier, before calls were tagged."""
    monkeypatch.setattr(forge_fake, "T0", T)
    fake = ForgeFake(tmp_path / "forge.db")
    delivered_feature(fake, 1, text="Search box that filters notes by title.")
    fake.mission("m-1", "Mark notes as done", "mc")
    fake.planning("mc-A-a1", "Add a done flag.", start=100, end=110)
    fake.build("bA", "FEAT-000A", "mc-A-a1", queued=110, started=110, completed=150, mission_id="m-1",
               mission_role="feature")
    fake.merge_decision("bA", 150)
    fake.merged("bA", 152, mission_branch=True)
    fake.planning("old", "An old feature.", start=-48 * 60, end=-48 * 60 + 20)
    fake.build("bOld", "FEAT-0OLD", "old", queued=-48 * 60 + 21, started=-48 * 60 + 21, completed=-48 * 60 + 80)
    fake.planning("other-old", "Another project's old request.", start=-48 * 60, end=-48 * 60 + 20,
                  repo=OLD_PROJECT)
    return fake


def _calls(t0: datetime) -> FakeSpend:
    spend = FakeSpend(t0)
    spend.add("p1", 5, "c-1", prompt=10_000, completion=1_000, model_name="product-owner-agent")   # planning
    spend.add("p2", 10, "c-1", prompt=2_000, completion=200, spend=0.0, model_name="architect-agent")  # estimated
    spend.add("b1", 30, "c-1", prompt=100_000, completion=5_000)                                  # building
    spend.add("b2", 60, "c-1", prompt=50_000, completion=2_000)
    spend.add("r1", 85, "c-1", prompt=500, completion=50)                                         # merge and check
    spend.add("m1", 105, "mc-A-a1", prompt=3_000, completion=300)
    spend.add("m2", 120, "mc-A-a1", prompt=40_000, completion=4_000)
    spend.add("u1", 30, None, prompt=700, completion=70, model_name="tutor-coach")                # untagged
    return spend


C1 = sum(_cost(p, c) for p, c in ((10_000, 1_000), (2_000, 200), (100_000, 5_000), (50_000, 2_000), (500, 50)))
MA = _cost(3_000, 300) + _cost(40_000, 4_000)
U1 = _cost(700, 70)


@pytest.fixture
def store(tmp_path: Path, factory: ForgeFake) -> Path:
    path = tmp_path / "readmodel.db"
    conn = store_from(factory, path, NOW)
    import_once(conn, _calls(T), NOW)
    conn.close()
    return path


# --- per feature -------------------------------------------------------------------------------------


def test_a_features_calls_are_joined_by_its_correlation_ids(store: Path) -> None:
    _chrome, page = dbread.feature_page(store, "FEAT-0001", NOW)  # type: ignore[misc]
    s = page.spend
    assert s.state == "measured" and s.measured and not s.empty
    assert s.calls == 5
    assert s.tokens_in == 162_500 and s.tokens_out == 8_250
    assert s.amount_usd == pytest.approx(C1)
    assert s.value == f"${C1:.2f}"
    assert s.estimated and s.estimated_calls == 1      # p2 had no spend and was priced from its tokens
    assert s.price_label == "Nominal: OpenRouter price for qwen/qwen3.8-flash on 17 Oct 2026"
    assert not s.stale and s.text.endswith("nominal (part estimated)")
    assert s.detail == "5 model calls, 162 k tokens in, 8 k tokens out"


def test_cost_is_split_by_stage_from_the_features_own_times(store: Path) -> None:
    _chrome, page = dbread.feature_page(store, "FEAT-0001", NOW)  # type: ignore[misc]
    by = {r.key: r for r in page.cost_by_stage}
    assert [r.key for r in page.cost_by_stage] == ["planning", "build", "review"]
    assert by["planning"].spend.calls == 2 and by["build"].spend.calls == 2 and by["review"].spend.calls == 1
    assert by["build"].spend.amount_usd == pytest.approx(_cost(100_000, 5_000) + _cost(50_000, 2_000))
    assert [r.seg for r in page.cost_by_stage] == ["plan", "build", "review"]
    assert sum(r.share for r in page.cost_by_stage) == pytest.approx(1.0)


def test_a_mission_part_is_joined_by_its_part_tag(store: Path) -> None:
    _chrome, page = dbread.feature_page(store, "FEAT-000A", NOW)  # type: ignore[misc]
    assert page.spend.calls == 2 and page.spend.amount_usd == pytest.approx(MA)
    assert page.time.so_far and any(s.key == "mission_merge" and s.open for s in page.time.segments)
    merge_wait = next(s for s in page.time.segments if s.key == "mission_merge")
    assert merge_wait.seconds == 48 * 60            # joined the branch at 152, now is 200 (M9)
    assert page.time.waiting_secs == 48 * 60 and page.time.machine_secs == (10 + 40 + 2) * 60


def test_a_feature_from_before_tagging_is_not_measured_never_zero(store: Path) -> None:
    _chrome, page = dbread.feature_page(store, "FEAT-0OLD", NOW)  # type: ignore[misc]
    s = page.spend
    assert s.state == "not_measured" and s.empty and s.amount_usd is None and s.calls is None
    assert s.text == "Not measured"
    assert s.reason == "Calls before 17 October were not tagged by feature."
    assert page.cost_by_stage == ()


def test_feature_lists_carry_the_cost_and_time(store: Path) -> None:
    view = dbread.features_view(store, days=None, now=NOW)
    rows = {r.code: r for r in view.features.rows}
    assert rows["FEAT-0001"].spend.amount_usd == pytest.approx(C1)
    assert rows["FEAT-0OLD"].spend.state == "not_measured"
    assert rows["FEAT-0001"].time.measured and rows["FEAT-0001"].time.waiting_secs == pytest.approx(120)


# --- per project ------------------------------------------------------------------------------------


def test_project_totals_count_tagged_calls_only(store: Path) -> None:
    view = dbread.project_view(store, REPO, NOW)
    assert view.spend.calls == 7                        # c-1 and the mission part; not the tutor call
    assert view.spend.amount_usd == pytest.approx(C1 + MA)
    assert "Counts calls from 17 October" in view.spend.reason
    assert view.spend_week.calls == 7
    assert view.untagged_note == ("This week 1 of the factory's 8 model calls was not tagged to a feature, so it "
                                  "counts for no project.")
    assert view.time.measured and view.time.machine_secs > 0


def test_project_weeks_newest_first_and_before_tagging_is_not_measured(store: Path) -> None:
    weeks = dbread.project_view(store, REPO, NOW).weeks
    assert weeks[0].week_from == "2026-10-12" and weeks[0].week_label == "12 Oct"
    assert weeks[0].spend.calls == 7 and weeks[0].delivered == 1 and weeks[0].sent == 3
    assert weeks[0].untagged is not None and weeks[0].untagged.calls == 1
    assert weeks[0].untagged.amount_usd == pytest.approx(U1)
    older = weeks[1]
    assert older.week_from == "2026-10-05"
    assert older.spend.state == "not_measured" and older.spend.empty   # a gap, not $0.00


def test_the_projects_list_shows_each_projects_cost(store: Path) -> None:
    rows = {r.project: r for r in dbread.projects_view(store, NOW).rows}
    assert rows[REPO].spend.amount_usd == pytest.approx(C1 + MA)
    assert rows[REPO].time.measured


# --- Now and the weekly report ------------------------------------------------------------------------


def test_now_shows_spend_today_and_this_week_for_all_calls(store: Path) -> None:
    home = dbread.home_view(store, NOW)
    assert home.spend_today.calls == 8 and home.spend_today.amount_usd == pytest.approx(C1 + MA + U1)
    assert home.spend_week.calls == 8
    assert home.spend_today.scope == "factory"


def test_the_weekly_report_has_real_spend_and_time(store: Path) -> None:
    conn = db.connect_ro(store)
    try:
        report = dbread.weekly_report(conn, "operator", NOW, window=("2026-10-12", "2026-10-18"))
    finally:
        conn.close()
    h = report.headline
    assert h.spend.calls == 7 and h.spend.amount_usd == pytest.approx(C1 + MA)
    assert h.factory_usage is not None and h.factory_usage.calls == 8   # the operator's whole-factory report
    assert h.waiting.measured and h.waiting.waiting_secs == pytest.approx(120)  # the delivered feature's 2 min
    assert report.delivered_rows[0].spend.amount_usd == pytest.approx(C1)
    assert report.delivered_rows[0].time.elapsed == "1 h 30 min"


def test_the_client_export_carries_no_cost(store: Path) -> None:
    conn = db.connect_ro(store)
    try:
        export = dbread.export_report(conn, "operator", NOW, window=("2026-10-12", "2026-10-18"))
    finally:
        conn.close()
    assert not hasattr(export, "spend")
    assert all(not hasattr(r, "spend") for r in export.delivered_rows)
    app = create_app(db_path=store, tenants_path=TENANTS_TEST)
    html = app.state.templates.env.get_template("reports_export.html").render(report=export, report_tenant="operator")
    assert "$" not in html and "Cost" not in html and "nominal" not in html.lower()


# --- honesty: not available, stale, and the two outages apart -------------------------------------------


def test_before_any_import_every_cost_is_not_available(tmp_path: Path, factory: ForgeFake) -> None:
    path = tmp_path / "readmodel.db"
    store_from(factory, path, NOW).close()
    _chrome, page = dbread.feature_page(path, "FEAT-0001", NOW)  # type: ignore[misc]
    for slot in (page.spend, dbread.home_view(path, NOW).spend_today, dbread.project_view(path, REPO, NOW).spend):
        assert slot.state == "not_available" and slot.text == "Cost data not available"
        assert slot.empty and slot.amount_usd is None
    chrome = dbread.chrome_view(path, NOW)
    assert chrome.banner is None                       # the factory's record is fine
    assert chrome.cost_banner is None                  # the figures themselves say "not available"
    assert dbread.health(path, NOW)["cost_feed"] == "never read"


def test_cost_data_older_than_ten_minutes_is_labelled_while_forge_is_fresh(tmp_path: Path, factory: ForgeFake) -> None:
    path = tmp_path / "readmodel.db"
    conn = store_from(factory, path, NOW)
    import_once(conn, _calls(T), NOW - timedelta(minutes=11))
    conn.close()
    _chrome, page = dbread.feature_page(path, "FEAT-0001", NOW)  # type: ignore[misc]
    assert page.spend.measured and page.spend.stale
    assert page.spend.stale_text == "cost data last read at 13:09:00 — may be incomplete"
    chrome = dbread.chrome_view(path, NOW)
    assert chrome.banner is None
    assert chrome.cost_banner is not None and "Cost data last read at 13:09:00" in chrome.cost_banner.text
    assert dbread.health(path, NOW)["cost_feed"] == "stale"


def test_under_ten_minutes_is_not_stale(tmp_path: Path, factory: ForgeFake) -> None:
    path = tmp_path / "readmodel.db"
    conn = store_from(factory, path, NOW)
    import_once(conn, _calls(T), NOW - timedelta(minutes=9))
    conn.close()
    _chrome, page = dbread.feature_page(path, "FEAT-0001", NOW)  # type: ignore[misc]
    assert not page.spend.stale and dbread.chrome_view(path, NOW).cost_banner is None


def test_a_forge_outage_leaves_cost_fresh(tmp_path: Path, factory: ForgeFake) -> None:
    path = tmp_path / "readmodel.db"
    db.apply_schema(path)
    conn = sqlite3.connect(path, isolation_level=None)
    mirror_once(conn, factory.path, NOW - timedelta(minutes=5))   # the record was last read 5 min ago
    write_heartbeat(conn, NOW)
    import_once(conn, _calls(T), NOW)
    conn.close()
    chrome = dbread.chrome_view(path, NOW)
    assert chrome.banner is not None and chrome.banner.kind == "stale"
    assert chrome.cost_banner is None
    health = dbread.health(path, NOW)
    assert (health["factory_record"], health["cost_feed"]) == ("stale", "fresh")


def test_a_cost_outage_changes_the_page_version_so_open_pages_refresh(tmp_path: Path, factory: ForgeFake) -> None:
    path = tmp_path / "readmodel.db"
    conn = store_from(factory, path, NOW)
    import_once(conn, _calls(T), NOW)
    fresh = dbread.chrome_view(path, NOW).version
    stale = dbread.chrome_view(path, NOW + timedelta(minutes=11)).version
    conn.close()
    assert fresh.split(".")[2] == "fresh" and stale.split(".")[2] == "stale"


# --- the chat's read functions (part D will call them) ----------------------------------------------------


def test_cost_and_time_summaries_for_the_chat(store: Path) -> None:
    c = dbread.cost_summary(store, feature="FEAT-0001", now=NOW)
    assert (c.state, c.calls, c.refs) == ("measured", 5, ("FEAT-0001",))
    assert c.cost_usd == pytest.approx(C1) and c.estimated
    by_title = dbread.cost_summary(store, feature="search box", now=NOW)
    assert by_title.refs == ("FEAT-0001",)
    p = dbread.cost_summary(store, project=REPO, now=NOW)
    assert p.calls == 7 and p.cost_usd == pytest.approx(C1 + MA)
    old = dbread.cost_summary(store, feature="FEAT-0OLD", now=NOW)
    assert old.state == "not_measured" and old.cost_usd is None
    t = dbread.time_summary(store, feature="FEAT-0001", now=NOW)
    assert (t.elapsed_secs, t.machine_secs, t.waiting_secs) == (5400, 5160, 120)
    assert dict(t.waiting_parts) == {"Waiting for approvals": 60, "Waiting for the merge decision": 60}


# --- light page checks: the values reach the pages, in plain words, and a gap is never a zero ----------


@pytest.fixture
def live_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path]]:
    now = datetime.now(UTC)
    t0 = now - timedelta(hours=3)
    monkeypatch.setattr(forge_fake, "T0", t0)
    fake = ForgeFake(tmp_path / "forge.db")
    delivered_feature(fake, 1, text="Search box that filters notes by title.")
    fake.planning("old", "An old feature.", start=-60 * 24 * 7, end=-60 * 24 * 7 + 20)   # a week ago: untagged
    path = tmp_path / "readmodel.db"
    conn = store_from(fake, path, now)
    calls = _calls(t0)
    calls.add("no-price", 40, None, prompt=800, completion=80, provider_model="openai/coach-ft-v4")
    import_once(conn, calls, now)
    conn.close()
    app = create_app(db_path=path, tenants_path=TENANTS_TEST)
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        login(client)
        yield client, path


def test_pages_show_the_figures_and_never_a_zero_for_a_gap(live_pages: tuple[TestClient, Path]) -> None:
    client, path = live_pages
    feature = client.get("/features/FEAT-0001").text
    assert f"${C1:.2f}" in feature and "nominal" in feature and "part estimated" in feature
    assert "5 model calls" in feature and "OpenRouter price for qwen/qwen3.8-flash on 17 Oct 2026" in feature
    assert "machine 1 h 26 min, waiting 2 min" in feature
    old = client.get("/features/old").text
    assert 'data-state="not-measured"' in old and "Not measured" in old and "$0" not in old
    home = client.get("/").text
    today = dbread.home_view(path).spend_today
    assert "Today, all projects" in home and "This week, all projects" in home and today.value in home
    project = client.get(f"/projects/{REPO}").text
    assert "Spend to date" in project and f"${C1:.2f}" in project   # only this project's features
    assert f"${C1:.2f}" in client.get("/projects").text
    report = client.get("/reports").text
    assert '<span class="stat-tile__label">Spend</span>' in report and "All model calls:" in report
    for url in ("/", "/features", f"/projects/{REPO}", "/projects", "/features/FEAT-0001", "/features/old",
                "/reports", "/fragments/freshness"):
        visible = re.sub(r"<[^>]+>", " ", client.get(url).text)
        for word in (*INTERNAL_SHORTHAND, "x-feature-id", "correlation", "cost_basis", "litellm_spend"):
            assert word not in visible, (url, word)


# --- coach round 1 -------------------------------------------------------------------------------------


def test_a_project_with_nothing_since_tagging_is_not_measured(store: Path) -> None:
    view = dbread.project_view(store, OLD_PROJECT, NOW)
    for slot in (view.spend, view.spend_week):
        assert slot.state == "not_measured" and slot.empty
        assert slot.text == "Not measured" and slot.reason == "Calls before 17 October were not tagged by feature."
    assert all(w.spend.state == "not_measured" for w in view.weeks)
    row = {r.project: r for r in dbread.projects_view(store, NOW).rows}[OLD_PROJECT]
    assert row.spend.state == "not_measured" and row.spend.label != "No model calls recorded"


def test_a_window_that_ended_before_tagging_is_not_measured(store: Path) -> None:
    conn = db.connect_ro(store)
    try:
        report = dbread.weekly_report(conn, "operator", NOW, window=("2026-10-05", "2026-10-11"))
    finally:
        conn.close()
    assert report.headline.spend.state == "not_measured"
    assert report.headline.spend.reason == "Calls before 17 October were not tagged by feature."


def test_both_feeds_stale_show_both_banners(tmp_path: Path, factory: ForgeFake) -> None:
    """M10: the factory's record and the spend log are each stale, and each says so."""
    path = tmp_path / "readmodel.db"
    db.apply_schema(path)
    conn = sqlite3.connect(path, isolation_level=None)
    mirror_once(conn, factory.path, NOW - timedelta(minutes=5))
    write_heartbeat(conn, NOW)
    import_once(conn, _calls(T), NOW - timedelta(minutes=11))
    conn.close()
    chrome = dbread.chrome_view(path, NOW)
    assert chrome.banner is not None and chrome.cost_banner is not None
    app = create_app(db_path=path, tenants_path=TENANTS_TEST)
    html = app.state.templates.env.get_template("fragments/freshness.html").render(chrome=chrome, standalone=True)
    assert html.count('class="banner banner--stale"') == 2
    assert "the dashboard cannot reach the factory's record" in html_lib.unescape(html)
    assert 'data-feed="cost"' in html and "Cost data last read at 13:09:00" in html


def test_the_cost_figures_come_from_the_pages_own_snapshot(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M12: a cost import commits in the middle of a page (after Forge's record is read, before the
    cost book is). The page still shows the figures of the moment it started reading."""
    real_load_norms = load_norms

    def feature_spend() -> SpendSlot:
        found = dbread.feature_page(store, "FEAT-0001", NOW)
        assert found is not None
        return found[1].spend

    views: tuple[Callable[[], SpendSlot], ...] = (
        feature_spend,
        lambda: dbread.project_view(store, REPO, NOW).spend,
        lambda: dbread.home_view(store, NOW).spend_today,
    )
    for n, build_view in enumerate(views):
        before = build_view().calls
        assert before is not None
        late = FakeSpend(T)
        late.add(f"late-{n}", 70, "c-1", prompt=9_000, completion=900)   # inside the 3-hour re-read
        passes: list[int] = []

        def norms_with_an_import_committing_now(late: FakeSpend = late, passes: list[int] = passes) -> Norms:
            if not passes:
                writer = sqlite3.connect(store, isolation_level=None)
                try:
                    passes.append(import_once(writer, late, NOW).inserted)
                finally:
                    writer.close()
            return real_load_norms()

        monkeypatch.setattr(dbread, "load_norms", norms_with_an_import_committing_now)
        during = build_view().calls
        monkeypatch.setattr(dbread, "load_norms", real_load_norms)
        after = build_view().calls
        assert passes == [1]                        # the import did commit a new call mid-view
        assert during == before                     # but not into this page's read
        assert after == before + 1


def test_each_figures_reason_is_shown_where_the_figure_is(live_pages: tuple[TestClient, Path]) -> None:
    """M8: calls left out for want of a price, and "counts calls from 17 October", appear beside the
    figure on the Now, project and report pages, not only on the feature page."""
    client, _path = live_pages
    left_out = "1 of these calls was on a model with no price, so it is not counted."
    assert left_out in client.get("/").text
    counts = "Counts calls from 17 October, when calls began to be tagged by feature."
    assert counts in client.get(f"/projects/{REPO}").text
    today = datetime.now(UTC).date().isoformat()
    report = client.get(f"/reports?from=2026-10-12&to={today}").text
    assert counts in report and left_out in report     # the report's "all model calls" figure
    # In a table cell the reason goes into the tooltip, not the cell.
    projects = client.get("/projects").text
    assert f'title="Nominal: OpenRouter price for qwen/qwen3.8-flash on 17 Oct 2026 {counts}"' in projects


# --- merge with main: the chat's snapshot, and the client scope ------------------------------------------


def test_the_chat_summaries_read_on_the_answers_snapshot(store: Path) -> None:
    conn = dbread.snapshot(store)
    try:
        c = dbread.cost_summary(store, feature="FEAT-0001", now=NOW, conn=conn)
        t = dbread.time_summary(store, feature="FEAT-0001", now=NOW, conn=conn)
        conn.execute("SELECT 1").fetchone()          # still open: a view given conn never closes it
    finally:
        conn.close()
    assert c.calls == 5 and c.cost_usd == pytest.approx(C1) and t.elapsed_secs == 5400


def test_a_clients_report_counts_only_its_own_projects(store: Path) -> None:
    writer = sqlite3.connect(store)
    writer.executemany(
        "INSERT INTO tenants (tenant_slug, display_name, projects_json) VALUES (?, ?, ?)",
        [("acmeco", "Acme", f'["{REPO}"]'), ("otherco", "Other", '["other/app"]'), ("emptyco", "Empty", "[]")])
    writer.commit()
    writer.close()
    conn = db.connect_ro(store)
    try:
        week = ("2026-10-12", "2026-10-18")
        own = dbread.weekly_report(conn, "acmeco", NOW, window=week).headline.spend
        other = dbread.weekly_report(conn, "otherco", NOW, window=week).headline.spend
        empty = dbread.weekly_report(conn, "emptyco", NOW, window=week).headline.spend
    finally:
        conn.close()
    assert own.calls == 7 and own.amount_usd == pytest.approx(C1 + MA)
    assert other.state == "not_measured" and other.amount_usd is None and other.reason == "No work on record for this."
    assert empty.state == "not_measured" and empty.reason == "This covers no projects."   # none, never every project
