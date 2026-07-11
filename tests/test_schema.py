"""Schema applies to a temp DB, the norms seed loads, and the client DDL is reduced (design §4)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.config_loader import load_tenants
from backend.db import init_client_db, init_db

CONFIG = Path(__file__).resolve().parent / "fixtures" / "tenants_test.yaml"

EXPECTED_TABLES = {
    "tenants", "users", "consumer_watermarks", "agents", "builds", "stage_events",
    "approvals", "issues", "planning_mirror", "ledger", "norms", "deploys",
    "live_verdicts", "episodes", "usage_gateway", "usage_frontier", "cost_rollups",
    "service_health", "plan_milestones",
}

EXPECTED_NORMS = {
    "runs", "turns_per_task", "sdk_ceiling_hits", "gate_failed_gated",
    "stage_dwell", "approval_waiting",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_operational_schema_applies_all_tables(tmp_path: Path) -> None:
    db = tmp_path / "rm.db"
    init_db(db, load_tenants(CONFIG).values())
    conn = sqlite3.connect(db)
    try:
        assert _tables(conn) >= EXPECTED_TABLES
    finally:
        conn.close()


def test_norms_seed_loads_founding_values(tmp_path: Path) -> None:
    db = tmp_path / "rm.db"
    init_db(db, load_tenants(CONFIG).values())
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT metric, green, amber, red, source, pinned_at FROM norms").fetchall()
        metrics = {r[0] for r in rows}
        assert metrics == EXPECTED_NORMS
        for _metric, green, amber, red, source, pinned_at in rows:
            assert green and amber and red and source and pinned_at  # every band pinned w/ source
            assert pinned_at == "2026-07-07"
    finally:
        conn.close()


def test_plan_milestones_ships_empty(tmp_path: Path) -> None:
    db = tmp_path / "rm.db"
    init_db(db, load_tenants(CONFIG).values())
    conn = sqlite3.connect(db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM plan_milestones").fetchone()[0]
        assert count == 0  # mirror logic is S4's; the table ships EMPTY at S1
    finally:
        conn.close()


def test_ledger_has_merge_sha_column(tmp_path: Path) -> None:
    """Amended DDR-DASH-001 (2026-07-11): the bar clears on merge_sha OR pr_url."""
    db = tmp_path / "rm.db"
    init_db(db, load_tenants(CONFIG).values())
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ledger)").fetchall()}
        assert "merge_sha" in cols
        assert "pr_url" in cols
    finally:
        conn.close()


def test_client_store_is_reduced_no_evidence_ref(tmp_path: Path) -> None:
    """The reduced client DDL drops evidence_ref (gate finding F-2d)."""
    db = tmp_path / "ledger_client_clientco.db"
    init_client_db(db)
    conn = sqlite3.connect(db)
    try:
        tables = _tables(conn)
        assert {"ledger_client", "cost_build", "cost_project", "cost_tenant"} <= tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ledger_client)").fetchall()}
        assert "evidence_ref" not in cols
    finally:
        conn.close()
