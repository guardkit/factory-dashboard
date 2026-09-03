"""P6 — gateway / llama-swap / NATS health polls (fence 5).

**Load-neutral endpoints ONLY, poll ≤1/30 s:** LiteLLM `GET /v1/models`, llama-swap `GET /health`
+ `GET /v1/models`, NATS `:8222/healthz`. **LiteLLM `/health` is FORBIDDEN** — it fires real test
completions per configured model and would reshuffle GPU residency twice a minute (design §2 P6 /
gate finding F-15). This module contains NO `/health` path for LiteLLM by construction, and a test
asserts the forbidden path is never requested.

Results → `service_health` (checked_at drives the as-of chip + the §5.2 dot-demotion). The llama-swap
resident model list (from `/v1/models`) is stored in `detail` as `resident: …` for the D4
no-eviction rule; the query layer parses it. This module makes zero model calls and writes nothing
to the bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from backend.projector.projections.base import iso

POLL_INTERVAL_SECS = 30
_TIMEOUT = 5.0


@dataclass(frozen=True)
class Endpoints:
    litellm_base: str = os.environ.get("FACTORY_LITELLM_URL", "http://127.0.0.1:4000")
    llama_swap_base: str = os.environ.get("FACTORY_LLAMASWAP_URL", "http://127.0.0.1:9000")
    nats_monitor: str = os.environ.get("FACTORY_NATS_MONITOR_URL", "http://127.0.0.1:8222")
    # 2026-09-03: LiteLLM refuses an unauthenticated `GET /v1/models` (401), so for four weeks this
    # poll wrote two failure rows a minute into LiteLLM's own spend log and the dashboard showed
    # LiteLLM as failing. The key is a dashboard-only LiteLLM key (see .env.example); it is sent
    # to the LiteLLM base ONLY, never to llama-swap or NATS.
    litellm_key: str = os.environ.get("FACTORY_LITELLM_KEY", "")


def _upsert(conn: sqlite3.Connection, service: str, status: str, detail: str, now: datetime) -> None:
    conn.execute(
        """INSERT INTO service_health (service, status, detail, checked_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(service) DO UPDATE SET status=excluded.status, detail=excluded.detail,
               checked_at=excluded.checked_at""",
        (service, status, detail, iso(now)),
    )


async def _get(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
) -> httpx.Response | None:
    try:
        return await client.get(url, timeout=_TIMEOUT, headers=headers)
    except httpx.HTTPError:
        return None


def _litellm_headers(ep: Endpoints) -> dict[str, str] | None:
    """Bearer header for LiteLLM only; None when no key is configured (the old, refused behaviour)."""
    return {"Authorization": f"Bearer {ep.litellm_key}"} if ep.litellm_key else None


async def poll_once(
    conn: sqlite3.Connection,
    endpoints: Endpoints | None = None,
    now: datetime | None = None,
    client: httpx.AsyncClient | None = None,
) -> None:
    """One poll pass across the three services. Load-neutral endpoints only (fence 5). An injected
    `client` (tests) is not closed here; otherwise a short-lived client is opened and closed."""
    ep = endpoints or Endpoints()
    now = now or datetime.now(UTC)
    owned = client is None
    http = client or httpx.AsyncClient()
    try:
        # LiteLLM — GET /v1/models ONLY (never /health — it fires real completions, fence 5).
        r = await _get(http, f"{ep.litellm_base}/v1/models", headers=_litellm_headers(ep))
        _upsert(conn, "litellm", "ok" if _ok(r) else "fail", "ok" if _ok(r) else _err(r), now)

        # llama-swap — GET /health then GET /v1/models for the resident list.
        h = await _get(http, f"{ep.llama_swap_base}/health")
        if _ok(h):
            models = await _get(http, f"{ep.llama_swap_base}/v1/models")
            _upsert(conn, "llama-swap", "ok", _resident_detail(models), now)
        else:
            _upsert(conn, "llama-swap", "fail", _err(h), now)

        # NATS monitoring — GET :8222/healthz.
        n = await _get(http, f"{ep.nats_monitor}/healthz")
        _upsert(conn, "nats", "ok" if _ok(n) else "fail", "ok" if _ok(n) else _err(n), now)
    finally:
        if owned:
            await http.aclose()


def _ok(r: httpx.Response | None) -> bool:
    return r is not None and r.status_code == 200


def _err(r: httpx.Response | None) -> str:
    return "unreachable" if r is None else f"HTTP {r.status_code}"


def _resident_detail(models: httpx.Response | None) -> str:
    if not _ok(models) or models is None:
        return "ok"
    try:
        data = models.json()
        ids = [str(m.get("id")) for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
    except (ValueError, TypeError, AttributeError):
        ids = []
    return f"resident: {', '.join(ids)}" if ids else "ok"


async def health_loop(conn: sqlite3.Connection, *, interval: int = POLL_INTERVAL_SECS) -> None:  # pragma: no cover
    ep = Endpoints()
    while True:
        # a poll failure is reflected as a fail row, never a crash
        with contextlib.suppress(Exception):
            await poll_once(conn, ep)
        await asyncio.sleep(interval)
