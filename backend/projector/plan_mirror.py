"""Plan registry mirror (ux §4.7 / S4): plans.yaml → `plan_milestones`, PROJECTOR-OWNED.

Config-is-data, exactly like tenants.yaml → tenants: the plan baseline is config the dashboard
READS. The projector mirrors it into `plan_milestones` at startup + on file change; NEVER a
web-request write (design §7 / M-D4 — the projector is the sole writer of `readmodel.db`, the web
layer opens `mode=ro`). **Deviation state (`behind|on_target|ahead|no_baseline`) is NEVER stored
here** — it is computed by the query layer from delivered-vs-target, so a stale projection can
withhold the verdict rather than serve a stale-green claim (§4.7 / §5.6 F-5).

Zero NATS, zero publish, zero JetStream: this transforms an already-parsed YAML file into upserts
on an already-open SQLite handle the projector owns (fences 1/2 hold).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from backend.config_loader import PLANS_PATH, load_plans
from backend.projector.projections.base import iso

PLAN_MIRROR_INTERVAL_SECS = 30


def _milestone_fields(tenant: str, milestone: dict[str, object]) -> tuple[str, str, str, str, str | None, str] | None:
    """Coerce one plans.yaml milestone into the `plan_milestones` row tuple, or None when it has
    no stable `id`. `feature_ids` is stored as a JSON list; `project` is the alternative scope."""
    mid = str(milestone.get("id") or "").strip()
    if not mid:
        return None
    title = str(milestone.get("title") or mid)
    feature_ids_raw = milestone.get("feature_ids") or []
    feature_ids = [str(f) for f in feature_ids_raw] if isinstance(feature_ids_raw, list) else []
    project = milestone.get("project")
    target_window = str(milestone.get("target_window") or "")
    return (mid, tenant, title, json.dumps(feature_ids), str(project) if project else None, target_window)


def mirror_plans_once(dash: sqlite3.Connection, plans_path: Path = PLANS_PATH, now: datetime | None = None) -> bool:
    """One plan-mirror pass: upsert every configured milestone and DELETE any `plan_milestones` row
    no longer present in the file (so removing a milestone from plans.yaml removes it from the
    read model — a full reconcile, not an append-only mirror). Returns True when anything changed."""
    now = now or datetime.now(UTC)
    plans = load_plans(plans_path)  # {tenant_slug: [milestone dict, ...]}
    seen: list[str] = []
    stamp = iso(now)
    for tenant, milestones in plans.items():
        for milestone in milestones:
            fields = _milestone_fields(tenant, milestone)
            if fields is None:
                continue
            mid, tenant_slug, title, feature_ids_json, project, target_window = fields
            dash.execute(
                """INSERT INTO plan_milestones
                       (milestone_id, tenant, title, feature_ids_json, project, target_window, mirrored_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(milestone_id) DO UPDATE SET
                       tenant=excluded.tenant, title=excluded.title,
                       feature_ids_json=excluded.feature_ids_json, project=excluded.project,
                       target_window=excluded.target_window, mirrored_at=excluded.mirrored_at""",
                (mid, tenant_slug, title, feature_ids_json, project, target_window, stamp),
            )
            seen.append(mid)
    if seen:
        placeholders = ",".join("?" * len(seen))
        cur = dash.execute(
            f"DELETE FROM plan_milestones WHERE milestone_id NOT IN ({placeholders})", seen
        )
    else:
        cur = dash.execute("DELETE FROM plan_milestones")
    # "changed" is best-effort: any upsert (rows configured) or any deletion counts.
    return bool(seen) or (cur.rowcount or 0) > 0


async def plan_mirror_loop(  # pragma: no cover (file-watch loop; mirror_plans_once is unit-tested)
    dash: sqlite3.Connection, plans_path: Path = PLANS_PATH, *, interval: int = PLAN_MIRROR_INTERVAL_SECS
) -> None:
    """Re-mirror plans.yaml on file change (mtime poll) — startup + on file change, per §4.7.
    A mirror hiccup is swallowed (an unchanged read model is honest); the loop never crashes."""
    last_mtime: float | None = None
    while True:
        with contextlib.suppress(OSError, sqlite3.Error):
            mtime = plans_path.stat().st_mtime
            if mtime != last_mtime:
                mirror_plans_once(dash, plans_path)
                last_mtime = mtime
        await asyncio.sleep(interval)
