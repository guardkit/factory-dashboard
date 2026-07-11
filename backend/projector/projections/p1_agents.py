"""P1 — agent roster projection (dual-source, capability note drift 3/4).

Subjects (plain core-NATS subscribe): `fleet.register` (AgentManifest), `fleet.heartbeat.{id}`
(AgentHeartbeatPayload), `fleet.deregister` (AgentDeregistrationPayload), AND a subject-space read
of the KV bucket: `$KV.agent-registry.>` (each re-put's subject key is the agent id; the body is
the registry value). The KV read is a plain subscribe — NEVER a KV/JetStream API call.

`source_kind` keeps the roster honest: `fleet` (subject heartbeats), `kv` (KV re-put freshness —
queue/tasks unmeasured), `register_only` (registered but no heartbeat feed — forge, ask A-10:
rendered "no heartbeat feed", never a fake stale-alarm).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from backend.projector.projections.base import ChangeSet, Envelope, iso, parse_dt

_PANEL = "p1"


def _upsert(
    conn: sqlite3.Connection,
    agent_id: str,
    *,
    name: str | None = None,
    status: str | None = None,
    queue_depth: int | None = None,
    active_tasks: int | None = None,
    uptime_seconds: int | None = None,
    last_heartbeat_at: str | None = None,
    deregistered_at: str | None = None,
    source_kind: str | None = None,
    manifest_json: str | None = None,
    upgrade_source: bool = False,
) -> None:
    existing = conn.execute("SELECT source_kind FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO agents (agent_id, name, status, queue_depth, active_tasks, uptime_seconds,
                                   last_heartbeat_at, deregistered_at, source_kind, manifest_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_id, name, status, queue_depth, active_tasks, uptime_seconds,
             last_heartbeat_at, deregistered_at, source_kind, manifest_json),
        )
        return
    # COALESCE keeps prior values when a partial update arrives; source_kind only ever UPGRADES
    # to 'fleet'/'kv' (a live feed) — never downgrades a heartbeating agent back to register_only.
    sk_clause = "source_kind=?" if (source_kind and upgrade_source) else "source_kind=COALESCE(?, source_kind)"
    conn.execute(
        f"""UPDATE agents SET
               name=COALESCE(?, name),
               status=COALESCE(?, status),
               queue_depth=COALESCE(?, queue_depth),
               active_tasks=COALESCE(?, active_tasks),
               uptime_seconds=COALESCE(?, uptime_seconds),
               last_heartbeat_at=COALESCE(?, last_heartbeat_at),
               deregistered_at=COALESCE(?, deregistered_at),
               {sk_clause},
               manifest_json=COALESCE(?, manifest_json)
           WHERE agent_id=?""",
        (name, status, queue_depth, active_tasks, uptime_seconds, last_heartbeat_at,
         deregistered_at, source_kind, manifest_json, agent_id),
    )


def project(conn: sqlite3.Connection, subject: str, env: Envelope, now: datetime) -> ChangeSet:
    """Project a fleet.* envelope into the roster."""
    p = env.payload
    agent_id = str(p.get("agent_id") or env.source_id or "")
    if not agent_id:
        return ChangeSet()
    if subject.startswith("fleet.register") or env.event_type == "agent_register":
        _upsert(
            conn, agent_id,
            name=_opt_str(p.get("name")),
            status=_opt_str(p.get("status")),
            source_kind="register_only",
            manifest_json=json.dumps(p),
        )
    elif subject.startswith("fleet.heartbeat") or env.event_type == "agent_heartbeat":
        _upsert(
            conn, agent_id,
            status=_opt_str(p.get("status")),
            queue_depth=_opt_int(p.get("queue_depth")),
            active_tasks=_opt_int(p.get("active_tasks")),
            uptime_seconds=_opt_int(p.get("uptime_seconds")),
            last_heartbeat_at=iso(env.timestamp),
            source_kind="fleet",
            upgrade_source=True,
        )
    elif subject.startswith("fleet.deregister") or env.event_type == "agent_deregister":
        _upsert(conn, agent_id, deregistered_at=iso(env.timestamp))
    else:
        return ChangeSet()
    return ChangeSet(panels={_PANEL}, scope_keys=[agent_id], event_at=env.timestamp)


def project_kv(conn: sqlite3.Connection, subject: str, raw: bytes | str, now: datetime) -> ChangeSet:
    """Project a `$KV.agent-registry.<key>` re-put. The subject key is the agent id; the body is
    the KV value (JSON when present). Liveness = re-put freshness (`now`); queue/tasks unmeasured."""
    agent_id = subject.rsplit(".", 1)[-1]
    if not agent_id:
        return ChangeSet()
    value: dict[str, object] = {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            value = parsed
    except (ValueError, TypeError):
        value = {}
    _upsert(
        conn, agent_id,
        name=_opt_str(value.get("name")) or agent_id,
        status=_opt_str(value.get("status")) or "kv",
        last_heartbeat_at=iso(now),  # KV re-put freshness is the liveness a KV agent claims
        source_kind="kv",
        upgrade_source=True,
        manifest_json=json.dumps(value) if value else None,
    )
    return ChangeSet(panels={_PANEL}, scope_keys=[agent_id], event_at=now)


def _opt_str(v: object) -> str | None:
    return None if v is None else str(v)


def _opt_int(v: object) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.lstrip("-").isdigit():
        return int(v)
    return None


def parse_optional_dt(v: object) -> datetime | None:  # re-export convenience for callers/tests
    return parse_dt(v)
