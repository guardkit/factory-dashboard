"""`python -m backend.projector` — run the projector process (connection A + mirror + health).

NATS credentials + the forge-mirror path live in THIS process's environment only; no web request
path ever imports the projector package (design §7 / fence 3).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from backend.projector.consumers import Projector, open_rw, write_heartbeat
from backend.projector.forge_mirror import mirror_loop
from backend.projector.health_polls import health_loop


async def _run() -> None:
    db_path = Path(os.environ.get("FACTORY_DASH_DB", "readmodel.db"))
    nats_url = os.environ.get("FACTORY_DASH_NATS_URL", "")
    if not nats_url:
        raise SystemExit("FACTORY_DASH_NATS_URL is unset — the projector needs a dev credential (IN-3 pending).")

    write_heartbeat(open_rw(db_path))
    projector = Projector(db_path, nats_url)
    await asyncio.gather(
        projector.run(),
        mirror_loop(open_rw(db_path)),
        health_loop(open_rw(db_path)),
    )


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_run())
