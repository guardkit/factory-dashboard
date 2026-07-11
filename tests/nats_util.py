"""Test helpers: an ephemeral local nats-server + envelope builders + a projected-DB factory.

Fence 3: develop/test against an EPHEMERAL LOCAL nats-server — never the live broker. We launch the
`nats-server` binary on a free loopback port (a clean interpretation of the fence's "ephemeral local
nats-server (docker)": a per-test local server, torn down after, that is not the live broker). No
JetStream is enabled or needed — every subscription under test is plain core-NATS, and KV re-puts are
simulated as plain core publishes to the `$KV.agent-registry.>` subject space (exactly what a KV
put looks like to a core subscriber).
"""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from backend.config_loader import load_tenants
from backend.db import init_db
from backend.projector.consumers import open_rw

TENANTS_TEST = Path(__file__).resolve().parent / "fixtures" / "tenants_test.yaml"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def nats_server() -> Iterator[str]:
    """Launch an ephemeral local nats-server (core only) and yield its URL."""
    port = _free_port()
    proc = subprocess.Popen(
        ["nats-server", "-a", "127.0.0.1", "-p", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_until_listening("127.0.0.1", port)
        yield f"nats://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _wait_until_listening(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            if s.connect_ex((host, port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"nats-server did not start on {host}:{port}")


def make_projected_db(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    """A schema-applied read model + the projector's rw connection (autocommit)."""
    db_path = tmp_path / "readmodel.db"
    init_db(db_path, load_tenants(TENANTS_TEST).values())
    return db_path, open_rw(db_path)


def envelope(
    event_type: str,
    payload: dict[str, object],
    *,
    correlation_id: str | None = None,
    timestamp: datetime | None = None,
    project: str | None = None,
    source_id: str = "test-producer",
) -> bytes:
    """Build a MessageEnvelope wire body (nats-core envelope.py shape)."""
    ts = timestamp or datetime.now(UTC)
    return json.dumps({
        "message_id": "msg-test",
        "timestamp": ts.isoformat(),
        "version": "1.0",
        "source_id": source_id,
        "event_type": event_type,
        "project": project,
        "correlation_id": correlation_id,
        "payload": payload,
    }).encode()
