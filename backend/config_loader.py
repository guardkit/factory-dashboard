"""Config-is-data loaders for the tenant registry and plan registry.

tenants.yaml and plans.yaml are CONFIG the dashboard READS (design §4.0 / ux §4.7). At runtime the
projector mirrors them into the `tenants` / `plan_milestones` tables (startup + on file change,
never a web-request write). At S1 there is no projector, so the web layer reads the tenant registry
directly from tenants.yaml for session->tenant resolution and the operator-only route guard.

Nothing here writes anything. `operator` is the reserved fleet-wide tenant with no client store.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent / "config"
TENANTS_PATH = CONFIG_DIR / "tenants.yaml"
PLANS_PATH = CONFIG_DIR / "plans.yaml"


@dataclass(frozen=True)
class Tenant:
    tenant_slug: str
    display_name: str
    nats_account: str | None
    subject_prefix: str | None
    projects: tuple[str, ...]
    store_path: str | None

    @property
    def is_operator(self) -> bool:
        return self.tenant_slug == "operator"


def _coerce_tenant(row: dict[str, object]) -> Tenant:
    projects_raw = row.get("projects") or []
    projects = tuple(str(p) for p in projects_raw) if isinstance(projects_raw, list) else ()
    return Tenant(
        tenant_slug=str(row["tenant_slug"]),
        display_name=str(row.get("display_name", row["tenant_slug"])),
        nats_account=_opt_str(row.get("nats_account")),
        subject_prefix=_opt_str(row.get("subject_prefix")),
        projects=projects,
        store_path=_opt_str(row.get("store_path")),
    )


def _opt_str(v: object) -> str | None:
    return None if v is None else str(v)


def load_tenants(path: Path = TENANTS_PATH) -> dict[str, Tenant]:
    """Return {tenant_slug: Tenant}. Always includes the reserved `operator` row."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = data.get("tenants") or []
    tenants: dict[str, Tenant] = {}
    for row in rows:
        if isinstance(row, dict):
            t = _coerce_tenant(row)
            tenants[t.tenant_slug] = t
    tenants.setdefault(
        "operator",
        Tenant("operator", "Factory (operator)", "APPMILLA", None, (), None),
    )
    return tenants


def load_plans(path: Path = PLANS_PATH) -> dict[str, list[dict[str, object]]]:
    """Return {tenant_slug: [milestone, ...]} from plans.yaml. Empty in v1 (examples commented)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    plans = data.get("plans") or {}
    if not isinstance(plans, dict):
        return {}
    out: dict[str, list[dict[str, object]]] = {}
    for tenant_slug, milestones in plans.items():
        if isinstance(milestones, list):
            out[str(tenant_slug)] = [m for m in milestones if isinstance(m, dict)]
    return out
