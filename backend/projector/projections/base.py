"""Shared projection helpers: envelope decode, timestamp parsing, watermarks, the SSE change log.

The projector is the SOLE writer of `readmodel.db` (design §7 / M-D4). These helpers run under a
read-WRITE connection owned by the projector process ONLY — no web request path ever imports this
module (the web layer opens `mode=ro`). All bus subscriptions upstream are plain core-NATS: nothing
here creates a JetStream consumer, acks, or publishes (ADR-DASH-007 / M-D3).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class Envelope:
    """The decoded MessageEnvelope (nats-core `envelope.py`), guarded for the drift-13 nullable
    `correlation_id`. Unknown envelope fields are ignored (v1 payloads are `extra="ignore"` — we
    consume only the known fields, capability note drift 8)."""

    event_type: str
    payload: dict[str, Any]
    timestamp: datetime
    source_id: str = ""
    project: str | None = None
    correlation_id: str | None = None  # nullable at the envelope level — guarded (drift 13)


@dataclass
class ChangeSet:
    """What a single projected message changed: panel ids to notify + the event recency to
    advance the feeding stream's watermark to."""

    panels: set[str] = field(default_factory=set)
    scope_keys: list[str] = field(default_factory=list)
    event_at: datetime | None = None


def parse_dt(value: object) -> datetime | None:
    """Parse an ISO-8601 string (or pass through a datetime) to an aware UTC datetime, or None.

    Guards the many nullable timestamp fields on the wire and in forge SQLite without raising.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def decode_envelope(raw: bytes | str) -> Envelope | None:
    """Decode a wire message body into an Envelope. Returns None for anything undecodable — a
    malformed message must never crash the projector (it advances no watermark, projects nothing)."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or "event_type" not in data:
        return None
    payload = data.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    ts = parse_dt(data.get("timestamp")) or datetime.now(UTC)
    cid = data.get("correlation_id")
    return Envelope(
        event_type=str(data["event_type"]),
        payload=payload,
        timestamp=ts,
        source_id=str(data.get("source_id", "")),
        project=data.get("project"),
        correlation_id=cid if isinstance(cid, str) else None,
    )


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def advance_watermark(conn: sqlite3.Connection, stream: str, event_at: datetime | None, now: datetime) -> None:
    """Advance a stream's consumer watermark: bump the monotonic message counter and move
    `last_event_at` forward to the newest event seen (never backward). Core-NATS has no JetStream
    sequence, so `last_stream_seq` is a plain per-stream message counter (design §4.1 resume aid)."""
    consumer = "dashboard_ro"
    row = conn.execute(
        "SELECT last_stream_seq, last_event_at FROM consumer_watermarks WHERE stream=? AND consumer=?",
        (stream, consumer),
    ).fetchone()
    new_event_at = event_at
    if row is not None and row[1]:
        prev = parse_dt(row[1])
        if prev is not None and (new_event_at is None or prev > new_event_at):
            new_event_at = prev
    seq = (row[0] if row is not None else 0) + 1
    conn.execute(
        """INSERT INTO consumer_watermarks (stream, consumer, last_stream_seq, last_event_at, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(stream, consumer) DO UPDATE SET
               last_stream_seq=excluded.last_stream_seq,
               last_event_at=excluded.last_event_at,
               updated_at=excluded.updated_at""",
        (stream, consumer, seq, iso(new_event_at) if new_event_at else None, iso(now)),
    )


def record_change(conn: sqlite3.Connection, panels: set[str], scope_keys: list[str], now: datetime) -> None:
    """Append one SSE change-log row per changed panel (design §6, notification-only — no row data
    on the push channel). The `seq` PK is the change counter the /events `id:` uses."""
    scope_json = json.dumps(scope_keys) if scope_keys else None
    for panel in sorted(panels):
        conn.execute(
            "INSERT INTO change_log (panel, scope_keys, at) VALUES (?, ?, ?)",
            (panel, scope_json, iso(now)),
        )


def stream_for_subject(subject: str) -> str:
    """Map a wire subject to its stream name (for the watermark + the §5.6 per-stream display)."""
    if subject.startswith("pipeline."):
        return "PIPELINE"
    if subject.startswith("agents."):
        return "AGENTS"
    if subject.startswith("fleet.") or subject.startswith("$KV.agent-registry."):
        return "FLEET"
    if subject.startswith("deploy."):
        return "DEPLOY"
    return "OTHER"
