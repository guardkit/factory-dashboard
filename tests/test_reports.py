"""The weekly report on real data: delivered counts, plan against actual, withheld when stale,
and a client-safe version that shows nothing internal."""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from backend import db
from backend.config_loader import load_tenants
from backend.projector.plan_mirror import mirror_plans_once
from backend.projector.store import iso
from backend.readmodel import dbread
from jinja2 import Environment, FileSystemLoader, select_autoescape

from tests.forge_fake import T0, ForgeFake, delivered_feature, store_from

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEMPLATES = Path(__file__).resolve().parent.parent / "frontend" / "templates"
TENANTS = FIXTURES / "tenants_reports.yaml"
PLANS = FIXTURES / "plans_pack.yaml"
NOW = T0 + timedelta(hours=3)
WEEK = ("2026-10-12", "2026-10-18")


@pytest.fixture
def store(tmp_path: Path) -> sqlite3.Connection:
    fake = ForgeFake(tmp_path / "forge.db")
    delivered_feature(fake, 1, text="Search notes by title.")
    delivered_feature(fake, 5, text="Welcome screen.")
    fake.planning("c2", "Clear button.")
    fake.build("b2", "FEAT-0002", "c2", status="RUNNING", completed=None)
    fake.planning("c9", "Secret failing request.", state="FAILED", error="OPERATOR_ONLY_ERROR_TEXT")
    path = tmp_path / "readmodel.db"
    conn = store_from(fake, path, NOW)
    db.init_db(path, load_tenants(TENANTS).values())
    mirror_plans_once(conn, PLANS, NOW)
    return conn


def test_plan_mirror_reflects_and_reconciles_the_file(store: sqlite3.Connection, tmp_path: Path) -> None:
    assert {r[0] for r in store.execute("SELECT milestone_id FROM plan_milestones")} == {"M1", "M2", "M3"}
    smaller = tmp_path / "plans.yaml"
    smaller.write_text("plans:\n  acme:\n    - id: M1\n      title: x\n      feature_ids: [FEAT-0005]\n")
    mirror_plans_once(store, smaller, NOW)
    assert {r[0] for r in store.execute("SELECT milestone_id FROM plan_milestones")} == {"M1"}
    empty = tmp_path / "empty.yaml"
    empty.write_text("plans: {}\n")
    mirror_plans_once(store, empty, NOW)
    assert store.execute("SELECT COUNT(*) FROM plan_milestones").fetchone()[0] == 0


def test_headline_counts_real_deliveries(store: sqlite3.Connection) -> None:
    """17 October evaluation: the report said '0 delivered' while listing 33."""
    report = dbread.weekly_report(store, "operator", NOW, window=WEEK)
    assert report.headline.delivered_count == 2 == len(report.delivered_rows)
    assert report.headline.in_flight_count == 1
    assert not report.headline.withheld
    assert {r.code for r in report.delivered_rows} == {"FEAT-0001", "FEAT-0005"}


def test_plan_against_actual(store: sqlite3.Connection) -> None:
    report = dbread.weekly_report(store, "acme", NOW, window=WEEK)
    by_id = {m.milestone_id: m.deviation for m in report.plan.milestones if m.milestone_id}
    assert by_id == {"M2": "behind", "M3": "on_target", "M1": "ahead"}
    no_plan = [m for m in report.plan.milestones if m.deviation == "no_baseline"]
    assert [m.feature_id for m in no_plan] == ["FEAT-0002"]  # in progress, in no milestone
    assert no_plan[0].band.label == "no plan agreed"         # never green


def test_operator_report_without_a_plan_shows_no_plan_agreed(store: sqlite3.Connection) -> None:
    report = dbread.weekly_report(store, "operator", NOW, window=WEEK)
    assert report.plan.milestones and all(m.deviation == "no_baseline" for m in report.plan.milestones)


def test_stale_record_withholds_counts_and_plan(store: sqlite3.Connection) -> None:
    store.execute("UPDATE mirror_status SET last_success_at=?", (iso(NOW - timedelta(minutes=30)),))
    report = dbread.weekly_report(store, "acme", NOW, window=WEEK)
    assert report.headline.withheld and report.plan.withheld
    assert report.headline.withheld_since


def test_window_echo_default_and_malformed(store: sqlite3.Connection) -> None:
    assert dbread.weekly_report(store, "operator", NOW, window=WEEK).window_from == "2026-10-12"
    default = dbread.weekly_report(store, "operator", NOW)
    assert (default.window_from, default.window_to) == ("2026-10-12", "2026-10-18")
    bad = dbread.weekly_report(store, "operator", NOW, window=("2026-13-40", "x"))
    assert (bad.window_from, bad.window_to) == ("2026-10-12", "2026-10-18")


def test_issues_this_week(store: sqlite3.Connection) -> None:
    issues = dbread.weekly_report(store, "operator", NOW, window=WEEK).issues
    assert issues.opened == 1 and issues.rows[0].kind == "Planning failed"


_DENY_TOKENS = (
    "coach_score", "turns", "agent_id", "evidence", "issue", "failure_reason", "queue_depth",
    "originating_user", "defer_count", "planning_run", "correlation_id", "in_flight", "error",
)


def test_export_template_is_clean_of_internal_fields() -> None:
    text = (TEMPLATES / "reports_export.html").read_text(encoding="utf-8").lower()
    for token in _DENY_TOKENS:
        assert token not in text, token


def test_export_shows_only_deliveries_and_the_four_plan_fields(store: sqlite3.Connection) -> None:
    export = dbread.export_report(store, "acme", NOW, window=WEEK)
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(default=True))
    html = env.get_template("reports_export.html").render(report=export, report_tenant="acme")
    assert "Search notes by title." in html and "Welcome screen." in html
    assert "Clear button." not in html               # work in progress stays internal
    assert "OPERATOR_ONLY_ERROR_TEXT" not in html and "Secret failing request" not in html
    assert "U1" not in html                          # nobody's identity
    # Rich's approved brief: the client report carries no costs (the coordinator's decision of
    # 18 October 2026). No cost section, column or wording at all.
    for word in ("cost", "spend", "£"):
        assert word not in html.lower(), word
    assert {m.milestone_id for m in export.milestones} == {"M1", "M2", "M3"}
    assert "behind" in html and "ahead" in html
    # Each delivered row's pill says "Delivered" in words, not only with its tick.
    pills = re.findall(r'<span class="status-pill status-pill--done"><svg.*?</svg>([^<]*)<', html, re.S)
    assert pills and len(pills) == len(export.delivered_rows) and {p.strip() for p in pills} == {"Delivered"}


class _Operator:
    display_name = "SIGNED_IN_OPERATOR_NAME"
    tenant_slug = "operator"


def test_export_heading_names_the_reports_client_and_week_not_the_signed_in_account(
    store: sqlite3.Connection,
) -> None:
    export = dbread.export_report(store, "acme", NOW, window=WEEK)
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(default=True))
    html = env.get_template("reports_export.html").render(report=export, report_tenant="acme", tenant=_Operator())
    assert "SIGNED_IN_OPERATOR_NAME" not in html
    assert f"Weekly summary for {export.tenant_display}" in html
    words = re.sub(r"<[^>]+>", "", html)
    assert f"Week {export.window_from} to {export.window_to}" in words
    # Each date is one unbreakable piece, and the time is in plain words, not the record's chip.
    assert f'<span class="report-export__date">{export.window_from}</span>' in html
    assert "factory record" not in words and "asof-chip" not in html
    assert "Prepared <time" in html


def test_export_prints_through_the_shared_script_without_an_inline_handler() -> None:
    text = (TEMPLATES / "reports_export.html").read_text(encoding="utf-8")
    assert "data-print" in text and "Save as PDF" in text
    assert "onclick" not in text and "window.print" not in text
    # The page's own controls stay out of the printed document.
    assert '<div class="report-toolbar no-print">' in text and '<p class="hint no-print">' in text


def test_the_weekly_report_shows_spend_as_not_measured_never_a_figure(store: sqlite3.Connection) -> None:
    report = dbread.weekly_report(store, "operator", NOW, window=WEEK)
    assert not report.headline.withheld
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(default=True))
    html = env.get_template("reports.html").render(report=report, report_tenant="operator")
    tile = html[html.index('class="stat-tile stat-tile--empty" data-state="not-measured"'):]
    tile = tile[: tile.index("</div>")]
    assert "Not measured" in tile and "Spend" in tile
    assert "£" not in html
    assert f'<strong class="key-figure">{report.headline.delivered_count} features merged and checked</strong>' in html


def test_issues_have_their_own_card_with_the_closed_ones_behind_a_count(store: sqlite3.Connection) -> None:
    """On real data the week's issues ran to some 70 rows beside the short plan card, leaving a long
    empty column; they now fill their own card, open ones first, closed ones behind "N closed"."""
    report = dbread.weekly_report(store, "operator", NOW, window=WEEK)
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(default=True))
    html = env.get_template("reports.html").render(report=report, report_tenant="operator")
    assert "report-two-col" not in html
    card = html[html.index('<section class="panel-card report-issues">'):]
    card = card[:card.index("</section>")]
    rows = report.issues.rows
    assert rows
    closed = [r for r in rows if not r.is_open]
    if closed:
        before, details = card.split('<details class="report-issues__closed">')
        assert f"<summary>{len(closed)} closed this week</summary>" in details
        assert details.count("report-issues__row--closed") == len(closed)  # every closed row, behind the count
        assert "report-issues__row--closed" not in before                 # and only open rows above it
        assert before.count('<li class="report-issues__row') == len(rows) - len(closed)
    else:
        assert "<details" not in card
    assert card.count('<li class="report-issues__row') == len(rows)


def test_no_spend_tile_writes_its_own_words() -> None:
    """Each tile shows its page's spend slot through one macro, so a measured cost replaces "Not
    measured" everywhere at once. (Now's view carries no spend slot yet; the cost feed adds one.)"""
    for name, slot in (("reports.html", "h.spend"), ("feature.html", "page.spend"), ("project.html", "view.spend")):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert f"m.spend_value({slot})" in source, name
        assert "Not measured" not in source, name
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(default=True))
    macros = env.get_template("macros.html").module
    gap = str(macros.spend_value(dbread.SpendSlot()))
    assert gap.endswith("Not measured") and "#i-slash" in gap

    class Measured:  # the shape the cost feed gives a slot
        value = "$2.20"
        label = ""

    assert str(macros.spend_value(Measured())) == "$2.20"
