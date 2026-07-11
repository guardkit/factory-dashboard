"""`python -m backend.projector` — run the projector process (connection A + mirror + health).

NATS credentials + the forge-mirror path live in THIS process's environment only; no web request
path ever imports the projector package (design §7 / fence 3).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

from backend import ledger
from backend.projector.consumers import Projector, open_rw, write_heartbeat
from backend.projector.dwell import dwell_loop
from backend.projector.forge_mirror import forge_db_path, mirror_loop
from backend.projector.health_polls import health_loop
from backend.projector.plan_mirror import mirror_plans_once, plan_mirror_loop
from backend.projector.projections.base import record_change


async def _run() -> None:
    db_path = Path(os.environ.get("FACTORY_DASH_DB", "readmodel.db"))
    nats_url = os.environ.get("FACTORY_DASH_NATS_URL", "")
    if not nats_url:
        raise SystemExit("FACTORY_DASH_NATS_URL is unset — the projector needs a dev credential (IN-3 pending).")

    boot_conn = open_rw(db_path)
    write_heartbeat(boot_conn)
    # S3 cold-start: bootstrap the delivery ledger from forge `builds` (mode=ro, WAL-courtesy —
    # ledger.py owns the fence-4 open/close). A receipt-bearing COMPLETE lands a `merged_pr` row;
    # a receipt-less COMPLETE is mirrored into `builds` and surfaces as the delivered "merge
    # unverified" gap. Honestly near-empty at launch (pr_url never populated to date — drift 2).
    now = datetime.now(UTC)
    boot_panels = ledger.bootstrap_from_forge(boot_conn, forge_db_path(), now)
    if boot_panels:
        record_change(boot_conn, boot_panels, [], now)
    # S4: mirror the plan registry (plans.yaml → plan_milestones) at startup — projector-owned,
    # never a web-request write (design §7 / M-D4). The file-change re-mirror runs in the loop below.
    mirror_plans_once(boot_conn, now=now)

    projector = Projector(db_path, nats_url)
    await asyncio.gather(
        projector.run(),
        mirror_loop(open_rw(db_path)),
        health_loop(open_rw(db_path)),
        plan_mirror_loop(open_rw(db_path)),
        # F-5 stalled-dwell monitor (same cadence class as the heartbeat) — the projector is the
        # sole writer, so the dwell verdict is manufactured here, never on a web path (M-D4).
        dwell_loop(open_rw(db_path)),
    )


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_run())
