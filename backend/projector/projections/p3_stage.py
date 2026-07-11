"""P3 — stage & gate board projection (capability note drift 5).

`pipeline.stage-complete` (StageCompletePayload) → `stage_events`. GATED state is DERIVED from
`StageComplete.gate_mode` + the forge `stage_log` backfill (forge_mirror) — there is NO
`pipeline.stage-gated` producer anywhere; if one ever fires, its StageGatedPayload DIVERGES
(`stage` not `stage_label`; 2-value lowercase gate_mode; no status/duration_secs) and is parsed
against those field names. `coach_score` is operator-view only (DF-008) — it never leaves this store.

A HARD_STOP / FAILED stage manufactures a `gate_rejected` issue for the Q3 register + the P4
needs-you strip (design §1 Q3).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from backend.projector.projections.base import ChangeSet, Envelope, iso, parse_dt

_PANEL = "p3"

# gate_mode values that mean "the gate stopped or flagged this stage" (StageComplete literal).
_STOPPING_GATE_MODES = {"HARD_STOP", "MANDATORY_HUMAN_APPROVAL"}


def project(conn: sqlite3.Connection, subject: str, env: Envelope, now: datetime) -> ChangeSet:
    p = env.payload
    feature_id = str(p.get("feature_id") or "")
    if not feature_id:
        return ChangeSet()

    gate_mode: str | None
    coach_score: float | None
    duration: float | None
    if env.event_type == "stage_gated":
        # Divergent contract (drift 5): `stage`, lowercase 2-value gate_mode, no status/duration.
        stage_label = str(p.get("stage") or "")
        status = "GATED"
        gate_mode = str(p.get("gate_mode") or "")
        coach_score = None
        duration = None
        completed = iso(env.timestamp)
    else:  # stage_complete
        stage_label = str(p.get("stage_label") or "")
        status = str(p.get("status") or "")
        gate_mode = _opt_str(p.get("gate_mode"))
        coach_score = _float(p.get("coach_score"))
        duration = _float(p.get("duration_secs"))
        completed = _dt(p.get("completed_at")) or iso(env.timestamp)

    build_id = _opt_str(p.get("build_id"))
    target_kind = _opt_str(p.get("target_kind"))

    # Idempotent on replay: one row per (feature, stage, completed_at, status).
    dup = conn.execute(
        "SELECT 1 FROM stage_events WHERE feature_id=? AND stage_label=? AND completed_at=? AND status=?",
        (feature_id, stage_label, completed, status),
    ).fetchone()
    if dup is None:
        conn.execute(
            """INSERT INTO stage_events (build_id, feature_id, stage_label, target_kind, status,
                                         gate_mode, coach_score, duration_secs, completed_at, origin)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'bus')""",
            (build_id, feature_id, stage_label, target_kind, status, gate_mode, coach_score,
             duration, completed),
        )

    panels = {_PANEL}
    if status == "FAILED" or (gate_mode or "") in _STOPPING_GATE_MODES:
        _manufacture_gate_rejected(conn, feature_id, stage_label, coach_score, completed)
        panels.add("p4")

    return ChangeSet(panels=panels, scope_keys=[feature_id], event_at=env.timestamp)


def _manufacture_gate_rejected(
    conn: sqlite3.Connection, feature_id: str, stage_label: str, coach_score: float | None, at: str
) -> None:
    issue_id = f"gate:{feature_id}:{stage_label}"
    score = f" · {coach_score:.2f}" if coach_score is not None else ""
    conn.execute(
        """INSERT INTO issues (issue_id, scope_type, scope_id, kind, opened_at, closed_at, detail, source_ref)
           VALUES (?, 'feature', ?, 'gate_rejected', ?, NULL, ?, ?)
           ON CONFLICT(issue_id) DO UPDATE SET opened_at=excluded.opened_at, detail=excluded.detail""",
        (issue_id, feature_id, at, f"{stage_label} FAILED{score}", "stage_log"),
    )


def _opt_str(v: object) -> str | None:
    return None if v is None else str(v)


def _float(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _dt(v: object) -> str | None:
    dt = parse_dt(v)
    return iso(dt) if dt else None
