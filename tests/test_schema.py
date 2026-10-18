"""The store's schema applies (twice, from either container), holds the copy of Forge's record,
and the reduced client DDL stays reduced."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.config_loader import load_tenants
from backend.db import apply_schema, init_client_db, init_db

CONFIG = Path(__file__).resolve().parent / "fixtures" / "tenants_test.yaml"

EXPECTED_TABLES = {
    "tenants", "users", "change_log", "service_health", "mirror_status", "plan_milestones",
    "forge_builds", "forge_stage_log", "forge_planning_runs", "forge_planning_events", "forge_queue",
    "forge_missions", "spend_calls", "cost_feed_status",
}
# The July bus-fed tables are gone: Forge's record replaced them. The July cost tables, never fed,
# are replaced by the copy of LiteLLM's spend log (part B).
REMOVED = {"builds", "stage_events", "approvals", "issues", "ledger", "agents", "consumer_watermarks", "norms",
           "usage_gateway", "usage_frontier", "cost_rollups"}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_schema_applies_from_either_container_in_either_order(tmp_path: Path) -> None:
    db = tmp_path / "rm.db"
    apply_schema(db)                                  # the feed starts first
    init_db(db, load_tenants(CONFIG).values())        # then the web container
    apply_schema(db)                                  # and the feed restarts
    conn = sqlite3.connect(db)
    try:
        tables = _tables(conn)
        assert tables >= EXPECTED_TABLES
        assert not tables & REMOVED
    finally:
        conn.close()


def test_mission_columns_are_copied(tmp_path: Path) -> None:
    db = tmp_path / "rm.db"
    apply_schema(db)
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(forge_builds)")}
        assert {"mission_id", "mission_role", "correlation_id", "feature_id"} <= cols
    finally:
        conn.close()


def test_plan_milestones_starts_empty(tmp_path: Path) -> None:
    db = tmp_path / "rm.db"
    init_db(db, load_tenants(CONFIG).values())
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM plan_milestones").fetchone()[0] == 0
    finally:
        conn.close()


def test_client_store_is_reduced_no_evidence_ref(tmp_path: Path) -> None:
    db = tmp_path / "ledger_client_clientco.db"
    init_client_db(db)
    conn = sqlite3.connect(db)
    try:
        assert {"ledger_client", "cost_build", "cost_project", "cost_tenant"} <= _tables(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ledger_client)").fetchall()}
        assert "evidence_ref" not in cols
    finally:
        conn.close()
