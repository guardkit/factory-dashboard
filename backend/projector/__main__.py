"""`python -m backend.projector` — the dashboard's feed.

It applies the store's schema, then runs four loops: the heartbeat (10 s), the copy of Forge's
record (15 s), the service checks (30 s) and the plan file (on change). It has no bus connection:
the design of 17 October 2026 takes every build, planning, approval and delivery fact from Forge's
record, which is complete and survives the dashboard being stopped; bus events do not.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from backend import db
from backend.projector.forge_mirror import forge_db_path, mirror_loop, mirror_once
from backend.projector.health_polls import health_loop
from backend.projector.plan_mirror import mirror_plans_once, plan_mirror_loop
from backend.projector.store import open_rw, write_heartbeat

HEARTBEAT_SECS = 10


async def heartbeat_loop(conn: sqlite3.Connection, *, interval: int = HEARTBEAT_SECS) -> None:  # pragma: no cover
    while True:
        with contextlib.suppress(sqlite3.Error):
            write_heartbeat(conn)
        await asyncio.sleep(interval)


async def _run() -> None:
    db_path = Path(os.environ.get("FACTORY_DASH_DB", "readmodel.db"))
    # The schema first, so the feed never depends on the web container having started.
    db.apply_schema(db_path)

    boot = open_rw(db_path)
    now = datetime.now(UTC)
    write_heartbeat(boot, now)
    mirror_once(boot, forge_db_path(), now)
    mirror_plans_once(boot, now=now)
    boot.close()

    await asyncio.gather(
        heartbeat_loop(open_rw(db_path)),
        mirror_loop(open_rw(db_path)),
        health_loop(open_rw(db_path)),
        plan_mirror_loop(open_rw(db_path)),
    )


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_run())
