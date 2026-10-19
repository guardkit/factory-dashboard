"""A small stand-in for Forge's record (its own table and column names), for tests.

`ForgeFake` writes rows the way Forge does; `store_from(fake)` copies it into a dashboard store
with the real feed code, so tests run the same path production does.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend import db
from backend.config_loader import load_tenants
from backend.projector.forge_mirror import mirror_once
from backend.projector.store import iso, open_rw, write_heartbeat

FORGE_SCHEMA = """
CREATE TABLE builds (
    build_id TEXT PRIMARY KEY, feature_id TEXT NOT NULL, repo TEXT NOT NULL, branch TEXT NOT NULL,
    feature_yaml_path TEXT NOT NULL DEFAULT '', project TEXT, status TEXT NOT NULL,
    triggered_by TEXT NOT NULL DEFAULT 'jarvis', originating_adapter TEXT, originating_user TEXT,
    correlation_id TEXT NOT NULL, parent_request_id TEXT, queued_at TEXT NOT NULL, started_at TEXT,
    completed_at TEXT, worktree_path TEXT, pr_url TEXT, error TEXT, terminal_class TEXT,
    mission_id TEXT, mission_role TEXT, pending_approval_request_id TEXT, target_branch TEXT);
CREATE TABLE publication_records (
    build_id TEXT PRIMARY KEY, feature_id TEXT, repo TEXT, decided_by TEXT, decided_at TEXT,
    target_branch TEXT, g_commit TEXT, build_tip TEXT, j_commit TEXT, attempt INTEGER NOT NULL DEFAULT 0,
    result TEXT, checked_json TEXT, lease_holder TEXT, lease_expires_at TEXT, turn INTEGER NOT NULL DEFAULT 0,
    lines_json TEXT NOT NULL DEFAULT '[]', created_at TEXT, updated_at TEXT);
CREATE TABLE stage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, build_id TEXT NOT NULL, stage_label TEXT NOT NULL,
    target_kind TEXT NOT NULL DEFAULT 'local_tool', target_identifier TEXT NOT NULL, status TEXT NOT NULL,
    gate_mode TEXT, coach_score REAL, threshold_applied REAL, started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL, duration_secs REAL NOT NULL DEFAULT 0, details_json TEXT NOT NULL);
CREATE TABLE planning_runs (
    correlation_id TEXT PRIMARY KEY, state TEXT NOT NULL, originating_user TEXT NOT NULL,
    expected_approver TEXT NOT NULL, request_text TEXT NOT NULL, target_repo TEXT,
    triggered_by TEXT NOT NULL DEFAULT 'jarvis', defer_count INTEGER NOT NULL DEFAULT 0,
    paused_at TEXT, escalated_at TEXT, handoff_branch TEXT, handoff_path TEXT,
    queued_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, error TEXT,
    pending_approval_request_id TEXT, parent_request_id TEXT);
CREATE TABLE planning_run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, correlation_id TEXT NOT NULL, stage_label TEXT NOT NULL,
    status TEXT NOT NULL, gate_mode TEXT, coach_score REAL, actor_identity TEXT, details_json TEXT,
    recorded_at TEXT NOT NULL);
CREATE TABLE work_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sentence TEXT NOT NULL, target_repo TEXT, kind TEXT NOT NULL,
    status TEXT NOT NULL, rank REAL NOT NULL DEFAULT 0, originating_user TEXT NOT NULL DEFAULT 'U1',
    correlation_id TEXT NOT NULL UNIQUE, queued_at TEXT NOT NULL, admitted_at TEXT, closed_at TEXT,
    closed_reason TEXT);
CREATE TABLE missions (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, target_repo TEXT NOT NULL, branch TEXT NOT NULL,
    status TEXT NOT NULL, correlation_id TEXT NOT NULL UNIQUE, originating_user TEXT NOT NULL DEFAULT 'U1',
    plan_request_id TEXT, created_at TEXT NOT NULL, approved_at TEXT, approved_by TEXT, closed_at TEXT,
    closed_reason TEXT);
CREATE TABLE mission_features (
    mission_id TEXT NOT NULL, key TEXT NOT NULL, sentence TEXT NOT NULL, no_sign_in INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mission_id, key));
CREATE TABLE mission_needs (
    mission_id TEXT NOT NULL, key TEXT NOT NULL, needs_key TEXT NOT NULL, PRIMARY KEY (mission_id, key, needs_key));
"""

T0 = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
REPO = "acme/notes"


def at(minutes: float) -> str:
    return iso(T0 + timedelta(minutes=minutes))


class ForgeFake:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(FORGE_SCHEMA)

    # --- planning ---------------------------------------------------------------------------
    def planning(self, corr: str, text: str, *, state: str = "BUILD_QUEUED", start: float = 0,
                 end: float | None = 20, error: str | None = None, repo: str = REPO) -> None:
        self.conn.execute(
            "INSERT INTO planning_runs (correlation_id, state, originating_user, expected_approver, request_text, "
            "target_repo, queued_at, started_at, completed_at, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (corr, state, "U1", "U1", text, repo, at(start), at(start), at(end) if end is not None else None, error),
        )

    def event(self, corr: str, label: str, status: str, minute: float,
              details: dict[str, object] | None = None, actor: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO planning_run_events (correlation_id, stage_label, status, details_json, recorded_at, "
            "actor_identity) VALUES (?,?,?,?,?,?)", (corr, label, status, json.dumps(details or {}), at(minute), actor),
        )

    def queue(self, corr: str, text: str, *, status: str = "DONE", minute: float = -1,
              reason: str | None = None, kind: str = "feature") -> None:
        self.conn.execute(
            "INSERT INTO work_queue (sentence, target_repo, kind, status, correlation_id, queued_at, admitted_at, "
            "closed_at, closed_reason) VALUES (?,?,?,?,?,?,?,?,?)",
            (text, REPO, kind, status, corr, at(minute), at(minute) if status != "QUEUED" else None,
             at(minute + 1) if status in ("DONE", "BLOCKED", "WITHDRAWN") else None, reason),
        )

    # --- builds -------------------------------------------------------------------------------
    def build(self, build_id: str, code: str, corr: str, *, status: str = "COMPLETE", queued: float = 21,
              started: float | None = 21, completed: float | None = 80, error: str | None = None,
              mission_id: str | None = None, mission_role: str | None = None, triggered_by: str = "jarvis",
              target_branch: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO builds (build_id, feature_id, repo, branch, status, triggered_by, correlation_id, "
            "queued_at, started_at, completed_at, error, mission_id, mission_role, target_branch) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (build_id, code, REPO, "main", status, triggered_by, corr, at(queued),
             at(started) if started is not None else None, at(completed) if completed is not None else None,
             error, mission_id, mission_role, target_branch),
        )

    def stage(self, build_id: str, label: str, target: str, status: str, minute: float,
              details: dict[str, object] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO stage_log (build_id, stage_label, target_identifier, status, started_at, completed_at, "
            "details_json) VALUES (?,?,?,?,?,?,?)",
            (build_id, label, target, status, at(minute), at(minute), json.dumps(details or {})),
        )

    def gate_pause(self, build_id: str, minute: float) -> None:
        self.stage(build_id, "autobuild", "autobuild_runner", "GATED", minute, {"gate_pause": {"request_id": "r"}})

    def gate_resume(self, build_id: str, minute: float) -> None:
        self.stage(build_id, "gate-resume", "gate-resume", "PASSED", minute, {"gate_resume": {"request_id": "r"}})

    def work(self, build_id: str, minute: float) -> None:
        self.stage(build_id, "autobuild", "FEAT", "PASSED", minute, {"lifecycle_state": "running"})

    def merge_offer(self, build_id: str, minute: float) -> None:
        self.stage(build_id, "the merge word", "merge_deploy_offer", "GATED", minute,
                   {"merge_offer": {"build_id": build_id}})

    def merge_decision(self, build_id: str, minute: float, decision: str = "approve") -> None:
        status = "PASSED" if decision == "approve" else "SKIPPED"
        self.stage(build_id, "the merge word", "merge_deploy_decision", status, minute,
                   {"merge_decision": {"decision": decision, "decided_by": "U1"}})

    def merged(self, build_id: str, minute: float, *, sha: str = "1e581663e8aa", mission_branch: bool = False) -> None:
        details: dict[str, object] = {"merged_sha": sha, "merge_decision": {"execution_attempt_id": "x"}}
        if mission_branch:
            details["result"] = "published-to-the-mission-branch"
        self.stage(build_id, "merge-deploy", "merge_deploy_executor", "PASSED", minute, details)
        if mission_branch:
            self.publication(build_id, minute, result="published to the mission branch", target="mission/branch",
                             sha=sha)
        else:
            self.publication(build_id, minute, sha=sha)

    def publication(self, build_id: str, minute: float, *, result: str = "merged into the remote and running",
                    target: str | None = "main", sha: str = "1e581663e8aa") -> None:
        """Forge's publication record for a build (one row per build): what the merge word did."""
        self.conn.execute(
            "INSERT OR REPLACE INTO publication_records (build_id, feature_id, repo, decided_by, decided_at, "
            "target_branch, j_commit, result, created_at, updated_at) "
            "SELECT build_id, feature_id, repo, 'U1', ?, ?, ?, ?, ?, ? FROM builds WHERE build_id = ?",
            (at(minute), target, sha, result, at(minute), at(minute), build_id),
        )

    def merge_failed(self, build_id: str, minute: float, detail: str = "the merge stopped on a conflict") -> None:
        self.stage(build_id, "merge-deploy", "merge_deploy_executor", "FAILED", minute, {"detail": detail})

    def mission(self, mid: str, title: str, corr: str, status: str = "RUNNING", closed: float | None = None) -> None:
        self.conn.execute(
            "INSERT INTO missions (id, title, target_repo, branch, status, correlation_id, created_at, closed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (mid, title, REPO, f"mission/{mid}", status, corr, at(0), at(closed) if closed is not None else None),
        )


def delivered_feature(fake: ForgeFake, n: int, *, text: str | None = None, merged_at: float = 90) -> str:
    """A feature that went all the way: planned, built, merge approved, merged and checked."""
    corr, code, bid = f"c-{n}", f"FEAT-{n:04d}", f"build-FEAT-{n:04d}"
    fake.planning(corr, text or f"Feature number {n} does a thing.")
    fake.event(corr, "feature-spec-digest-review", "GATED", 5)
    fake.event(corr, "feature-spec-digest-review", "approved", 6)
    fake.event(corr, "build-queued", "approved", 20)
    fake.build(bid, code, corr)
    fake.merge_offer(bid, 81)
    fake.merge_decision(bid, 82)
    fake.merged(bid, merged_at)
    return code


def store_from(fake: ForgeFake, store: Path, now: datetime | None = None) -> sqlite3.Connection:
    """A dashboard store, filled by the real feed code from the fake record, with a live heartbeat."""
    db.apply_schema(store)
    conn = open_rw(store)
    now = now or datetime.now(UTC)
    write_heartbeat(conn, now)
    mirror_once(conn, fake.path, now)
    return conn


TENANTS_TEST = Path(__file__).resolve().parent / "fixtures" / "tenants_test.yaml"


def make_projected_db(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    """A schema-applied dashboard store and the feed's read-write connection."""
    db_path = tmp_path / "readmodel.db"
    db.init_db(db_path, load_tenants(TENANTS_TEST).values())
    return db_path, open_rw(db_path)
