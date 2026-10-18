"""The feed's copy of Forge's record: read-only, complete, incremental, honest about failures."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from backend import db
from backend.projector.forge_mirror import connect_forge_ro, forge_db_path, mirror_once, prune_change_log
from backend.projector.store import iso, open_rw

from tests.forge_fake import ForgeFake, delivered_feature, store_from


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.fixture
def fake(tmp_path: Path) -> ForgeFake:
    return ForgeFake(tmp_path / "forge.db")


def test_forge_open_is_read_only(fake: ForgeFake) -> None:
    ro = connect_forge_ro(fake.path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO missions (id, title, target_repo, branch, status, correlation_id, created_at) "
                       "VALUES ('m','t','r','b','RUNNING','c','x')")
    finally:
        ro.close()


def test_default_path_is_the_read_only_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_FORGE_DB_PATH", raising=False)
    assert str(forge_db_path()) == "/forge-state/forge.db"
    monkeypatch.setenv("FACTORY_FORGE_DB_PATH", "/x/forge.db")
    assert str(forge_db_path()) == "/x/forge.db"


def test_copies_every_table_the_pages_read(fake: ForgeFake, tmp_path: Path) -> None:
    delivered_feature(fake, 1)
    fake.queue("c-1", "Feature number 1 does a thing.")
    fake.mission("m-1", "A mission", "mc-1")
    conn = store_from(fake, tmp_path / "store.db")
    assert _count(conn, "forge_builds") == 1
    assert _count(conn, "forge_stage_log") == 3
    assert _count(conn, "forge_planning_runs") == 1
    assert _count(conn, "forge_planning_events") == 3
    assert _count(conn, "forge_queue") == 1
    assert _count(conn, "forge_missions") == 1
    status = conn.execute("SELECT last_success_at, last_error FROM mirror_status WHERE source='forge'").fetchone()
    assert status[0] and status[1] is None


def test_a_second_pass_with_nothing_new_changes_nothing(fake: ForgeFake, tmp_path: Path) -> None:
    delivered_feature(fake, 1)
    conn = store_from(fake, tmp_path / "store.db")
    seq = conn.execute("SELECT MAX(seq) FROM change_log").fetchone()[0]
    assert mirror_once(conn, fake.path) is False
    assert conn.execute("SELECT MAX(seq) FROM change_log").fetchone()[0] == seq  # no live-update noise
    assert _count(conn, "forge_stage_log") == 3


def test_new_rows_and_changed_builds_are_picked_up(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c-9", "Something new.", state="RUNNING", end=None)
    conn = store_from(fake, tmp_path / "store.db")
    fake.build("build-FEAT-0009", "FEAT-0009", "c-9", status="RUNNING", completed=None)
    fake.event("c-9", "build-queued", "approved", 20)
    assert mirror_once(conn, fake.path) is True
    fake.conn.execute("UPDATE builds SET status='COMPLETE', completed_at=? WHERE build_id='build-FEAT-0009'",
                      ("2026-10-16T10:30:00+00:00",))
    fake.work("build-FEAT-0009", 60)
    assert mirror_once(conn, fake.path) is True
    assert conn.execute("SELECT status FROM forge_builds").fetchone()[0] == "COMPLETE"
    assert _count(conn, "forge_stage_log") == 1
    assert _count(conn, "forge_planning_events") == 1


def test_a_replaced_forge_record_is_read_again_in_full(fake: ForgeFake, tmp_path: Path) -> None:
    for minute in range(5):
        fake.work("b", minute)
    conn = store_from(fake, tmp_path / "store.db")
    assert _count(conn, "forge_stage_log") == 5
    replacement = ForgeFake(tmp_path / "restored.db")
    replacement.work("b", 1)
    mirror_once(conn, replacement.path)
    assert _count(conn, "forge_stage_log") == 1


def test_a_missing_record_is_a_failed_read_and_keeps_the_last_copy(fake: ForgeFake, tmp_path: Path) -> None:
    delivered_feature(fake, 1)
    conn = store_from(fake, tmp_path / "store.db")
    good = conn.execute("SELECT last_success_at FROM mirror_status").fetchone()[0]
    later = datetime.now(UTC) + timedelta(minutes=5)
    assert mirror_once(conn, tmp_path / "gone.db", later) is False
    row = conn.execute("SELECT last_success_at, last_attempt_at, last_error FROM mirror_status").fetchone()
    assert row[0] == good and row[1] == iso(later)
    assert "not at" in row[2]
    assert _count(conn, "forge_builds") == 1  # never an empty factory


def test_an_unreadable_record_is_recorded_not_raised(tmp_path: Path) -> None:
    bad = tmp_path / "forge.db"
    bad.write_bytes(b"this is not a database at all, just bytes" * 100)
    store = tmp_path / "store.db"
    db.apply_schema(store)
    conn = open_rw(store)
    assert mirror_once(conn, bad) is False
    assert "could not read" in conn.execute("SELECT last_error FROM mirror_status").fetchone()[0]


def test_an_older_ledger_without_missions_still_copies(tmp_path: Path) -> None:
    old = tmp_path / "old.db"
    c = sqlite3.connect(old)
    c.executescript(
        "CREATE TABLE builds (build_id TEXT, feature_id TEXT, repo TEXT, branch TEXT, status TEXT, "
        "correlation_id TEXT, queued_at TEXT, started_at TEXT, completed_at TEXT, error TEXT);"
        "INSERT INTO builds VALUES "
        "('b1','FEAT-1','r','main','COMPLETE','c1','2026-10-01T00:00:00+00:00',NULL,NULL,NULL);"
    )
    c.close()
    store = tmp_path / "store.db"
    db.apply_schema(store)
    conn = open_rw(store)
    mirror_once(conn, old)
    assert conn.execute("SELECT feature_id, mission_role FROM forge_builds").fetchone() == ("FEAT-1", None)


def test_the_feed_creates_the_schema_itself(fake: ForgeFake, tmp_path: Path) -> None:
    """17 October: the feed crashed on first start because only the web container made the tables."""
    store = tmp_path / "fresh" / "store.db"
    db.apply_schema(store)  # what the feed's start-up does, with no web container involved
    conn = open_rw(store)
    delivered_feature(fake, 1)
    assert mirror_once(conn, fake.path) is True
    db.apply_schema(store)  # and again: idempotent
    assert _count(conn, "forge_builds") == 1


def test_old_live_update_notifications_are_pruned(fake: ForgeFake, tmp_path: Path) -> None:
    conn = store_from(fake, tmp_path / "store.db")
    old = datetime.now(UTC) - timedelta(hours=3)
    conn.execute("INSERT INTO change_log (panel, scope_keys, at) VALUES ('p2', NULL, ?)", (iso(old),))
    prune_change_log(conn, datetime.now(UTC))
    assert conn.execute("SELECT COUNT(*) FROM change_log WHERE at < ?",
                        (iso(datetime.now(UTC) - timedelta(hours=1)),)).fetchone()[0] == 0
