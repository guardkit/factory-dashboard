"""Accounts, session secret, Secure cookie, throttling and sign-out (design 2026-10-17, req. 2)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import backend.app as app_module
import pytest
from backend import accounts
from backend.accounts import AccountStore, ConfigError, LoginThrottle
from backend.app import LOGIN_FAILED, LOGIN_THROTTLED, create_app
from backend.auth import make_credential_hash, verify_password
from starlette.testclient import TestClient

from tests.conftest import (
    CLIENT_USER,
    TENANTS_TEST,
    TEST_DISPLAY_NAME,
    TEST_PASSWORD,
    TEST_USER,
    account_row,
    client_account_row,
    login,
    write_accounts,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


# --- accounts file -----------------------------------------------------------


def test_accounts_load_with_the_tenant_defaulting_to_operator(config_dir: Path) -> None:
    write_accounts(config_dir, [
        account_row(),
        {"username": "james", "display_name": "James", "password_hash": make_credential_hash("pw")},
        client_account_row(),
    ])
    store = AccountStore(config_dir / "accounts.yaml", {"operator", "clientco"})
    james = store.get("james")
    assert james is not None and james.display_name == "James" and james.tenant == "operator"
    client = store.get(CLIENT_USER)
    assert client is not None and client.tenant == "clientco"


def test_an_account_naming_an_unregistered_tenant_refuses_to_start(
    config_dir: Path, tmp_path: Path
) -> None:
    """The shipped tenants.yaml registers only the operator, so no real account can be a client
    tenant until a client is registered there."""
    write_accounts(config_dir, [account_row(), {**client_account_row(), "tenant": "finproxy"}])
    with pytest.raises(ConfigError, match=r"not in tenants\.yaml"):
        create_app(db_path=tmp_path / "r.db", tenants_path=TENANTS_TEST)
    write_accounts(config_dir, [account_row(), client_account_row()])
    with pytest.raises(ConfigError, match=r"not in tenants\.yaml"):
        create_app(db_path=tmp_path / "r.db")  # the shipped registry: operator only


def test_a_mistyped_field_is_refused_rather_than_ignored() -> None:
    """A misspelt `tenant` must never fall back to operator access."""
    text = "accounts:\n  - {username: x, password_hash: 'pbkdf2_sha256$1$aa$bb', tennant: clientco}\n"
    with pytest.raises(ConfigError, match="unknown field"):
        accounts.parse_accounts(text)


def test_accounts_reload_when_the_file_changes(config_dir: Path) -> None:
    path = config_dir / "accounts.yaml"
    store = AccountStore(path)
    assert store.get("james") is None
    write_accounts(config_dir, [account_row(), {"username": "james", "display_name": "James",
                                                "password_hash": make_credential_hash("pw")}])
    _bump_mtime(path)
    assert store.get("james") is not None
    write_accounts(config_dir, [account_row()])
    _bump_mtime(path)
    assert store.get("james") is None


def test_a_broken_accounts_file_after_start_lets_nobody_in(config_dir: Path) -> None:
    path = config_dir / "accounts.yaml"
    store = AccountStore(path)
    assert store.get(TEST_USER) is not None
    path.write_text("accounts: [unclosed", encoding="utf-8")
    _bump_mtime(path)
    assert store.get(TEST_USER) is None
    path.unlink()
    assert store.get(TEST_USER) is None
    write_accounts(config_dir, [account_row()])
    assert store.get(TEST_USER) is not None


@pytest.mark.parametrize("text", [
    "accounts: [unclosed",
    "accounts:\n  - username: x\n",                                   # no hash
    "accounts:\n  - username: x\n    password_hash: plaintext\n",     # not a pbkdf2 hash
    "accounts:\n  - {username: x, password_hash: 'pbkdf2_sha256$1$aa$bb'}\n"
    "  - {username: x, password_hash: 'pbkdf2_sha256$1$aa$bb'}\n",     # duplicate
])
def test_malformed_accounts_are_refused(text: str) -> None:
    with pytest.raises(ConfigError):
        accounts.parse_accounts(text)


def test_the_example_accounts_file_parses() -> None:
    parsed = accounts.parse_accounts((REPO_ROOT / "accounts.example.yaml").read_text(encoding="utf-8"))
    assert parsed  # one placeholder account
    for account in parsed.values():
        assert not verify_password("", account.password_hash)


def test_hash_command_prints_a_hash_that_verifies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(accounts.getpass, "getpass", lambda prompt="": "s3cret-typed")
    assert accounts.main(["hash"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed.startswith("pbkdf2_sha256$")
    assert verify_password("s3cret-typed", printed)


def test_hash_command_refuses_mismatched_passwords(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter(["first", "second"])
    monkeypatch.setattr(accounts.getpass, "getpass", lambda prompt="": next(answers))
    assert accounts.main(["hash"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "do not match" in out.err


# --- start-up refusals ----------------------------------------------------------


def test_missing_session_secret_refuses_to_start(config_dir: Path, tmp_path: Path) -> None:
    (config_dir / "session_secret").unlink()
    with pytest.raises(ConfigError, match="no session secret"):
        create_app(db_path=tmp_path / "r.db", tenants_path=TENANTS_TEST)


def test_short_session_secret_refuses_to_start(config_dir: Path, tmp_path: Path) -> None:
    (config_dir / "session_secret").write_text("x" * 31 + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="shorter than 32"):
        create_app(db_path=tmp_path / "r.db", tenants_path=TENANTS_TEST)


def test_missing_accounts_file_refuses_to_start(config_dir: Path, tmp_path: Path) -> None:
    (config_dir / "accounts.yaml").unlink()
    with pytest.raises(ConfigError, match="accounts file"):
        create_app(db_path=tmp_path / "r.db", tenants_path=TENANTS_TEST)


def test_production_entry_point_stops_with_a_plain_message(
    config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (config_dir / "session_secret").unlink()
    app_module._production_app.cache_clear()
    try:
        with pytest.raises(SystemExit):
            _ = app_module.app
    finally:
        app_module._production_app.cache_clear()
    assert "no session secret" in capsys.readouterr().err


# --- signing in -----------------------------------------------------------------


def test_sign_in_shows_the_display_name_and_a_sign_out_link(client: TestClient) -> None:
    body = login(client).text
    assert f"Signed in as {TEST_DISPLAY_NAME}" in body
    assert 'href="/logout"' in body and "Sign out" in body


def test_login_page_is_plain_english(client: TestClient) -> None:
    body = client.get("/login").text
    assert "Sign in to the factory dashboard" in body
    for code in ("S1", "D0", "ADR-", "read-model", "forge"):
        assert code not in body


def test_wrong_password_is_refused(client: TestClient) -> None:
    r = login(client, password="wrong")
    assert r.status_code == 401
    assert LOGIN_FAILED in r.text
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_unknown_user_is_refused_with_the_same_sentence(client: TestClient) -> None:
    r = login(client, username="nobody", password=TEST_PASSWORD)
    assert r.status_code == 401
    assert LOGIN_FAILED in r.text


def test_the_july_seed_users_are_gone(client: TestClient, tmp_path: Path) -> None:
    for name in ("operator", "clientco"):
        assert login(client, username=name, password=name).status_code == 401
    conn = sqlite3.connect(tmp_path / "readmodel.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone() == (0,)
    finally:
        conn.close()


def test_old_seed_rows_in_an_existing_read_model_are_deleted(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    create_app(db_path=db_path, tenants_path=TENANTS_TEST)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO users VALUES ('operator', 'operator', ?)", (make_credential_hash("operator"),))
    conn.commit()
    conn.close()
    create_app(db_path=db_path, tenants_path=TENANTS_TEST)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone() == (0,)
    finally:
        conn.close()


def test_removing_an_account_ends_its_session(client: TestClient, config_dir: Path) -> None:
    login(client)
    assert client.get("/", follow_redirects=False).status_code == 200
    write_accounts(config_dir, [{"username": "someone-else", "password_hash": make_credential_hash("x")}])
    _bump_mtime(config_dir / "accounts.yaml")
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_logout_ends_the_session(client: TestClient) -> None:
    login(client)
    assert client.get("/", follow_redirects=False).status_code == 200
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/fragments/p1").status_code == 401


# --- cookie ------------------------------------------------------------------------


def test_session_cookie_is_secure_httponly_lax_14_days(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FACTORY_DASH_INSECURE_COOKIE")
    app = create_app(db_path=tmp_path / "r.db", tenants_path=TENANTS_TEST)
    with TestClient(app, base_url="https://testserver") as c:
        r = login(c)
        assert r.status_code == 200  # the Secure cookie came back over HTTPS
        cookie = r.history[0].headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert f"max-age={14 * 24 * 60 * 60}" in cookie


def test_insecure_cookie_only_with_the_env_switch(client: TestClient) -> None:
    cookie = login(client).history[0].headers["set-cookie"].lower()
    assert "secure" not in cookie
    assert "httponly" in cookie


# --- throttling ----------------------------------------------------------------------


def test_throttle_locks_after_five_failures_and_reopens_after_15_minutes() -> None:
    now = [1000.0]
    t = LoginThrottle(clock=lambda: now[0])
    for _ in range(4):
        t.record_failure("james")
    assert not t.is_locked("james")
    t.record_failure("james")
    assert t.is_locked("james")
    assert not t.is_locked("someone-else")
    now[0] += 15 * 60 - 1
    assert t.is_locked("james")
    now[0] += 2
    assert not t.is_locked("james")


def test_throttle_counts_only_failures_within_15_minutes() -> None:
    now = [0.0]
    t = LoginThrottle(clock=lambda: now[0])
    for _ in range(4):
        t.record_failure("james")
    now[0] += 15 * 60 + 1
    t.record_failure("james")
    assert not t.is_locked("james")


def test_success_clears_the_failure_count() -> None:
    t = LoginThrottle()
    for _ in range(4):
        t.record_failure("james")
    t.record_success("james")
    t.record_failure("james")
    assert not t.is_locked("james")


def test_login_route_refuses_after_five_failures_even_with_the_right_password(client: TestClient) -> None:
    for _ in range(5):
        assert login(client, password="wrong").status_code == 401
    r = login(client)
    assert r.status_code == 429
    assert LOGIN_THROTTLED in r.text
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
