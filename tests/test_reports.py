"""The weekly report on real data: delivered counts, plan against actual, withheld when stale,
and a client-safe version that shows nothing internal."""

from __future__ import annotations

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
    assert "Cost reporting is being set up." in html
    assert {m.milestone_id for m in export.milestones} == {"M1", "M2", "M3"}
    assert "behind" in html and "ahead" in html
