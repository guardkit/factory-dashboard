"""The projector — connection A: core-NATS subscriptions → the read model (design §6, ux §9.2).

**Fences (coach greps every close):** ALL subscriptions are plain core-NATS. This module creates
ZERO JetStream consumers, binds none on PIPELINE, acks nothing, and publishes nothing (ADR-DASH-007
/ M-D3 / fence 1-2). `$KV.agent-registry.>` is a subject-space read of the KV bucket's re-puts — a
plain subscribe, never a KV/JetStream API call (capability note drift 3). The dev credential is the
projector's environment ONLY (`FACTORY_DASH_NATS_URL`, named for the IN-3 `dashboard_ro` swap; the
interim borrow is `james` — read-only is enforced by this module's discipline, not by cred scope,
until IN-3 lands, drift 10). No web request path imports this module.

The projector is the SOLE writer of `readmodel.db`. It owns one read-WRITE connection; the whole
web layer opens `mode=ro` (fence 4 / M-D4).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import nats
from nats.aio.client import Client
from nats.aio.msg import Msg

from backend import ledger
from backend.projector.projections import p1_agents, p2_builds, p3_stage, p4_approvals
from backend.projector.projections.base import (
    ChangeSet,
    Envelope,
    advance_watermark,
    decode_envelope,
    iso,
    record_change,
    stream_for_subject,
)

# The four subject spaces the roster/board/gate/approval projections consume (all core-NATS).
SUBJECTS: tuple[str, ...] = ("pipeline.>", "agents.>", "fleet.>", "$KV.agent-registry.>")

HEARTBEAT_SECS = 10


def open_rw(db_path: Path) -> sqlite3.Connection:
    """The projector's sole read-WRITE connection (autocommit; WAL). Never used by a web request."""
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _now() -> datetime:
    return datetime.now(UTC)


# Subject-prefix → projection (first match wins). All plain core-NATS; no JetStream, no publish.
_ROUTES: tuple[tuple[str, Callable[[sqlite3.Connection, str, Envelope, datetime], ChangeSet]], ...] = (
    ("fleet.", p1_agents.project),
    ("agents.approval.", p4_approvals.project),
    ("pipeline.build-", p2_builds.project),
    ("pipeline.stage-", p3_stage.project),
)


def route(conn: sqlite3.Connection, subject: str, data: bytes, now: datetime) -> ChangeSet:
    """Pure routing + projection of one wire message. Returns the ChangeSet (panels touched +
    the event recency). Separated from the network so it is unit-testable without a broker."""
    if subject.startswith("$KV.agent-registry."):
        return p1_agents.project_kv(conn, subject, data, now)
    env = decode_envelope(data)
    if env is None:
        return ChangeSet()
    for prefix, handler in _ROUTES:
        if subject.startswith(prefix):
            changes = handler(conn, subject, env, now)
            # S3: a build_complete carrying a merge receipt also appends a `merged_pr` ledger row
            # (idempotent, keyed (feature_id, bar) — design §5 steady state). No receipt ⇒ no
            # ledger row (the delivered page renders the complete as its "merge unverified" gap).
            if prefix == "pipeline.build-" and env.event_type == "build_complete":
                ledger_panels = ledger.append_from_build_complete(conn, env, now)
                if ledger_panels:
                    changes.panels |= ledger_panels
                    fid = str(env.payload.get("feature_id") or "")
                    if fid and fid not in changes.scope_keys:
                        changes.scope_keys = [*changes.scope_keys, fid]
            return changes
    return ChangeSet()  # other pipeline./agents.* events: watermark-only (advanced by the caller)


def apply_message(conn: sqlite3.Connection, subject: str, data: bytes, now: datetime | None = None) -> ChangeSet:
    """Project one message and persist: advance the feeding stream's watermark (ALWAYS, so 'events
    flowed' is detectable for F-5) and append change-log rows for any panels that changed."""
    now = now or _now()
    changes = route(conn, subject, data, now)
    advance_watermark(conn, stream_for_subject(subject), changes.event_at or now, now)
    if changes.panels:
        record_change(conn, changes.panels, changes.scope_keys, now)
    return changes


def write_heartbeat(conn: sqlite3.Connection, now: datetime | None = None) -> None:
    """Upsert `service_health('projector')` every 10 s regardless of event flow (ux §5.6 signal 1)."""
    now = now or _now()
    conn.execute(
        """INSERT INTO service_health (service, status, detail, checked_at)
           VALUES ('projector', 'ok', 'projector heartbeat', ?)
           ON CONFLICT(service) DO UPDATE SET
               status='ok', detail='projector heartbeat', checked_at=excluded.checked_at""",
        (iso(now),),
    )


class Projector:
    """Owns the NATS connection + the rw DB connection; runs the subscriptions + heartbeat loop."""

    def __init__(self, db_path: Path, nats_url: str, *, heartbeat_secs: int = HEARTBEAT_SECS) -> None:
        self._db_path = db_path
        self._nats_url = nats_url
        self._heartbeat_secs = heartbeat_secs
        self._conn: sqlite3.Connection | None = None
        self._nc: Client | None = None
        self._hb_task: asyncio.Task[None] | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = open_rw(self._db_path)
        return self._conn

    async def _on_message(self, msg: Msg) -> None:
        # Sqlite writes are synchronous + fast (<100 ms). A poison message never wedges the loop.
        with contextlib.suppress(Exception):
            apply_message(self.conn, msg.subject, msg.data)

    async def connect(self) -> None:
        self._nc = await nats.connect(self._nats_url, max_reconnect_attempts=-1)
        for subject in SUBJECTS:
            await self._nc.subscribe(subject, cb=self._on_message)
        write_heartbeat(self.conn)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_secs)
            write_heartbeat(self.conn)

    async def run(self) -> None:
        await self.connect()
        self._hb_task = asyncio.create_task(self._heartbeat_loop())
        await self._hb_task

    async def close(self) -> None:
        if self._hb_task is not None:
            self._hb_task.cancel()
        if self._nc is not None:
            await self._nc.drain()
        if self._conn is not None:
            self._conn.close()


async def _main() -> None:
    db_path = Path(os.environ.get("FACTORY_DASH_DB", "readmodel.db"))
    nats_url = os.environ.get("FACTORY_DASH_NATS_URL", "")
    if not nats_url:
        raise SystemExit("FACTORY_DASH_NATS_URL is unset — the projector needs a dev credential (IN-3 pending).")
    await Projector(db_path, nats_url).run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_main())
