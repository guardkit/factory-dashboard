"""P2 — build board projection (the build-lifecycle family, capability note drift 6).

`pipeline.build-queued/started/progress/paused/resumed/complete/failed/cancelled` → `builds`.
In-flight progress is WAVE-LEVEL ONLY: `BuildProgressPayload` carries wave / wave_total /
overall_progress_pct / elapsed_seconds and NO task counters — task counts arrive only at
BuildComplete (drift 6). `source='bus'` on every row projected here (forge_sqlite rows come from
the ledger bootstrap at S3). PIPELINE is consumed by plain core-NATS subscribe (fence 1).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from backend.projector.projections.base import ChangeSet, Envelope, iso, parse_dt

_PANEL = "p2"

_STATUS_BY_EVENT: dict[str, str] = {
    "build_queued": "QUEUED",
    "build_started": "RUNNING",
    "build_progress": "RUNNING",
    "build_paused": "PAUSED",
    "build_resumed": "RUNNING",
    "build_complete": "COMPLETE",
    "build_failed": "FAILED",
    "build_cancelled": "CANCELLED",
}


def project(conn: sqlite3.Connection, subject: str, env: Envelope, now: datetime) -> ChangeSet:
    p = env.payload
    feature_id = str(p.get("feature_id") or "")
    if not feature_id:
        return ChangeSet()
    status = _STATUS_BY_EVENT.get(env.event_type)
    if status is None:
        return ChangeSet()
    build_id = str(p.get("build_id") or feature_id)

    cols: dict[str, object | None] = {
        "feature_id": feature_id,
        "status": status,
        "correlation_id": env.correlation_id,  # nullable — guarded upstream (drift 13)
        "source": "bus",
    }
    if env.project:
        cols["project"] = env.project
    if p.get("repo") is not None:
        cols["repo"] = p.get("repo")
    if p.get("branch") is not None:
        cols["branch"] = p.get("branch")
    if p.get("mode") is not None:
        cols["mode"] = p.get("mode")

    if env.event_type == "build_queued":
        cols["queued_at"] = _dt(p.get("queued_at")) or iso(env.timestamp)
    elif env.event_type == "build_started":
        cols["started_at"] = iso(env.timestamp)
        cols["wave_total"] = _int(p.get("wave_total"))
        cols["current_wave"] = 1
        cols["progress_pct"] = 0.0
    elif env.event_type == "build_progress":
        cols["current_wave"] = _int(p.get("wave"))
        cols["wave_total"] = _int(p.get("wave_total"))
        cols["progress_pct"] = _float(p.get("overall_progress_pct"))
        cols["duration_seconds"] = _int(p.get("elapsed_seconds"))
    elif env.event_type == "build_complete":
        cols["completed_at"] = iso(env.timestamp)
        cols["tasks_completed"] = _int(p.get("tasks_completed"))
        cols["tasks_failed"] = _int(p.get("tasks_failed"))
        cols["tasks_total"] = _int(p.get("tasks_total"))
        cols["pr_url"] = p.get("pr_url")
        cols["duration_seconds"] = _int(p.get("duration_seconds"))
        cols["progress_pct"] = 100.0
        cols["title"] = p.get("summary")  # no title feed exists v1 — summary is the nearest (drift 9)
    elif env.event_type == "build_failed":
        cols["completed_at"] = iso(env.timestamp)
        cols["failure_reason"] = p.get("failure_reason")
    elif env.event_type == "build_cancelled":
        cols["completed_at"] = iso(env.timestamp)
        cols["failure_reason"] = p.get("reason")

    _upsert(conn, build_id, cols)
    return ChangeSet(panels={_PANEL}, scope_keys=[feature_id], event_at=env.timestamp)


def _upsert(conn: sqlite3.Connection, build_id: str, cols: dict[str, object | None]) -> None:
    present = {k: v for k, v in cols.items() if v is not None}
    if conn.execute("SELECT 1 FROM builds WHERE build_id=?", (build_id,)).fetchone() is None:
        keys = ["build_id", *present.keys()]
        conn.execute(
            f"INSERT INTO builds ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
            (build_id, *present.values()),
        )
        return
    assignments = ", ".join(f"{k}=?" for k in present)
    conn.execute(f"UPDATE builds SET {assignments} WHERE build_id=?", (*present.values(), build_id))


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


def _float(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _dt(v: object) -> str | None:
    dt = parse_dt(v)
    return iso(dt) if dt else None
