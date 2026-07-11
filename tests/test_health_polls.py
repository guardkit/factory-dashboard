"""S2 P6 health polls (fence 5): LiteLLM `/health` is NEVER requested; load-neutral endpoints only;
resident model list parsed from llama-swap `/v1/models`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from backend.projector import health_polls
from backend.projector.health_polls import Endpoints, poll_once

from tests.nats_util import make_projected_db

REQUESTED: list[str] = []


def _handler(request: httpx.Request) -> httpx.Response:
    REQUESTED.append(f"{request.url.host}:{request.url.port}{request.url.path}")
    path = request.url.path
    if path == "/v1/models" and request.url.port == 9000:  # llama-swap models
        return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}, {"id": "gemma-2b"}]})
    if path == "/v1/models":  # litellm models (load-neutral)
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})
    if path == "/health":  # llama-swap health
        return httpx.Response(200, text="ok")
    if path == "/healthz":  # nats monitoring
        return httpx.Response(200, text="ok")
    return httpx.Response(404)


def test_litellm_health_is_never_polled_and_resident_models_parsed(tmp_path: Path) -> None:
    REQUESTED.clear()
    _db, conn = make_projected_db(tmp_path)
    ep = Endpoints(litellm_base="http://127.0.0.1:4000",
                   llama_swap_base="http://127.0.0.1:9000",
                   nats_monitor="http://127.0.0.1:8222")

    async def run() -> None:
        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            await poll_once(conn, ep, client=client)

    asyncio.run(run())

    # FORBIDDEN endpoint: LiteLLM /health must never appear on any poll path.
    assert "127.0.0.1:4000/health" not in REQUESTED
    assert "127.0.0.1:4000/v1/models" in REQUESTED   # the load-neutral one IS used
    assert "127.0.0.1:8222/healthz" in REQUESTED

    rows = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT service, status, detail FROM service_health")}
    assert rows["litellm"][0] == "ok"
    assert rows["nats"][0] == "ok"
    assert rows["llama-swap"] == ("ok", "resident: qwen3.6-35b, gemma-2b")  # resident list parsed


def test_no_litellm_health_path_in_source() -> None:
    """Belt-and-braces: the module contains no LiteLLM /health call site by construction."""
    src = Path(health_polls.__file__).read_text(encoding="utf-8")
    assert "litellm_base}/health" not in src and 'litellm_base + "/health"' not in src
