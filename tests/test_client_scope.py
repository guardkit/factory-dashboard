"""A client report covers exactly that client's projects (redesign Codex review, data exposure).

Only the operator's own view covers every project. A client tenant whose projects are missing,
empty or unreadable covers NO project: its weekly report and its client-safe export show nothing,
and never another client's deliveries or work in progress.
"""

from __future__ import annotations

import html
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from backend import db
from backend.app import create_app
from backend.auth import make_credential_hash
from backend.config_loader import load_tenants
from backend.projector.plan_mirror import mirror_plans_once
from backend.readmodel import dbread
from backend.readmodel.viewmodels import WeeklyReport
from starlette.testclient import TestClient

from tests.conftest import LOOPBACK_URL, account_row, login, write_accounts
from tests.forge_fake import T0, ForgeFake, delivered_feature, store_from

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = T0 + timedelta(hours=3)
WEEK = ("2026-10-12", "2026-10-18")

# Every piece of client work on record, by project: acme/notes (Acme's) and other/app (another client's).
ACME_DELIVERED = ("Search notes by title.", "Welcome screen.")
ACME_IN_PROGRESS = "Clear button."
OTHER_IN_PROGRESS = "Other client's secret work."
OTHER_FAILED = "Other client's failed request."
EVERY_TITLE = (*ACME_DELIVERED, ACME_IN_PROGRESS, OTHER_IN_PROGRESS, OTHER_FAILED)

# Tenants whose projects are set badly. Each must cover nothing.
BAD_SCOPES = {
    "nullco": "",                   # nothing recorded (the column is NOT NULL, so blank)
    "emptyco": "[]",                # an empty list
    "brokenco": "{not json",        # unreadable
    "dictco": '{"acme/notes": 1}',  # not a list
    "blankco": '["", 3]',           # nothing usable in the list
}


def _tenants_yaml(folder: Path) -> Path:
    data = yaml.safe_load((FIXTURES / "tenants_reports.yaml").read_text(encoding="utf-8"))
    data["tenants"] += [
        {"tenant_slug": "otherco", "display_name": "OtherCo", "nats_account": "OTHERCO",
         "subject_prefix": "otherco", "projects": ["other/app"], "store_path": "ledger_client_otherco.db"},
        {"tenant_slug": "emptyco", "display_name": "EmptyCo", "nats_account": "EMPTYCO",
         "subject_prefix": "emptyco", "projects": [], "store_path": "ledger_client_emptyco.db"},
    ]
    path = folder / "tenants_scope.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, sqlite3.Connection, Path]:
    fake = ForgeFake(tmp_path / "forge.db")
    for n, text in enumerate(ACME_DELIVERED, start=1):
        delivered_feature(fake, n, text=text)
    fake.planning("c3", ACME_IN_PROGRESS)
    fake.build("b3", "FEAT-0003", "c3", status="RUNNING", completed=None)
    fake.planning("o1", OTHER_IN_PROGRESS, state="RUNNING", end=None, repo="other/app", start=150)
    fake.planning("o2", OTHER_FAILED, state="FAILED", error="It broke.", repo="other/app", start=160)
    path = tmp_path / "readmodel.db"
    conn = store_from(fake, path, NOW)
    tenants_path = _tenants_yaml(tmp_path)
    db.init_db(path, load_tenants(tenants_path).values())
    mirror_plans_once(conn, FIXTURES / "plans_pack.yaml", NOW)
    for slug, projects_json in BAD_SCOPES.items():
        conn.execute("INSERT INTO tenants (tenant_slug, display_name, projects_json) VALUES (?, ?, ?) "
                     "ON CONFLICT(tenant_slug) DO UPDATE SET projects_json=excluded.projects_json",
                     (slug, slug, projects_json))
    return path, conn, tenants_path


def _titles(report: WeeklyReport) -> set[str]:
    return ({r.title for r in report.delivered_rows} | {r.title for r in report.in_flight_rows}
            | {r.headline for r in report.issues.rows} | {m.title for m in report.plan.milestones})


@pytest.mark.parametrize("tenant", [*BAD_SCOPES, "not-a-tenant"])
def test_a_client_with_no_usable_projects_sees_nothing(world: tuple[Path, sqlite3.Connection, Path],
                                                       tenant: str) -> None:
    _path, conn, _ = world
    report = dbread.weekly_report(conn, tenant, NOW, window=WEEK)
    assert report.delivered_rows == () and report.in_flight_rows == () and report.issues.rows == ()
    assert report.plan.milestones == ()
    h = report.headline
    assert (h.delivered_count, h.in_flight_count, h.behind_count, h.on_target_count, h.ahead_count) == (0, 0, 0, 0, 0)
    assert (report.issues.opened, report.issues.closed) == (0, 0)
    export = dbread.export_report(conn, tenant, NOW, window=WEEK)
    assert export.delivered_rows == () and export.milestones == ()


def test_a_client_sees_only_its_own_project(world: tuple[Path, sqlite3.Connection, Path]) -> None:
    _path, conn, _ = world
    other = dbread.weekly_report(conn, "otherco", NOW, window=WEEK)
    assert {r.title for r in other.in_flight_rows} == {OTHER_IN_PROGRESS}
    assert other.delivered_rows == ()
    assert {r.headline.split(": ", 1)[1] for r in other.issues.rows} == {OTHER_FAILED}
    assert not (_titles(other) & {*ACME_DELIVERED, ACME_IN_PROGRESS})
    acme = dbread.weekly_report(conn, "acme", NOW, window=WEEK)
    assert {r.title for r in acme.delivered_rows} == set(ACME_DELIVERED)
    assert {r.title for r in acme.in_flight_rows} == {ACME_IN_PROGRESS}
    assert not (_titles(acme) & {OTHER_IN_PROGRESS, OTHER_FAILED})
    assert {r.title for r in dbread.export_report(conn, "acme", NOW, window=WEEK).delivered_rows} == \
        set(ACME_DELIVERED)
    assert dbread.export_report(conn, "otherco", NOW, window=WEEK).delivered_rows == ()


def test_the_operator_report_still_covers_every_project(world: tuple[Path, sqlite3.Connection, Path]) -> None:
    _path, conn, _ = world
    report = dbread.weekly_report(conn, "operator", NOW, window=WEEK)
    assert {r.title for r in report.delivered_rows} == set(ACME_DELIVERED)
    assert {r.title for r in report.in_flight_rows} == {ACME_IN_PROGRESS, OTHER_IN_PROGRESS}
    assert report.headline.delivered_count == 2 and report.headline.in_flight_count == 2
    assert OTHER_FAILED in {r.headline.split(": ", 1)[1] for r in report.issues.rows}
    assert {r.title for r in dbread.export_report(conn, "operator", NOW, window=WEEK).delivered_rows} == \
        set(ACME_DELIVERED)


def test_the_pages_for_a_client_with_no_projects_show_nothing(world: tuple[Path, sqlite3.Connection, Path],
                                                              config_dir: Path) -> None:
    path, _conn, tenants_path = world
    write_accounts(config_dir, [account_row(), {"username": "empty-tester", "display_name": "Empty Person",
                                               "password_hash": make_credential_hash("empty-password-not-real"),
                                               "tenant": "emptyco"}])
    app = create_app(db_path=path, tenants_path=tenants_path)
    with TestClient(app, base_url=LOOPBACK_URL) as operator:
        login(operator)
        # The operator's view of EmptyCo's report and its client-safe export: nothing listed.
        for view in ("", "&view=export"):
            body = html.unescape(operator.get(f"/reports?tenant=emptyco&from={WEEK[0]}&to={WEEK[1]}{view}").text)
            assert "EmptyCo" in body
            for title in EVERY_TITLE:
                assert title not in body, (view, title)
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        login(client, "empty-tester", "empty-password-not-real")
        home = html.unescape(client.get("/").text)
        assert "EmptyCo" in home
        for title in EVERY_TITLE:
            assert title not in home, title
        for page in ("/reports?tenant=emptyco&view=export", "/projects/acme/notes", "/features"):
            assert client.get(page, follow_redirects=False).status_code == 303, page
        assert client.post("/chat", data={"question": "What was delivered?"},
                           headers={"HX-Request": "true", "Origin": LOOPBACK_URL}).status_code == 403
