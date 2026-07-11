"""P4 — approvals & escalations projection (capability note drift 7).

`agents.approval.{agent_id}.{task_id}` (ApprovalRequestPayload) and `.response`
(ApprovalResponsePayload) → `approvals`, keyed on `request_id` (NOT `slot_id` — that column is
vestigial/unfed, drift 7). An open request manufactures an `approval_waiting` issue whose age
drives the P4 needs-you strip and the §1 age bands; the response closes it. The decision enum is
approve|reject|defer|override. This dashboard OBSERVES — it never writes a decision (ADR-DASH-007).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from backend.projector.projections.base import ChangeSet, Envelope, iso, parse_dt

_PANEL = "p4"


def project(conn: sqlite3.Connection, subject: str, env: Envelope, now: datetime) -> ChangeSet:
    p = env.payload
    request_id = str(p.get("request_id") or "")
    if not request_id:
        return ChangeSet()
    is_response = subject.endswith(".response") or env.event_type == "approval_response"
    if is_response:
        return _project_response(conn, request_id, p, env)
    return _project_request(conn, request_id, p, env)


def _project_request(conn: sqlite3.Connection, request_id: str, p: dict[str, object], env: Envelope) -> ChangeSet:
    agent_id = _opt_str(p.get("agent_id"))
    action = _opt_str(p.get("action_description")) or ""
    risk = _opt_str(p.get("risk_level"))
    timeout = _int(p.get("timeout_seconds"))
    requested_at = iso(env.timestamp)
    conn.execute(
        """INSERT INTO approvals (request_id, agent_id, slot_id, action_description, risk_level,
                                  requested_at, timeout_seconds, decision, decided_by, decided_at,
                                  latency_seconds, state)
           VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, NULL, NULL, NULL, 'open')
           ON CONFLICT(request_id) DO UPDATE SET
               agent_id=excluded.agent_id, action_description=excluded.action_description,
               risk_level=excluded.risk_level, requested_at=excluded.requested_at,
               timeout_seconds=excluded.timeout_seconds""",
        (request_id, agent_id, action, risk, requested_at, timeout),
    )
    detail = f'{agent_id or "agent"} · "{action}"'
    if risk:
        detail += f" · risk:{risk}"
    conn.execute(
        """INSERT INTO issues (issue_id, scope_type, scope_id, kind, opened_at, closed_at, detail, source_ref)
           VALUES (?, 'agent', ?, 'approval_waiting', ?, NULL, ?, ?)
           ON CONFLICT(issue_id) DO UPDATE SET
               opened_at=excluded.opened_at, detail=excluded.detail, closed_at=NULL""",
        (f"approval:{request_id}", agent_id, requested_at, detail, f"approvals/{request_id}"),
    )
    return ChangeSet(panels={_PANEL}, scope_keys=[agent_id or request_id], event_at=env.timestamp)


def _project_response(conn: sqlite3.Connection, request_id: str, p: dict[str, object], env: Envelope) -> ChangeSet:
    decision = _opt_str(p.get("decision"))
    decided_by = _opt_str(p.get("decided_by"))
    decided_at = iso(env.timestamp)
    row = conn.execute("SELECT requested_at FROM approvals WHERE request_id=?", (request_id,)).fetchone()
    latency: int | None = None
    if row is not None and row[0]:
        req_dt = parse_dt(row[0])
        if req_dt is not None:
            latency = int((env.timestamp - req_dt).total_seconds())
    conn.execute(
        """UPDATE approvals SET decision=?, decided_by=?, decided_at=?, latency_seconds=?, state='decided'
           WHERE request_id=?""",
        (decision, decided_by, decided_at, latency, request_id),
    )
    # If we never saw the request (started mid-stream), record the decided approval anyway.
    if conn.execute("SELECT changes()").fetchone()[0] == 0:
        conn.execute(
            """INSERT OR IGNORE INTO approvals (request_id, decision, decided_by, decided_at, state)
               VALUES (?, ?, ?, ?, 'decided')""",
            (request_id, decision, decided_by, decided_at),
        )
    conn.execute("UPDATE issues SET closed_at=? WHERE issue_id=?", (decided_at, f"approval:{request_id}"))
    return ChangeSet(panels={_PANEL}, scope_keys=[request_id], event_at=env.timestamp)


def _opt_str(v: object) -> str | None:
    return None if v is None else str(v)


def _int(v: object) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.lstrip("-").isdigit():
        return int(v)
    return None
