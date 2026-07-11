"""S5 — the F-5 stalled-dwell monitor end-to-end at the ISSUE-REGISTER level (ux §10.6 case 3).

Closes the S2 coach finding: `should_manufacture_stalled` was wired to a projector-side periodic
evaluation. These exercise the three F-5 cases through `evaluate_dwell` writing/clearing the real
`issues` register — not the isolated predicate (that is `test_freshness.py`).

Setup shape for a dwell breach with a FRESH watermark: feed the target build's start with an OLD
timestamp (so its stage dwell is breached) THEN a fresh pipeline event from another build (so the
PIPELINE watermark is fresh — "other events flowed, the silence is real").
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.projector.consumers import apply_message, write_heartbeat
from backend.projector.dwell import evaluate_dwell
from backend.readmodel import dbread
from backend.readmodel.viewmodels import PanelState

from tests.nats_util import envelope, make_projected_db

NOW = datetime(2026, 7, 11, 14, 13, 34, tzinfo=UTC)
DWELL_3H = 3 * 3600  # well past the >120m red norm


def _ago(secs: float) -> datetime:
    return NOW - timedelta(seconds=secs)


def _start_build(conn: sqlite3.Connection, feature: str, build: str, *, when: datetime) -> None:
    """A build_started whose event timestamp (→ started_at, → watermark) is `when`."""
    apply_message(
        conn,
        f"pipeline.build-started.{feature}",
        envelope("build_started", {"feature_id": feature, "build_id": build, "wave_total": 6}, timestamp=when),
        NOW,
    )


def _fresh_other_pipeline_event(conn: sqlite3.Connection) -> None:
    """Another build's fresh start — advances the PIPELINE watermark to NOW (other events flowed)."""
    _start_build(conn, "FEAT-FRESH", "b-fresh", when=NOW)


def _resume_stage(conn: sqlite3.Connection, feature: str, build: str) -> None:
    """A fresh stage completion for the target run — its events resume, dwell resets below threshold."""
    apply_message(
        conn,
        f"pipeline.stage-complete.{feature}",
        envelope(
            "stage_complete",
            {"feature_id": feature, "build_id": build, "stage_label": "build-2", "status": "PASSED",
             "gate_mode": "AUTO_APPROVE", "coach_score": 0.9, "duration_secs": 30.0,
             "completed_at": NOW.isoformat()},
            timestamp=NOW,
        ),
        NOW,
    )


def _open_stalled_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM issues WHERE kind='stalled' AND closed_at IS NULL").fetchone()[0]
    )


def _stalled_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM issues WHERE kind='stalled'").fetchone()[0])


# --- Case 1: heartbeat stale ⇒ page LAGGING + ZERO stalled rows --------------


def test_case1_heartbeat_stale_suppresses_all_stalled(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    _start_build(conn, "FEAT-STALL", "b1", when=_ago(DWELL_3H))  # dwell breached
    _fresh_other_pipeline_event(conn)                            # watermark FRESH
    write_heartbeat(conn, _ago(120))                             # but the projector heartbeat is STALE

    evaluate_dwell(conn, NOW)

    # the whole projection is suspect ⇒ NO stalled verdict manufactured
    assert _stalled_rows(conn) == 0
    chrome = dbread.page_chrome(conn, NOW)
    assert chrome.projector_state == "stale"
    assert chrome.banner is not None and "LAGGING" in chrome.banner.text


# --- Case 2: dwell breach + STALE watermark ⇒ "projection lagging", ZERO stalled


def test_case2_stale_watermark_reports_lagging_not_stalled(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    _start_build(conn, "FEAT-STALL", "b1", when=_ago(DWELL_3H))  # dwell breached, watermark left stale
    write_heartbeat(conn, NOW)                                   # projector alive

    evaluate_dwell(conn, NOW)

    # quiet-or-wedged is indistinguishable from a stale watermark ⇒ NO stalled verdict
    assert _stalled_rows(conn) == 0
    # panel reports LAGGING ("projection lagging (since ⟨ts⟩)"), not a fake-live board
    assert dbread.build_board(conn, NOW).state is PanelState.LAGGING


# --- Case 3: dwell breach + FRESH watermark ⇒ manufacture, then clear on resume


def test_case3_fresh_watermark_manufactures_then_clears_on_resume(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    _start_build(conn, "FEAT-STALL", "b1", when=_ago(DWELL_3H))  # dwell breached
    _fresh_other_pipeline_event(conn)                            # watermark FRESH (other events flowed)
    write_heartbeat(conn, NOW)                                   # projector alive

    res = evaluate_dwell(conn, NOW)

    assert "stalled:b1" in res.opened
    assert _open_stalled_count(conn) == 1
    row = conn.execute(
        "SELECT scope_id, kind FROM issues WHERE issue_id='stalled:b1' AND closed_at IS NULL"
    ).fetchone()
    assert row is not None and row[0] == "FEAT-STALL" and row[1] == "stalled"

    # re-evaluation is idempotent: no duplicate row, opened_at stays stable
    opened_at_before = conn.execute("SELECT opened_at FROM issues WHERE issue_id='stalled:b1'").fetchone()[0]
    res2 = evaluate_dwell(conn, NOW)
    assert res2.opened == set()
    assert _stalled_rows(conn) == 1
    assert conn.execute("SELECT opened_at FROM issues WHERE issue_id='stalled:b1'").fetchone()[0] == opened_at_before

    # the run's events resume ⇒ the stalled issue clears (closed, not deleted; no new row)
    _resume_stage(conn, "FEAT-STALL", "b1")
    res3 = evaluate_dwell(conn, NOW)
    assert "stalled:b1" in res3.closed
    assert _open_stalled_count(conn) == 0
    assert _stalled_rows(conn) == 1
    assert conn.execute("SELECT closed_at FROM issues WHERE issue_id='stalled:b1'").fetchone()[0] is not None


# --- suppression never CLOSES a standing stalled issue -----------------------


def test_suppression_does_not_close_a_standing_stalled_issue(tmp_path: Path) -> None:
    """A projection that goes suspect (heartbeat stale) after a stall was manufactured leaves the
    last honest verdict in place — it neither manufactures nor clears under suspicion."""
    _db, conn = make_projected_db(tmp_path)
    _start_build(conn, "FEAT-STALL", "b1", when=_ago(DWELL_3H))
    _fresh_other_pipeline_event(conn)
    write_heartbeat(conn, NOW)
    evaluate_dwell(conn, NOW)
    assert _open_stalled_count(conn) == 1

    # projector goes stale — no fresh events, no fresh heartbeat
    write_heartbeat(conn, _ago(120))
    evaluate_dwell(conn, NOW)
    assert _open_stalled_count(conn) == 1  # still open — suppression does not clear it


# --- a terminal build clears its standing stalled issue ----------------------


def test_stalled_clears_when_build_leaves_in_flight(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    _start_build(conn, "FEAT-STALL", "b1", when=_ago(DWELL_3H))
    _fresh_other_pipeline_event(conn)
    write_heartbeat(conn, NOW)
    evaluate_dwell(conn, NOW)
    assert _open_stalled_count(conn) == 1

    # the build completes (leaves the in-flight set) — the stall is moot
    apply_message(
        conn, "pipeline.build-complete.FEAT-STALL",
        envelope("build_complete", {"feature_id": "FEAT-STALL", "build_id": "b1",
                                     "tasks_completed": 5, "tasks_failed": 0, "tasks_total": 5,
                                     "duration_seconds": 10800, "summary": "done"}, timestamp=NOW),
        NOW,
    )
    evaluate_dwell(conn, NOW)
    assert _open_stalled_count(conn) == 0


# --- a quiet in-flight build with no dwell basis is neutral (nothing at stake)


def test_quiet_build_no_activity_basis_no_stalled(tmp_path: Path) -> None:
    """A build with no started_at / stage events (e.g. progress-only) has no dwell basis — the
    monitor manufactures nothing (no verdict at stake), never a false stalled row."""
    _db, conn = make_projected_db(tmp_path)
    apply_message(
        conn, "pipeline.build-progress.FEAT-Q",
        envelope("build_progress", {"feature_id": "FEAT-Q", "build_id": "bq", "wave": 2,
                                    "wave_total": 6, "overall_progress_pct": 30.0, "elapsed_seconds": 60},
                 timestamp=NOW),
        NOW,
    )
    write_heartbeat(conn, NOW)
    evaluate_dwell(conn, NOW)
    assert _stalled_rows(conn) == 0
