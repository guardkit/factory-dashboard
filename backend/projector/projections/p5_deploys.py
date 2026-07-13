"""P5 — deploy / QA-verdict / live-gate projection (the back-half last-mile, G-01).

The fifth projection MODULE in the `p1_agents`/`p2_builds`/`p3_stage`/`p4_approvals`
grammar. It closes gap G-01: forge emits the deploy/QA/live-gate receipts on
`deploy.>` (nats-core 0.7.0 `_deploy.py`, WS2 B7) but nothing consumed them — the
endpoint's back-half published into the void. This module subscribes them (plain
core-NATS, fence 1 — no JetStream consumer on any stream) into the read model's
already-shaped §4.10 sinks (`deploys`, `live_verdicts`).

Contract discipline (one-contract law): fields are read off the decoded envelope
payload by their nats-core `_deploy.py` names VERBATIM — never re-declared here,
exactly as p2/p3/p4 read their payload dicts. The two pinned naming exceptions the
deploy contract carries (`_deploy.py` header §7) are honoured: `feat_id` (⇐ envelope
`feature_id`, the work-item join key) and `env_id` (⇐ `target_env`, the deploy
target). A deploy has no identity of its own beyond its forge run id, so the
`deploys` PK `deploy_id` is fed from the payload's `deploy_run_id` and every subject
is keyed on `correlation_id` (the build/feature spine).

Event types (envelope `EventType`, nats-core `envelope.py`): `deploy_queued`,
`deploy_started`, `deploy_complete`, `deploy_failed`, `qa_verdict`,
`live_gate_result`. `deploy_reverted` is not yet a declared payload (the O-32
revert-on-gate-fail receipt is a named-future) — it is handled defensively on the
same correlation spine so the timeline carries a rollback the moment a producer
emits one, without inventing a shape beyond that spine.

Panel channel: the SSE/change_log panel id is `p8` — the design doc's "P8/deploy"
free slot (`wire-consumer-requirements` A-1/A-3). It is deliberately NOT `p5`: that
change_log panel is the *planning* display panel and is in `DEFAULT_PANELS`, so
emitting `p5` here would wrongly notify planning-panel subscribers on every deploy
event. `p8` is outside `DEFAULT_PANELS`, so the notification is inert until a deploy
UI panel + its `DEFAULT_PANELS` registration land (the IN-3-gated follow-on), and
`/fragments/{panel}` 404s it meanwhile — no broken route.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from backend.projector.projections.base import ChangeSet, Envelope, iso, parse_dt

_PANEL = "p8"

# Envelope event_type -> the deploy-lifecycle status written to `deploys.status`.
_DEPLOY_STATUS_BY_EVENT: dict[str, str] = {
    "deploy_queued": "QUEUED",
    "deploy_started": "STARTED",
    "deploy_complete": "COMPLETE",
    "deploy_failed": "FAILED",
    "deploy_reverted": "REVERTED",  # O-32 named-future receipt; handled when present
}

# The verdict-family event types + the source tag that distinguishes the two rows
# that share a (correlation_id, run_id, attempt) spine (overall QA verdict vs the
# per-run live-gate detail) so they never collide on the `live_verdicts` PK.
_VERDICT_SOURCE_BY_EVENT: dict[str, str] = {
    "qa_verdict": "qa",
    "live_gate_result": "live-gate",
}


def project(conn: sqlite3.Connection, subject: str, env: Envelope, now: datetime) -> ChangeSet:
    """Project one `deploy.>` message. Dispatches the verdict family to `live_verdicts`
    and the deploy-lifecycle family to `deploys`; anything else on the namespace is a
    watermark-only no-op (an unknown-but-decodable future event still counts as flow)."""
    event = env.event_type
    if event in _VERDICT_SOURCE_BY_EVENT:
        return _project_verdict(conn, _VERDICT_SOURCE_BY_EVENT[event], env)
    status = _DEPLOY_STATUS_BY_EVENT.get(event)
    if status is not None:
        return _project_deploy(conn, status, env)
    return ChangeSet()


def _project_deploy(conn: sqlite3.Connection, status: str, env: Envelope) -> ChangeSet:
    p = env.payload
    # A deploy has no identity of its own beyond its forge run id (deploy_run_id);
    # fall back to the correlation spine if a producer omits it.
    correlation_id = env.correlation_id or _opt_str(p.get("correlation_id"))
    deploy_id = _opt_str(p.get("deploy_run_id")) or correlation_id
    if not deploy_id:
        return ChangeSet()

    feature_id = _opt_str(p.get("feat_id"))  # ⇐ envelope feature_id (contract §7 exception)
    event_at = _event_at(p, ("queued_at", "started_at", "completed_at", "failed_at"), env)

    cols: dict[str, object | None] = {
        "feature_id": feature_id,
        "env_id": _opt_str(p.get("env_id")),  # ⇐ target_env (contract §7 exception)
        "status": status,
        "correlation_id": correlation_id,
        "event_at": event_at,
    }
    if p.get("artifact_digest") is not None:
        cols["artifact_digest"] = _opt_str(p.get("artifact_digest"))
    if p.get("image_digests") is not None:
        cols["image_digests"] = json.dumps(p.get("image_digests"))
    if p.get("deploy_record_ref") is not None:
        cols["deploy_record_ref"] = _opt_str(p.get("deploy_record_ref"))
    if p.get("failed_step") is not None:
        cols["failed_step"] = _opt_str(p.get("failed_step"))
    if p.get("duration_seconds") is not None:
        cols["duration_seconds"] = _int(p.get("duration_seconds"))

    _upsert_deploy(conn, deploy_id, cols)
    return ChangeSet(panels={_PANEL}, scope_keys=[feature_id or correlation_id or deploy_id],
                     event_at=env.timestamp)


def _project_verdict(conn: sqlite3.Connection, source: str, env: Envelope) -> ChangeSet:
    p = env.payload
    correlation_id = env.correlation_id or _opt_str(p.get("correlation_id"))
    run_id = _opt_str(p.get("run_id"))
    attempt = _int(p.get("attempt"))
    # Stable PK per (source, correlation, run, attempt): QA verdict and live-gate
    # detail sit in the same table without colliding; idempotent on replay.
    verdict_id = f"{source}:{correlation_id or '?'}:{run_id or '?'}:{attempt if attempt is not None else '?'}"
    event_at = _event_at(p, ("decided_at", "finished_at"), env)

    conn.execute(
        """INSERT INTO live_verdicts (verdict_id, feature_id, verdict, gate_ids, assertions_json,
                                      evidence_index_ref, app_url, attempt, run_id, event_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(verdict_id) DO UPDATE SET
               feature_id=excluded.feature_id, verdict=excluded.verdict, gate_ids=excluded.gate_ids,
               assertions_json=excluded.assertions_json, evidence_index_ref=excluded.evidence_index_ref,
               app_url=excluded.app_url, attempt=excluded.attempt, run_id=excluded.run_id,
               event_at=excluded.event_at""",
        (
            verdict_id,
            _opt_str(p.get("feat_id")),
            _opt_str(p.get("verdict")),
            _json_or_none(p.get("gate_ids")),
            _json_or_none(p.get("assertions")),
            _opt_str(p.get("evidence_index_ref")),
            _opt_str(p.get("app_url")),
            attempt,
            run_id,
            event_at,
        ),
    )
    scope = _opt_str(p.get("feat_id")) or correlation_id or verdict_id
    return ChangeSet(panels={_PANEL}, scope_keys=[scope], event_at=env.timestamp)


def _upsert_deploy(conn: sqlite3.Connection, deploy_id: str, cols: dict[str, object | None]) -> None:
    present = {k: v for k, v in cols.items() if v is not None}
    if conn.execute("SELECT 1 FROM deploys WHERE deploy_id=?", (deploy_id,)).fetchone() is None:
        keys = ["deploy_id", *present.keys()]
        conn.execute(
            f"INSERT INTO deploys ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
            (deploy_id, *present.values()),
        )
        return
    assignments = ", ".join(f"{k}=?" for k in present)
    conn.execute(f"UPDATE deploys SET {assignments} WHERE deploy_id=?", (*present.values(), deploy_id))


def _event_at(p: dict[str, object], keys: tuple[str, ...], env: Envelope) -> str:
    for key in keys:
        dt = parse_dt(p.get(key))
        if dt is not None:
            return iso(dt)
    return iso(env.timestamp)


def _json_or_none(v: object) -> str | None:
    return None if v is None else json.dumps(v)


def _opt_str(v: object) -> str | None:
    return None if v is None else str(v)


def _int(v: object) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str) and v.lstrip("-").isdigit():
        return int(v)
    return None
