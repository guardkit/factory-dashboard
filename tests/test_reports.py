"""S4 (weekly delivery report, ux §4.7): the `weekly_report(tenant, window)` composition, the
plans.yaml→plan_milestones projector mirror, the four plan-vs-actual states (BEHIND / ON TARGET /
AHEAD / NO BASELINE), the stale-projection WITHHOLDING case, issues-window boundaries, and the
client-safe export firewall (zero operator-only fields; only the four sanctioned milestone fields).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from backend.app import create_app
from backend.config_loader import load_tenants
from backend.db import init_db
from backend.projector.consumers import open_rw, write_heartbeat
from backend.projector.plan_mirror import mirror_plans_once
from backend.projector.projections.base import iso
from backend.readmodel import dbread
from starlette.testclient import TestClient

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TENANTS_REPORTS = FIXTURES / "tenants_reports.yaml"
PLANS_PACK = FIXTURES / "plans_pack.yaml"

NOW = datetime(2026, 7, 11, 14, 13, 34, tzinfo=UTC)
WINDOW = ("2026-07-06", "2026-07-12")  # the report week (Mon..Sun), ref date = 2026-07-12


# --- DB setup helpers -------------------------------------------------------


def _projected_db(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    """A read model built with the reports tenant registry (finproxy owns a project set)."""
    db_path = tmp_path / "readmodel.db"
    init_db(db_path, load_tenants(TENANTS_REPORTS).values())
    return db_path, open_rw(db_path)


def _fresh_projection(dash: sqlite3.Connection, when: datetime = NOW) -> None:
    """A live projector heartbeat + a fresh build/ledger feeder watermark, so the plan verdict is
    NOT withheld (the §4.7 default). Direct-call tests anchor at NOW (deterministic); route-driven
    tests (which compute `now` from the wall clock) anchor at real now so freshness holds."""
    write_heartbeat(dash, when)
    dash.execute(
        "INSERT INTO consumer_watermarks (stream, consumer, last_stream_seq, last_event_at, updated_at) "
        "VALUES ('PIPELINE','dashboard_ro',1,?,?) "
        "ON CONFLICT(stream, consumer) DO UPDATE SET last_event_at=excluded.last_event_at, "
        "updated_at=excluded.updated_at",
        (iso(when), iso(when)),
    )


def _ledger(dash: sqlite3.Connection, feature_id: str, delivered_at: str, *, project: str,
            title: str, merge_sha: str = "abc1234") -> None:
    dash.execute(
        "INSERT INTO ledger (feature_id, project, tenant, title, bar, delivered_at, pr_url, "
        "merge_sha, repo, branch, evidence_ref) VALUES (?,?,?,?, 'merged_pr', ?, NULL, ?, ?, 'main', ?)",
        (feature_id, project, "finproxy", title, delivered_at, merge_sha, project,
         f"forge_sqlite:{feature_id}@{delivered_at}"),
    )


def _inflight(dash: sqlite3.Connection, feature_id: str, *, project: str, title: str) -> None:
    dash.execute(
        "INSERT INTO builds (build_id, feature_id, project, status, started_at, current_wave, "
        "wave_total, progress_pct, title, source) VALUES (?,?,?, 'RUNNING', ?, 2, 6, 40, ?, 'bus')",
        (f"build-{feature_id}", feature_id, project, iso(NOW), title),
    )


def _seed_plan_pack(dash: sqlite3.Connection, when: datetime = NOW) -> None:
    """Realize the plan_pack states in the read model: M2 BEHIND (1/3 delivered), M3 ON TARGET
    (in flight, target next week), M1 AHEAD (delivered before target), + FEAT-NOBASE NO BASELINE.
    `when` anchors the projection freshness (NOW for direct calls; real now for route tests)."""
    _fresh_projection(dash, when)
    mirror_plans_once(dash, PLANS_PACK, NOW)
    # M2: only FEAT-9A21 of its three features is delivered (this week).
    _ledger(dash, "FEAT-9A21", "2026-07-08T10:00:00+00:00", project="finproxy-app", title="Payments webhooks")
    # M1: FEAT-DONE delivered 07-06, BEFORE its w/c 07-13 target → AHEAD.
    _ledger(dash, "FEAT-DONE", "2026-07-06T10:00:00+00:00", project="finproxy-app", title="Onboarding flow")
    # M3: a build in flight in the reporting project (target next week → ON TARGET).
    _inflight(dash, "FEAT-RPT", project="finproxy-reporting", title="Reporting dashboards")
    # NO BASELINE: in-flight work in a tenant project, tracked by no milestone.
    _inflight(dash, "FEAT-NOBASE", project="finproxy-app", title="Unplanned spike")


def _bands(report: object) -> dict[str, str]:
    """Map each milestone_id (or 'NO_BASELINE') → its band label for compact assertions."""
    out: dict[str, str] = {}
    for ms in report.plan.milestones:  # type: ignore[attr-defined]
        key = ms.milestone_id or "NO_BASELINE"
        out[key] = ms.band.label
    return out


# --- the plans.yaml → plan_milestones mirror (projector-owned) ---------------


def test_plan_mirror_reflects_plans_yaml(tmp_path: Path) -> None:
    """The projector mirror upserts every configured milestone with its fields (feature_ids as JSON
    or a project scope); deviation state is NEVER stored (computed by the query layer)."""
    _db, dash = _projected_db(tmp_path)
    changed = mirror_plans_once(dash, PLANS_PACK, NOW)
    assert changed is True
    rows = dash.execute(
        "SELECT milestone_id, tenant, title, feature_ids_json, project, target_window "
        "FROM plan_milestones ORDER BY milestone_id"
    ).fetchall()
    ids = {r[0] for r in rows}
    assert ids == {"M1", "M2", "M3"}
    m2 = next(r for r in rows if r[0] == "M2")
    assert m2[1] == "finproxy" and '"FEAT-9A21"' in m2[3] and m2[5] == "w/c 2026-07-06"
    m3 = next(r for r in rows if r[0] == "M3")
    assert m3[4] == "finproxy-reporting"  # a project-scoped milestone
    # No deviation/verdict column exists — verdicts are query-layer only.
    cols = {c[1] for c in dash.execute("PRAGMA table_info(plan_milestones)").fetchall()}
    assert "deviation" not in cols and "band" not in cols


def test_plan_mirror_removes_dropped_milestones(tmp_path: Path) -> None:
    """A full reconcile: a milestone removed from plans.yaml is DELETED from the read model on the
    next pass (not an append-only mirror)."""
    _db, dash = _projected_db(tmp_path)
    mirror_plans_once(dash, PLANS_PACK, NOW)
    smaller = tmp_path / "plans_small.yaml"
    smaller.write_text(
        'plans:\n  finproxy:\n    - id: M2\n      title: "Payments API"\n'
        '      feature_ids: [FEAT-9A21]\n      target_window: "w/c 2026-07-06"\n',
        encoding="utf-8",
    )
    mirror_plans_once(dash, smaller, NOW)
    ids = {r[0] for r in dash.execute("SELECT milestone_id FROM plan_milestones").fetchall()}
    assert ids == {"M2"}  # M1 + M3 removed


def test_plan_mirror_empty_file_clears_table(tmp_path: Path) -> None:
    _db, dash = _projected_db(tmp_path)
    mirror_plans_once(dash, PLANS_PACK, NOW)
    empty = tmp_path / "plans_empty.yaml"
    empty.write_text("plans: {}\n", encoding="utf-8")
    mirror_plans_once(dash, empty, NOW)
    assert dash.execute("SELECT COUNT(*) FROM plan_milestones").fetchone()[0] == 0


# --- the four plan-vs-actual states -----------------------------------------


def test_weekly_report_all_four_plan_states(tmp_path: Path) -> None:
    """plan_pack drives all four states: M2 BEHIND, M3 ON TARGET, M1 AHEAD, FEAT-NOBASE NO BASELINE."""
    _db, dash = _projected_db(tmp_path)
    _seed_plan_pack(dash)
    report = dbread.weekly_report(dash, "finproxy", NOW, window=WINDOW)

    bands = _bands(report)
    assert bands["M2"] == "BEHIND"
    assert bands["M3"] == "ON TARGET"
    assert bands["M1"] == "AHEAD"
    assert bands["NO_BASELINE"] == "NO BASELINE"

    # AHEAD renders [G]; BEHIND renders [R]; NO BASELINE renders [—] (a named gap, never green).
    by_id = {ms.milestone_id or "NO_BASELINE": ms for ms in report.plan.milestones}
    assert by_id["M1"].band.band == "G" and by_id["M1"].deviation == "ahead"
    assert by_id["M2"].band.band == "R" and by_id["M2"].deviation == "behind"
    assert by_id["M3"].band.band == "G" and by_id["M3"].deviation == "on_target"
    assert by_id["NO_BASELINE"].band.band == "—" and by_id["NO_BASELINE"].deviation == "no_baseline"

    # Headline tallies (not withheld — the projection is fresh).
    assert report.headline.withheld is False
    assert report.headline.behind_count == 1
    assert report.headline.on_target_count == 1
    assert report.headline.ahead_count == 1
    assert report.headline.delivered_count == 2   # FEAT-9A21 + FEAT-DONE, in-window
    assert report.headline.in_flight_count == 2   # FEAT-RPT + FEAT-NOBASE


def test_no_baseline_is_a_named_gap_not_fake_on_target(tmp_path: Path) -> None:
    """Untracked active work renders `[—] NO BASELINE` — never omitted, never counted on-target."""
    _db, dash = _projected_db(tmp_path)
    _seed_plan_pack(dash)
    report = dbread.weekly_report(dash, "finproxy", NOW, window=WINDOW)
    nobase = [ms for ms in report.plan.milestones if ms.deviation == "no_baseline"]
    assert len(nobase) == 1
    row = nobase[0]
    assert row.feature_id == "FEAT-NOBASE"
    assert row.band.band == "—" and row.band.label == "NO BASELINE"
    assert "plan registry" in row.detail          # the named gap explains itself
    # A NO BASELINE row is NOT tallied into on_target (it is not a green claim).
    assert report.headline.on_target_count == 1


# --- the stale-projection WITHHOLDING case ----------------------------------


def test_stale_projection_withholds_deviation_and_counts(tmp_path: Path) -> None:
    """A stale ledger/build watermark WITHHOLDS the deviation chips + headline counts — never a
    dimmed-green claim (§4.7 / §5.6 F-5). Here: no fresh feeder watermark at all."""
    _db, dash = _projected_db(tmp_path)
    write_heartbeat(dash, NOW)              # projector alive...
    mirror_plans_once(dash, PLANS_PACK, NOW)
    _ledger(dash, "FEAT-9A21", "2026-07-08T10:00:00+00:00", project="finproxy-app", title="x")
    # ...but NO PIPELINE/FORGE_MIRROR watermark → the build/ledger feed is stale → WITHHELD.
    report = dbread.weekly_report(dash, "finproxy", NOW, window=WINDOW)
    assert report.headline.withheld is True
    assert report.plan.withheld is True
    assert report.headline.withheld_since != ""


def test_stale_heartbeat_also_withholds(tmp_path: Path) -> None:
    """A dead projector heartbeat (whole projection suspect) also withholds the plan verdict."""
    _db, dash = _projected_db(tmp_path)
    old = datetime(2026, 7, 11, 14, 0, 0, tzinfo=UTC)  # >60s before NOW → stale heartbeat
    write_heartbeat(dash, old)
    dash.execute(
        "INSERT INTO consumer_watermarks (stream, consumer, last_stream_seq, last_event_at, updated_at) "
        "VALUES ('PIPELINE','dashboard_ro',1,?,?)",
        (iso(NOW), iso(NOW)),
    )
    mirror_plans_once(dash, PLANS_PACK, NOW)
    report = dbread.weekly_report(dash, "finproxy", NOW, window=WINDOW)
    assert report.headline.withheld is True and report.plan.withheld is True


def test_reports_page_withheld_never_renders_dimmed_green(operator_client_reports: TestClient) -> None:
    """The rendered /reports withholds with the 'projection lagging' copy and shows no green band
    label (AHEAD/ON TARGET) when the projection is stale — never a dimmed-green claim."""
    client, db_path = operator_client_reports
    dash = open_rw(db_path)
    write_heartbeat(dash, NOW)
    mirror_plans_once(dash, PLANS_PACK, NOW)  # no fresh feeder watermark → withheld
    dash.close()
    body = client.get(f"/reports?tenant=finproxy&from={WINDOW[0]}&to={WINDOW[1]}").text
    assert "plan status unavailable — projection lagging" in body
    assert "ON TARGET" not in body and "AHEAD" not in body


# --- issues-window boundary cases -------------------------------------------


def _issue(dash: sqlite3.Connection, issue_id: str, *, feature: str, kind: str,
           opened_at: str | None, closed_at: str | None, detail: str = "") -> None:
    dash.execute(
        "INSERT INTO issues (issue_id, scope_type, scope_id, kind, opened_at, closed_at, detail, source_ref) "
        "VALUES (?, 'feature', ?, ?, ?, ?, ?, 'ref')",
        (issue_id, feature, kind, opened_at, closed_at, detail),
    )


def test_issues_window_boundaries(tmp_path: Path) -> None:
    """An issue opened on the `to` day counts; opened the day AFTER `to` is excluded; an issue
    CLOSED in the window counts (opened OR closed in `[lo, hi)`). Scoped to the tenant's projects."""
    _db, dash = _projected_db(tmp_path)
    _fresh_projection(dash)
    # All issues scope features in finproxy-app so they pass the tenant scope filter.
    _inflight(dash, "FEAT-9A21", project="finproxy-app", title="a")
    _inflight(dash, "FEAT-EDGE", project="finproxy-app", title="b")
    _inflight(dash, "FEAT-AFTER", project="finproxy-app", title="c")
    _inflight(dash, "FEAT-CLOSED", project="finproxy-app", title="d")
    _issue(dash, "i-mid", feature="FEAT-9A21", kind="gate_rejected",
           opened_at="2026-07-08T09:00:00+00:00", closed_at=None)
    _issue(dash, "i-toedge", feature="FEAT-EDGE", kind="gate_rejected",
           opened_at="2026-07-12T23:00:00+00:00", closed_at=None)              # on the `to` day → in
    _issue(dash, "i-after", feature="FEAT-AFTER", kind="gate_rejected",
           opened_at="2026-07-13T00:00:00+00:00", closed_at=None)             # day after `to` → out
    _issue(dash, "i-closed", feature="FEAT-CLOSED", kind="gate_rejected",
           opened_at="2026-06-20T00:00:00+00:00", closed_at="2026-07-09T00:00:00+00:00")  # closed in-window

    report = dbread.weekly_report(dash, "finproxy", NOW, window=WINDOW)
    features = {r.feature_id for r in report.issues.rows}
    assert features == {"FEAT-9A21", "FEAT-EDGE", "FEAT-CLOSED"}   # AFTER excluded
    assert report.issues.opened == 2                              # mid + toedge opened in-window
    assert report.issues.closed == 1                             # i-closed closed in-window


def test_issues_scoped_to_tenant_projects(tmp_path: Path) -> None:
    """An issue whose feature belongs to another tenant's project is NOT in this tenant's report."""
    _db, dash = _projected_db(tmp_path)
    _fresh_projection(dash)
    _inflight(dash, "FEAT-MINE", project="finproxy-app", title="mine")
    _inflight(dash, "FEAT-OTHER", project="acme-thing", title="other")
    _issue(dash, "i-mine", feature="FEAT-MINE", kind="gate_rejected",
           opened_at="2026-07-08T09:00:00+00:00", closed_at=None)
    _issue(dash, "i-other", feature="FEAT-OTHER", kind="gate_rejected",
           opened_at="2026-07-08T09:00:00+00:00", closed_at=None)
    report = dbread.weekly_report(dash, "finproxy", NOW, window=WINDOW)
    assert {r.feature_id for r in report.issues.rows} == {"FEAT-MINE"}


def test_issues_open_reds_first(tmp_path: Path) -> None:
    """Issues lead with open reds first (§4.5 bands): an open gate_rejected [R] before an open
    approval_waiting [A] before a closed row."""
    _db, dash = _projected_db(tmp_path)
    _fresh_projection(dash)
    _inflight(dash, "FEAT-R", project="finproxy-app", title="r")
    _inflight(dash, "FEAT-A", project="finproxy-app", title="a")
    _issue(dash, "i-a", feature="FEAT-A", kind="approval_waiting",
           opened_at="2026-07-08T09:00:00+00:00", closed_at=None)
    _issue(dash, "i-r", feature="FEAT-R", kind="gate_rejected",
           opened_at="2026-07-08T09:00:00+00:00", closed_at=None)
    report = dbread.weekly_report(dash, "finproxy", NOW, window=WINDOW)
    assert report.issues.rows[0].band.band == "R"


# --- window echo ------------------------------------------------------------


def test_window_echo_correct(tmp_path: Path) -> None:
    _db, dash = _projected_db(tmp_path)
    _seed_plan_pack(dash)
    report = dbread.weekly_report(dash, "finproxy", NOW, window=WINDOW)
    assert (report.window_from, report.window_to) == WINDOW


def test_default_window_is_the_current_iso_week(tmp_path: Path) -> None:
    """A bare report (no window) echoes the current ISO week (Mon..Sun), deterministic from `now`."""
    _db, dash = _projected_db(tmp_path)
    _fresh_projection(dash)
    report = dbread.weekly_report(dash, "finproxy", NOW)
    assert (report.window_from, report.window_to) == ("2026-07-06", "2026-07-12")


# --- the client-safe export firewall ----------------------------------------

# §7.2 + §4.7's extended deny set: NO operator-only field ever reaches the client-safe export.
_DENY_TOKENS = (
    "coach_score", "turns", "agent_id", "evidence",
    "issue", "failure_reason", "queue_depth", "originating_user", "defer_count",
    "planning_run", "correlation_id",
)


def test_export_template_is_grep_clean_of_operator_only_tokens() -> None:
    """Coach check, mechanized: the export TEMPLATE contains none of the deny-set tokens."""
    tmpl = (Path(__file__).resolve().parent.parent / "frontend" / "templates" / "reports_export.html").read_text()
    lowered = tmpl.lower()
    for token in _DENY_TOKENS:
        assert token not in lowered, f"operator-only token in export template: {token}"


def test_export_renders_zero_operator_only_fields_against_a_hostile_fixture(
    operator_client_reports: TestClient,
) -> None:
    """The export RENDERING, driven by a DB that contains EVERY operator-only field, leaks none of
    them (§9.4). Positive-assert the plan section renders only the four sanctioned milestone fields."""
    client, db_path = operator_client_reports
    dash = open_rw(db_path)
    _seed_plan_pack(dash, when=datetime.now(UTC))  # route computes `now` from the wall clock
    # Load the DB with operator-only data the export must NEVER surface:
    dash.execute(
        "INSERT INTO stage_events (feature_id, stage_label, status, gate_mode, coach_score, origin) "
        "VALUES ('FEAT-9A21','build-3','REJECTED','COACH', 0.42, 'bus')"
    )
    _issue(dash, "i-secret", feature="FEAT-9A21", kind="gate_rejected",
           opened_at="2026-07-08T09:00:00+00:00", closed_at=None, detail="OPERATOR_ONLY_ISSUE_DETAIL")
    dash.execute(
        "INSERT INTO agents (agent_id, name, status, queue_depth, source_kind) "
        "VALUES ('a1','forge-orch','busy', 7, 'fleet')"
    )
    dash.execute(
        "INSERT INTO planning_mirror (correlation_id, state, originating_user, defer_count, error, mirrored_at) "
        "VALUES ('cid-secret','PLANNED_HANDOFF','SECRET_USER', 3, 'SECRET_ERROR', ?)",
        (iso(NOW),),
    )
    dash.execute("UPDATE builds SET failure_reason='SECRET_FAILURE' WHERE feature_id='FEAT-NOBASE'")
    dash.close()

    body = client.get(f"/reports?view=export&tenant=finproxy&from={WINDOW[0]}&to={WINDOW[1]}").text
    lowered = body.lower()

    # Deny-set tokens: zero hits in the rendered export.
    for token in _DENY_TOKENS:
        assert token not in lowered, f"operator-only token leaked: {token}"
    # Distinctive operator-only VALUES never leak either.
    for secret in ("OPERATOR_ONLY_ISSUE_DETAIL", "SECRET_USER", "SECRET_ERROR", "SECRET_FAILURE", "0.42"):
        assert secret not in body, f"operator-only value leaked: {secret}"
    # In-flight internals and the NO BASELINE prompt are operator-only — not in the client export.
    assert "FEAT-NOBASE" not in body and "NO BASELINE" not in body
    assert "features merged" not in body  # the operator-only milestone detail line

    # Positive: the four SANCTIONED milestone fields DO render (id, title, target window, band).
    assert "M2" in body and "Payments API" in body
    assert "w/c 2026-07-06" in body           # target window
    assert "BEHIND" in body                    # deviation state (the fourth field)
    # And a permitted delivered field renders (feature title + change link).
    assert "Payments webhooks" in body


def test_export_uses_plain_language_spend_not_ask_ids(operator_client_reports: TestClient) -> None:
    """Client surfaces never cite internal ask ids (§7.2) — the export spend is plain-language."""
    client, db_path = operator_client_reports
    dash = open_rw(db_path)
    _seed_plan_pack(dash, when=datetime.now(UTC))
    dash.close()
    body = client.get(f"/reports?view=export&tenant=finproxy&from={WINDOW[0]}&to={WINDOW[1]}").text
    assert "provisioned" in body                       # plain-language spend copy
    assert "A-4" not in body and "IN-5" not in body    # no operator ask ids on a client surface


def test_export_delivered_rows_are_the_window_verified_deliveries(
    operator_client_reports: TestClient,
) -> None:
    """The export lists the tenant's in-window verified deliveries with a change receipt (§4.7)."""
    _client, db_path = operator_client_reports
    dash = open_rw(db_path)
    _seed_plan_pack(dash)
    dash.close()
    export = dbread.export_report_view(db_path, "finproxy", WINDOW, NOW)
    fids = {r.feature_id for r in export.delivered_rows}
    assert fids == {"FEAT-9A21", "FEAT-DONE"}
    assert all(r.change_verified for r in export.delivered_rows)


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def operator_client_reports(tmp_path: Path):  # type: ignore[no-untyped-def]
    """An app on a temp DB with the reports tenant registry (finproxy configured), logged in as
    operator. Yields (client, db_path) so a test can project data via the rw connection first."""
    db_path = tmp_path / "readmodel.db"
    app = create_app(db_path=db_path, tenants_path=TENANTS_REPORTS, plans_path=PLANS_PACK)
    with TestClient(app) as client:
        client.post("/login", data={"username": "operator", "password": "operator"})
        yield client, db_path
