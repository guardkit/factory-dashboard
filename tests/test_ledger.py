"""S3 (D2) delivery ledger: bootstrap parity from forge `builds`, the build-complete append (with
receipt / receipt-absent / idempotent), and the period query's F-10b two-list semantics —
window-boundary, upgrade-exclusion, and the receipt-absent "merge unverified" gap. Bar labelling is
verbatim per ux §4.4; "ledger" leaks into no rendered copy.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from backend import ledger
from backend.app import create_app
from backend.projector.consumers import apply_message, open_rw, write_heartbeat
from backend.readmodel import dbread
from backend.readmodel.viewmodels import PanelState
from starlette.testclient import TestClient

from tests.nats_util import envelope, make_projected_db

TENANTS_TEST = Path(__file__).resolve().parent / "fixtures" / "tenants_test.yaml"

NOW = datetime(2026, 7, 11, 14, 13, 34, tzinfo=UTC)
WINDOW = ("2026-07-01", "2026-07-08")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


# --- forge `builds` fixture (bootstrap source) ------------------------------


def _make_forge_builds_fixture(path: Path) -> None:
    """A minimal forge SQLite with a `builds` table (no merge_sha column — verified prod schema):
    one COMPLETE build WITH a pr_url receipt, one COMPLETE with NEITHER receipt, one RUNNING."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE builds (
            build_id TEXT PRIMARY KEY, feature_id TEXT, project TEXT, repo TEXT, branch TEXT,
            status TEXT, completed_at TEXT, pr_url TEXT, correlation_id TEXT
        );
        INSERT INTO builds VALUES
            ('build-FEAT-3ED2-1','FEAT-3ED2','study-tutor','study-tutor','main','COMPLETE',
             '2026-07-06T14:13:00+00:00','https://github.com/x/pull/9','cid-1'),
            ('build-FEAT-9E59-1','FEAT-9E59','study-tutor','study-tutor','main','COMPLETE',
             '2026-07-07T09:00:00+00:00',NULL,'cid-2'),
            ('build-FEAT-51B0-1','FEAT-51B0','study-tutor','study-tutor','main','RUNNING',
             NULL,NULL,'cid-3');
        """
    )
    conn.commit()
    conn.close()


def test_bootstrap_parity_receipt_clears_bar_and_gap_stays_unverified(tmp_path: Path) -> None:
    forge_path = tmp_path / "forge_fixture.db"
    _make_forge_builds_fixture(forge_path)
    _db, dash = make_projected_db(tmp_path)

    panels = ledger.bootstrap_from_forge(dash, forge_path, NOW)
    assert panels == {"p2", "p7"}  # a receipt-bearing complete touched both the board and delivered

    # The receipt-bearing COMPLETE landed a merged_pr ledger row...
    ledgered = dash.execute("SELECT feature_id, bar, pr_url FROM ledger").fetchall()
    assert ledgered == [("FEAT-3ED2", "merged_pr", "https://github.com/x/pull/9")]
    # ...both completes were mirrored into builds (source=forge_sqlite); the RUNNING one too.
    completes = dash.execute(
        "SELECT feature_id, source FROM builds WHERE status='COMPLETE' ORDER BY feature_id"
    ).fetchall()
    assert completes == [("FEAT-3ED2", "forge_sqlite"), ("FEAT-9E59", "forge_sqlite")]


def test_bootstrap_missing_forge_db_is_honest_noop(tmp_path: Path) -> None:
    _db, dash = make_projected_db(tmp_path)
    assert ledger.bootstrap_from_forge(dash, tmp_path / "nope.db", NOW) == set()
    assert dash.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


# --- steady-state build-complete append -------------------------------------


def test_append_from_build_complete_with_pr_receipt(tmp_path: Path) -> None:
    _db, dash = make_projected_db(tmp_path)
    env = envelope(
        "build_complete",
        {"feature_id": "FEAT-3ED2", "build_id": "b1", "repo": "study-tutor",
         "branch": "main", "pr_url": "https://github.com/x/pull/9", "summary": "Study tutor chain"},
        project="study-tutor", timestamp=NOW,
    )
    changed = apply_message(dash, "pipeline.build-complete.FEAT-3ED2", env, NOW)
    assert "p7" in changed.panels  # the ledger append rode the same routing pass
    row = dash.execute("SELECT feature_id, bar, title FROM ledger").fetchone()
    assert row == ("FEAT-3ED2", "merged_pr", "Study tutor chain")


def test_append_from_build_complete_with_merge_sha_receipt(tmp_path: Path) -> None:
    """The amended DDR-DASH-001: `merge_sha` alone (the primary receipt, A-11) clears the bar."""
    _db, dash = make_projected_db(tmp_path)
    env = envelope(
        "build_complete",
        {"feature_id": "FEAT-DD4F", "build_id": "b2", "repo": "study-tutor",
         "merge_sha": "34b17d0abc", "summary": "Planning wiring fix"},
        project="study-tutor", timestamp=NOW,
    )
    apply_message(dash, "pipeline.build-complete.FEAT-DD4F", env, NOW)
    row = dash.execute("SELECT feature_id, bar, merge_sha FROM ledger").fetchone()
    assert row == ("FEAT-DD4F", "merged_pr", "34b17d0abc")


def test_append_without_receipt_writes_no_ledger_row(tmp_path: Path) -> None:
    """A COMPLETE with NEITHER receipt clears no bar — the build is still projected, but the
    delivered page renders it as the "merge unverified" gap, never a delivered row (§4.4)."""
    _db, dash = make_projected_db(tmp_path)
    env = envelope(
        "build_complete",
        {"feature_id": "FEAT-9E59", "build_id": "b3", "repo": "study-tutor", "summary": "no receipt"},
        project="study-tutor", timestamp=NOW,
    )
    changed = apply_message(dash, "pipeline.build-complete.FEAT-9E59", env, NOW)
    assert "p7" not in changed.panels
    assert dash.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
    # ...but the build itself is COMPLETE in the read model (the gap's source row).
    assert dash.execute("SELECT status FROM builds WHERE feature_id='FEAT-9E59'").fetchone()[0] == "COMPLETE"


def test_append_is_idempotent_on_replay(tmp_path: Path) -> None:
    """Idempotent upsert keyed (feature_id, bar): a JetStream-less replay / projector restart never
    double-counts (design §5 steady state)."""
    _db, dash = make_projected_db(tmp_path)
    env = envelope(
        "build_complete",
        {"feature_id": "FEAT-3ED2", "build_id": "b1", "pr_url": "https://github.com/x/pull/9"},
        project="study-tutor", timestamp=NOW,
    )
    apply_message(dash, "pipeline.build-complete.FEAT-3ED2", env, NOW)
    apply_message(dash, "pipeline.build-complete.FEAT-3ED2", env, NOW)
    assert dash.execute("SELECT COUNT(*) FROM ledger WHERE feature_id='FEAT-3ED2'").fetchone()[0] == 1


# --- period query: F-10b two-list semantics ---------------------------------


def _insert_ledger(dash: sqlite3.Connection, feature_id: str, bar: str, delivered_at: str,
                   *, pr_url: str | None = None, merge_sha: str | None = None,
                   title: str | None = None, project: str = "study-tutor") -> None:
    dash.execute(
        "INSERT INTO ledger (feature_id, project, tenant, title, bar, delivered_at, pr_url, "
        "merge_sha, repo, branch, evidence_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (feature_id, project, None, title, bar, delivered_at, pr_url, merge_sha, project, "main",
         f"forge_sqlite:{feature_id}@{delivered_at}"),
    )


def test_period_window_is_half_open_inclusive_of_the_to_day(tmp_path: Path) -> None:
    """`[from 00:00 … to 23:59]` inclusive: a delivery on `from`, mid-window, and on the `to` day
    all count; a delivery on the day AFTER `to` is excluded (design §5 `>= ? AND < ?`)."""
    _db, dash = make_projected_db(tmp_path)
    write_heartbeat(dash, NOW)
    _insert_ledger(dash, "FEAT-AAAA", "merged_pr", "2026-07-01T00:00:00+00:00", pr_url="u", title="from edge")
    _insert_ledger(dash, "FEAT-BBBB", "merged_pr", "2026-07-06T14:13:00+00:00", pr_url="u", title="mid")
    _insert_ledger(dash, "FEAT-CCCC", "merged_pr", "2026-07-08T23:00:00+00:00", pr_url="u", title="to edge")
    _insert_ledger(dash, "FEAT-DDDD", "merged_pr", "2026-07-09T00:00:00+00:00", pr_url="u", title="after")

    panel = dbread.delivered_panel(dash, NOW, window=WINDOW)
    assert panel.state is PanelState.LIVE
    delivered_ids = {r.feature_id for r in panel.rows if r.change_verified}
    assert delivered_ids == {"FEAT-AAAA", "FEAT-BBBB", "FEAT-CCCC"}
    assert panel.delivered_count == 3


def test_period_upgrade_exclusion(tmp_path: Path) -> None:
    """F-10b: a graduation row IN the window whose feature was FIRST delivered in a PRIOR window is
    an upgrade, not a new delivery — excluded from the count AND the delivered list (the upgrades
    slot is FEED-PENDING, never a number)."""
    _db, dash = make_projected_db(tmp_path)
    write_heartbeat(dash, NOW)
    # First delivered BEFORE the window...
    _insert_ledger(dash, "FEAT-UP", "merged_pr", "2026-06-20T10:00:00+00:00", pr_url="u", title="upgraded")
    # ...graduated (a second bar) INSIDE the window.
    _insert_ledger(dash, "FEAT-UP", "deployed_live_verified", "2026-07-05T10:00:00+00:00", pr_url="u",
                   title="upgraded")
    # A genuinely-new delivery in the window for contrast.
    _insert_ledger(dash, "FEAT-NEW", "merged_pr", "2026-07-04T10:00:00+00:00", pr_url="u", title="new")

    panel = dbread.delivered_panel(dash, NOW, window=WINDOW)
    delivered_ids = {r.feature_id for r in panel.rows if r.change_verified}
    assert delivered_ids == {"FEAT-NEW"}          # the upgrade is NOT a new delivery
    assert panel.delivered_count == 1
    assert panel.upgrades_pending is True          # upgrades slot stays FEED-PENDING (never a 0)


def test_period_receipt_absent_renders_as_named_gap_not_counted(tmp_path: Path) -> None:
    """A COMPLETE build with NEITHER receipt in-window renders the "complete — merge unverified"
    gap row (change_verified False) and is NOT counted as delivered (§4.4)."""
    _db, dash = make_projected_db(tmp_path)
    write_heartbeat(dash, NOW)
    dash.execute(
        "INSERT INTO builds (build_id, feature_id, project, status, completed_at, source) "
        "VALUES ('b9','FEAT-GAP','study-tutor','COMPLETE','2026-07-06T12:00:00+00:00','forge_sqlite')"
    )
    panel = dbread.delivered_panel(dash, NOW, window=WINDOW)
    assert panel.delivered_count == 0
    gap = next(r for r in panel.rows if r.feature_id == "FEAT-GAP")
    assert gap.change_verified is False
    assert gap.bar_label == "complete — merge unverified"
    assert gap.change_link == ""


def test_period_delivered_feature_not_also_a_gap(tmp_path: Path) -> None:
    """A feature that reached the ledger is delivered — it must never ALSO surface as a gap row,
    even if a receipt-less COMPLETE build exists for it."""
    _db, dash = make_projected_db(tmp_path)
    write_heartbeat(dash, NOW)
    _insert_ledger(dash, "FEAT-3ED2", "merged_pr", "2026-07-06T14:13:00+00:00", pr_url="u", title="delivered")
    dash.execute(
        "INSERT INTO builds (build_id, feature_id, project, status, completed_at, source) "
        "VALUES ('b1','FEAT-3ED2','study-tutor','COMPLETE','2026-07-06T14:13:00+00:00','forge_sqlite')"
    )
    panel = dbread.delivered_panel(dash, NOW, window=WINDOW)
    rows_for = [r for r in panel.rows if r.feature_id == "FEAT-3ED2"]
    assert len(rows_for) == 1 and rows_for[0].change_verified is True


def test_bar_labelling_verbatim(tmp_path: Path) -> None:
    """ux §4.4 acceptance: verified bar label is VERBATIM 'Delivered — merged ⟨change⟩'."""
    _db, dash = make_projected_db(tmp_path)
    write_heartbeat(dash, NOW)
    _insert_ledger(dash, "FEAT-3ED2", "merged_pr", "2026-07-06T14:13:00+00:00",
                   merge_sha="34b17d0abc", title="Study tutor chain")
    panel = dbread.delivered_panel(dash, NOW, window=WINDOW)
    row = panel.rows[0]
    assert row.bar_label == "Delivered — merged ⟨change⟩"
    assert row.change_verified is True
    assert row.change_link == "/change/study-tutor/34b17d0"  # merge-commit receipt (pr_url absent)


def test_empty_window_is_true_empty_when_projector_alive(tmp_path: Path) -> None:
    _db, dash = make_projected_db(tmp_path)
    write_heartbeat(dash, NOW)
    panel = dbread.delivered_panel(dash, NOW, window=WINDOW)
    assert panel.state is PanelState.TRUE_EMPTY and panel.delivered_count == 0


# --- the /delivered page (§4.4 wireframe) -----------------------------------


def test_delivered_page_renders_live_rows_and_leaks_no_ledger_copy(tmp_path: Path) -> None:
    """Coach check: the Delivered page renders the §4.4 wireframe with 'ledger' in NO rendered copy
    (§4.4 naming map), the verbatim bar label, and the FEED-PENDING upgrades + spend slots."""
    db_path = tmp_path / "readmodel.db"
    app = create_app(db_path=db_path, tenants_path=TENANTS_TEST)
    # project a delivered feature via the projector's rw connection, then read through the app (ro)
    dash = open_rw(db_path)
    write_heartbeat(dash)
    _insert_ledger(dash, "FEAT-3ED2", "merged_pr", "2026-07-06T14:13:00+00:00",
                   merge_sha="34b17d0abc", title="Study tutor planning chain")
    dash.close()

    with TestClient(app) as c:
        c.post("/login", data={"username": "operator", "password": "operator"})
        body = c.get("/delivered?from=2026-07-01&to=2026-07-08").text

    assert "Delivered — merged" in body                 # verbatim bar label
    assert "Study tutor planning chain" in body         # title leads (§5.8)
    assert "graduation feed pending" in body            # upgrades slot FEED-PENDING (never a 0)
    assert "ledger" not in body.lower()                 # naming map: no "ledger" in rendered copy
    assert "M-D2 parity" in body                        # the display-only parity footer
