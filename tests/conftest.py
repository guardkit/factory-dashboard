"""Shared pytest fixtures: a configuration folder (accounts + session secret), an app on a temp DB,
and a signed-in client."""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from backend.app import create_app
from backend.auth import make_credential_hash
from starlette.testclient import TestClient

# Starlette's TestClient emits a noisy httpx deprecation; irrelevant to these tests.
warnings.filterwarnings("ignore", category=DeprecationWarning)

REPO_ROOT = Path(__file__).resolve().parent.parent
TENANTS_TEST = Path(__file__).resolve().parent / "fixtures" / "tenants_test.yaml"

# A test-only account, written to a temporary configuration folder for every test.
TEST_USER = "tester"
TEST_PASSWORD = "test-password-not-real"
TEST_DISPLAY_NAME = "Test Person"
TEST_SECRET = "test-session-secret-" + "x" * 40
_TEST_HASH = make_credential_hash(TEST_PASSWORD)


def write_accounts(folder: Path, rows: list[dict[str, object]]) -> Path:
    path = folder / "accounts.yaml"
    path.write_text(yaml.safe_dump({"accounts": rows}), encoding="utf-8")
    return path


# A test-only client-tenant account (the test registry lists the client tenant `clientco`), so the
# July client scoping (redirect to the client layout, 403 on the fragment API and the event stream)
# stays tested now that accounts come from accounts.yaml.
CLIENT_USER = "client-tester"
CLIENT_PASSWORD = "client-password-not-real"
_CLIENT_HASH = make_credential_hash(CLIENT_PASSWORD)


def account_row() -> dict[str, object]:
    return {"username": TEST_USER, "display_name": TEST_DISPLAY_NAME, "password_hash": _TEST_HASH}


def client_account_row() -> dict[str, object]:
    return {"username": CLIENT_USER, "display_name": "Client Person", "password_hash": _CLIENT_HASH,
            "tenant": "clientco"}


@pytest.fixture(autouse=True)
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every app built in a test reads this folder (FACTORY_DASH_CONFIG_DIR). The plain-HTTP test
    client needs FACTORY_DASH_INSECURE_COOKIE=1; the Secure flag has its own test."""
    folder = tmp_path / "config"
    folder.mkdir()
    (folder / "session_secret").write_text(TEST_SECRET + "\n", encoding="utf-8")
    write_accounts(folder, [account_row()])
    monkeypatch.setenv("FACTORY_DASH_CONFIG_DIR", str(folder))
    monkeypatch.setenv("FACTORY_DASH_INSECURE_COOKIE", "1")
    return folder


def login(client: TestClient, username: str = TEST_USER, password: str = TEST_PASSWORD):  # type: ignore[no-untyped-def]
    return client.post("/login", data={"username": username, "password": password})


@pytest.fixture
def app(tmp_path: Path):  # type: ignore[no-untyped-def]
    return create_app(db_path=tmp_path / "readmodel.db", tenants_path=TENANTS_TEST)


@pytest.fixture
def client(app) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    with TestClient(app) as c:
        yield c


@pytest.fixture
def operator_client(client: TestClient) -> TestClient:
    r = login(client)
    assert r.status_code == 200  # redirected to "/" then rendered
    return client


@pytest.fixture
def clientco_client(client: TestClient, config_dir: Path) -> TestClient:
    # Added after start (the board re-reads the file): only TENANTS_TEST registers `clientco`.
    write_accounts(config_dir, [account_row(), client_account_row()])
    r = login(client, CLIENT_USER, CLIENT_PASSWORD)
    assert r.status_code == 200  # redirected to "/" (the client layout) then rendered
    return client
