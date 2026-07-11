"""Shared pytest fixtures: an app on a temp DB, an operator client, and a client-tenant client."""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest
from backend.app import create_app
from starlette.testclient import TestClient

# Starlette's TestClient emits a noisy httpx deprecation; irrelevant to these tests.
warnings.filterwarnings("ignore", category=DeprecationWarning)

REPO_ROOT = Path(__file__).resolve().parent.parent
TENANTS_TEST = Path(__file__).resolve().parent / "fixtures" / "tenants_test.yaml"


@pytest.fixture
def app(tmp_path: Path):  # type: ignore[no-untyped-def]
    return create_app(db_path=tmp_path / "readmodel.db", tenants_path=TENANTS_TEST)


@pytest.fixture
def client(app) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    with TestClient(app) as c:
        yield c


@pytest.fixture
def operator_client(client: TestClient) -> TestClient:
    r = client.post("/login", data={"username": "operator", "password": "operator"})
    assert r.status_code == 200  # redirected to "/" then rendered
    return client


@pytest.fixture
def clientco_client(client: TestClient) -> TestClient:
    client.post("/login", data={"username": "clientco", "password": "clientco"})
    return client
