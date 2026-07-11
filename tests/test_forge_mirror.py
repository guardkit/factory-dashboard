"""S2 forge mirror (fence 4): mode=ro (write fails), the CONFIGURED path (never ~/.forge default),
planning + stage backfill, idempotency, and an honest empty pass on a missing DB."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from backend.projector import forge_mirror

from tests.nats_util import make_projected_db


def _make_forge_fixture(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE planning_runs (
            correlation_id TEXT PRIMARY KEY, state TEXT, originating_user TEXT, expected_approver TEXT,
            request_text TEXT, target_repo TEXT, defer_count INTEGER, escalated_at TEXT,
            handoff_branch TEXT, handoff_path TEXT, queued_at TEXT, started_at TEXT,
            completed_at TEXT, error TEXT
        );
        CREATE TABLE stage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, build_id TEXT, stage_label TEXT, target_kind TEXT,
            target_identifier TEXT, status TEXT, gate_mode TEXT, coach_score REAL, duration_secs REAL,
            completed_at TEXT, details_json TEXT
        );
        INSERT INTO planning_runs (correlation_id, state, originating_user, target_repo, defer_count,
                                   queued_at, started_at)
            VALUES ('cid-8c41abcd', 'PLANNED_HANDOFF', 'rich', 'lpa-poc', 1,
                    '2026-07-11T13:11:00+00:00', '2026-07-11T13:12:00+00:00');
        INSERT INTO stage_log (build_id, stage_label, target_kind, target_identifier, status, gate_mode,
                               coach_score, duration_secs, completed_at, details_json)
            VALUES ('build-FEAT-3ED2-20260706', 'build-6', 'subagent', 'FEAT-3ED2', 'PASSED', NULL,
                    0.91, 12.0, '2026-07-11T14:13:00+00:00', '{"feature_id": "FEAT-3ED2"}');
        """
    )
    conn.commit()
    conn.close()


def test_forge_open_is_read_only_write_fails(tmp_path: Path) -> None:
    forge_path = tmp_path / "forge_fixture.db"
    _make_forge_fixture(forge_path)
    ro = forge_mirror.connect_forge_ro(forge_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO planning_runs (correlation_id, state) VALUES ('x', 'y')")
    finally:
        ro.close()


def test_configured_path_is_prod_bindmount_never_home_forge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_FORGE_DB_PATH", raising=False)
    default = str(forge_mirror.forge_db_path())
    assert "forge-prod-state" in default
    assert default != str(Path.home() / ".forge" / "forge.db")  # NEVER the stale dev DB (drift 1)
    monkeypatch.setenv("FACTORY_FORGE_DB_PATH", "/tmp/explicit/forge.db")
    assert str(forge_mirror.forge_db_path()) == "/tmp/explicit/forge.db"


def test_mirror_projects_planning_and_stage(tmp_path: Path) -> None:
    forge_path = tmp_path / "forge_fixture.db"
    _make_forge_fixture(forge_path)
    _db, dash = make_projected_db(tmp_path)

    changed = forge_mirror.mirror_once(dash, forge_path)
    assert changed == {"p5", "p3"}

    run = dash.execute("SELECT state, originating_user, defer_count FROM planning_mirror").fetchone()
    assert run == ("PLANNED_HANDOFF", "rich", 1)
    stage = dash.execute("SELECT feature_id, origin FROM stage_events WHERE stage_label='build-6'").fetchone()
    assert stage == ("FEAT-3ED2", "forge_sqlite")  # feature id derived from details_json; SQLite origin
    wm = dash.execute("SELECT last_event_at FROM consumer_watermarks WHERE stream='FORGE_MIRROR'").fetchone()
    assert wm is not None and wm[0] is not None  # the mirror pass is stamped (P5 freshness gate)


def test_mirror_is_idempotent(tmp_path: Path) -> None:
    forge_path = tmp_path / "forge_fixture.db"
    _make_forge_fixture(forge_path)
    _db, dash = make_projected_db(tmp_path)
    forge_mirror.mirror_once(dash, forge_path)
    forge_mirror.mirror_once(dash, forge_path)
    assert dash.execute("SELECT COUNT(*) FROM stage_events").fetchone()[0] == 1
    assert dash.execute("SELECT COUNT(*) FROM planning_mirror").fetchone()[0] == 1


def test_missing_forge_db_is_an_honest_empty_pass(tmp_path: Path) -> None:
    _db, dash = make_projected_db(tmp_path)
    changed = forge_mirror.mirror_once(dash, tmp_path / "does-not-exist.db")
    assert changed == set()
    assert dash.execute("SELECT COUNT(*) FROM planning_mirror").fetchone()[0] == 0
    # a pass with no rows is still stamped — so P5 renders LAGGING (mirror never run), not TRUE-EMPTY
    assert dash.execute("SELECT COUNT(*) FROM consumer_watermarks WHERE stream='FORGE_MIRROR'").fetchone()[0] == 1
