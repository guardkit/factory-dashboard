"""S2 (ux §9.2): fixture event streams → P1–P6 project correctly; dual-source roster; wave-only
P2; watermarks advance; and one end-to-end pass through an EPHEMERAL local nats-server."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import nats
from backend.projector.consumers import Projector, apply_message

from tests.nats_util import envelope, make_projected_db, nats_server  # noqa: F401

NOW = datetime(2026, 7, 11, 14, 13, 34, tzinfo=UTC)


# --- P1 roster is dual-source (drift 3/4) ------------------------------------


def test_roster_dual_source_fleet_kv_and_register_only(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    # a heartbeating fleet agent, a KV-only agent, and a register-only agent (no heartbeat feed)
    apply_message(conn, "fleet.heartbeat.forge-orch",
                  envelope("agent_heartbeat", {"agent_id": "forge-orch", "status": "ready", "queue_depth": 2}), NOW)
    apply_message(conn, "$KV.agent-registry.jarvis", b'{"name": "jarvis", "status": "ready"}', NOW)
    apply_message(conn, "fleet.register",
                  envelope("agent_register", {"agent_id": "forge", "name": "forge"}), NOW)

    rows = {r[0]: r for r in conn.execute("SELECT agent_id, source_kind, queue_depth FROM agents")}
    assert rows["forge-orch"][1] == "fleet" and rows["forge-orch"][2] == 2
    assert rows["jarvis"][1] == "kv"
    assert rows["forge"][1] == "register_only"


def test_heartbeat_upgrades_register_only_to_fleet(tmp_path: Path) -> None:
    """A register then a heartbeat: source_kind UPGRADES to fleet — never downgrades a live feed."""
    _db, conn = make_projected_db(tmp_path)
    apply_message(conn, "fleet.register", envelope("agent_register", {"agent_id": "a1", "name": "a1"}), NOW)
    apply_message(conn, "fleet.heartbeat.a1", envelope("agent_heartbeat", {"agent_id": "a1", "status": "busy"}), NOW)
    sk = conn.execute("SELECT source_kind FROM agents WHERE agent_id='a1'").fetchone()[0]
    assert sk == "fleet"


# --- P2 in-flight is WAVE-ONLY (drift 6) -------------------------------------


def test_build_progress_is_wave_only_no_task_counters(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    apply_message(conn, "pipeline.build-started.FEAT-51B0",
                  envelope("build_started", {"feature_id": "FEAT-51B0", "build_id": "b1", "wave_total": 6}), NOW)
    apply_message(conn, "pipeline.build-progress.FEAT-51B0",
                  envelope("build_progress", {"feature_id": "FEAT-51B0", "build_id": "b1",
                                              "wave": 4, "wave_total": 6, "overall_progress_pct": 64.0,
                                              "elapsed_seconds": 2280}), NOW)
    row = conn.execute(
        "SELECT status, current_wave, wave_total, progress_pct, tasks_total, tasks_completed "
        "FROM builds WHERE build_id='b1'"
    ).fetchone()
    assert row[0] == "RUNNING" and row[1] == 4 and row[2] == 6 and row[3] == 64.0
    assert row[4] is None and row[5] is None  # task counters absent mid-build (BuildProgress reality)


def test_build_complete_carries_task_counters(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    apply_message(conn, "pipeline.build-complete.FEAT-3ED2",
                  envelope("build_complete", {"feature_id": "FEAT-3ED2", "build_id": "b2",
                                              "tasks_completed": 11, "tasks_failed": 0, "tasks_total": 11,
                                              "duration_seconds": 4495, "summary": "done", "pr_url": None}), NOW)
    row = conn.execute("SELECT status, tasks_total, tasks_failed FROM builds WHERE build_id='b2'").fetchone()
    assert row == ("COMPLETE", 11, 0)


# --- P3 stage / gate ---------------------------------------------------------


def test_stage_complete_projects_and_is_idempotent(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    body = envelope("stage_complete", {"feature_id": "FEAT-3ED2", "build_id": "b1", "stage_label": "build-6",
                                       "status": "PASSED", "gate_mode": "AUTO_APPROVE", "coach_score": 0.91,
                                       "duration_secs": 12.0, "completed_at": "2026-07-11T14:13:00+00:00"})
    apply_message(conn, "pipeline.stage-complete.FEAT-3ED2", body, NOW)
    apply_message(conn, "pipeline.stage-complete.FEAT-3ED2", body, NOW)  # replay
    n = conn.execute("SELECT COUNT(*) FROM stage_events WHERE feature_id='FEAT-3ED2'").fetchone()[0]
    assert n == 1  # de-duped on (feature, stage, completed_at, status)


def test_failed_stage_manufactures_gate_rejected_issue(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    apply_message(conn, "pipeline.stage-complete.FEAT-9A21",
                  envelope("stage_complete", {"feature_id": "FEAT-9A21", "build_id": "b3", "stage_label": "build-3",
                                              "status": "FAILED", "gate_mode": "HARD_STOP", "coach_score": 0.42,
                                              "duration_secs": 5.0, "completed_at": "2026-07-11T13:12:00+00:00"}), NOW)
    kind = conn.execute("SELECT kind FROM issues WHERE scope_id='FEAT-9A21' AND closed_at IS NULL").fetchone()[0]
    assert kind == "gate_rejected"


# --- P4 approvals key on request_id (drift 7) --------------------------------


def test_approval_request_then_response(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    apply_message(conn, "agents.approval.forge.task-9",
                  envelope("approval_request", {"request_id": "req-1", "agent_id": "forge",
                                                "action_description": "apply migration", "risk_level": "high",
                                                "timeout_seconds": 3600}), NOW)
    assert conn.execute("SELECT state FROM approvals WHERE request_id='req-1'").fetchone()[0] == "open"
    assert conn.execute("SELECT closed_at FROM issues WHERE issue_id='approval:req-1'").fetchone()[0] is None

    apply_message(conn, "agents.approval.forge.task-9.response",
                  envelope("approval_response",
                           {"request_id": "req-1", "decision": "approve", "decided_by": "rich"}), NOW)
    row = conn.execute("SELECT state, decision, decided_by FROM approvals WHERE request_id='req-1'").fetchone()
    assert row == ("decided", "approve", "rich")
    assert conn.execute("SELECT closed_at FROM issues WHERE issue_id='approval:req-1'").fetchone()[0] is not None


# --- watermarks + change log -------------------------------------------------


def test_watermarks_advance_and_change_log_appends(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    apply_message(conn, "pipeline.build-started.FEAT-1",
                  envelope("build_started", {"feature_id": "FEAT-1", "build_id": "b1", "wave_total": 3}), NOW)
    apply_message(conn, "pipeline.build-progress.FEAT-1",
                  envelope("build_progress", {"feature_id": "FEAT-1", "build_id": "b1",
                                              "wave": 2, "wave_total": 3, "overall_progress_pct": 40.0,
                                              "elapsed_seconds": 60}), NOW)
    wm = conn.execute(
        "SELECT last_stream_seq, last_event_at FROM consumer_watermarks WHERE stream='PIPELINE'"
    ).fetchone()
    assert wm[0] == 2 and wm[1] is not None
    changes = conn.execute("SELECT COUNT(*) FROM change_log WHERE panel='p2'").fetchone()[0]
    assert changes == 2


def test_malformed_message_never_crashes(tmp_path: Path) -> None:
    _db, conn = make_projected_db(tmp_path)
    apply_message(conn, "pipeline.build-started.X", b"not json at all", NOW)
    # nothing projected, but the stream watermark still advances (an event flowed)
    assert conn.execute("SELECT last_stream_seq FROM consumer_watermarks WHERE stream='PIPELINE'").fetchone()[0] == 1


# --- end-to-end through the ephemeral local nats-server ----------------------


def test_end_to_end_through_ephemeral_nats(tmp_path: Path, nats_server: str) -> None:  # noqa: F811
    db_path, conn = make_projected_db(tmp_path)

    async def scenario() -> None:
        projector = Projector(db_path, nats_server)
        await projector.connect()
        pub = await nats.connect(nats_server)
        await pub.publish("fleet.heartbeat.forge-orch",
                          envelope("agent_heartbeat", {"agent_id": "forge-orch", "status": "ready", "queue_depth": 5}))
        await pub.publish("$KV.agent-registry.jarvis", b'{"name": "jarvis", "status": "ready"}')
        await pub.publish("pipeline.build-progress.FEAT-51B0",
                          envelope("build_progress", {"feature_id": "FEAT-51B0", "build_id": "b9",
                                                      "wave": 4, "wave_total": 6, "overall_progress_pct": 64.0,
                                                      "elapsed_seconds": 10}))
        await pub.flush()
        await asyncio.sleep(0.4)
        await pub.close()
        await projector.close()

    asyncio.run(scenario())
    # a fresh mode=ro reader sees what the projector wrote
    agents = {r[0]: r[1] for r in conn.execute("SELECT agent_id, source_kind FROM agents")}
    assert agents.get("forge-orch") == "fleet"
    assert agents.get("jarvis") == "kv"
    assert conn.execute("SELECT current_wave FROM builds WHERE build_id='b9'").fetchone()[0] == 4
