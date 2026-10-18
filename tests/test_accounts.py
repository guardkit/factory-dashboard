"""Accounts, session secret, Secure cookie, throttling and sign-out (design 2026-10-17, req. 2)."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import backend.app as app_module
import itsdangerous
import pytest
from backend import accounts
from backend.accounts import AccountStore, ConfigError, LoginThrottle
from backend.app import (
    CROSS_SITE_LOGIN,
    CROSS_SITE_LOGOUT,
    LOGIN_FAILED,
    LOGIN_THROTTLED,
    create_app,
)
from backend.auth import make_credential_hash, verify_password
from starlette.testclient import TestClient

from tests.conftest import (
    CLIENT_USER,
    LOOPBACK_URL,
    TENANTS_TEST,
    TEST_DISPLAY_NAME,
    TEST_PASSWORD,
    TEST_SECRET,
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
    assert '<form method="post" action="/logout"' in body and "Sign out" in body
    assert 'href="/logout"' not in body  # a plain link could sign people out from anywhere


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
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/fragments/p1").status_code == 401


# --- cookie ------------------------------------------------------------------------


def _set_cookie(client: TestClient, **headers: str) -> str:
    r = client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD},
                    headers=headers, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["set-cookie"].lower()


@pytest.mark.parametrize("base_url", ["https://dashboard.example:8443", "http://dashboard.example"])
def test_session_cookie_is_secure_httponly_lax_14_days_off_loopback(app, base_url: str) -> None:  # type: ignore[no-untyped-def]
    """Any host other than localhost/127.0.0.1 gets Secure, whatever the scheme: behind Docker and
    tailscale serve the board always sees plain HTTP."""
    with TestClient(app, base_url=base_url) as c:
        cookie = _set_cookie(c)
    assert "; secure" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert f"max-age={14 * 24 * 60 * 60}" in cookie


def test_secure_cookie_round_trips_over_https(app) -> None:  # type: ignore[no-untyped-def]
    with TestClient(app, base_url="https://dashboard.example:8443") as c:
        assert login(c).status_code == 200
        assert c.get("/", follow_redirects=False).status_code == 200


@pytest.mark.parametrize("base_url", ["http://localhost:8090", "http://127.0.0.1:8090",
                                      "http://LOCALHOST", "http://127.0.0.1"])
def test_loopback_host_gets_a_plain_cookie_for_the_ssh_tunnel(app, base_url: str) -> None:  # type: ignore[no-untyped-def]
    """Safari refuses a Secure cookie from http://localhost; the tunnel must work in every browser."""
    with TestClient(app, base_url=base_url) as c:
        cookie = _set_cookie(c)
        assert "; secure" not in cookie
        assert "httponly" in cookie and "samesite=lax" in cookie
        assert c.get("/", follow_redirects=False).status_code == 200  # signed in over plain HTTP


@pytest.mark.parametrize("header", ["X-Forwarded-Proto", "X-Forwarded-Host"])
def test_a_proxied_request_naming_loopback_still_gets_secure(app, header: str) -> None:  # type: ignore[no-untyped-def]
    with TestClient(app, base_url="http://127.0.0.1:8090") as c:
        assert "; secure" in _set_cookie(c, **{header: "https"})


def test_sign_out_clears_the_cookie_with_the_same_flags(app) -> None:  # type: ignore[no-untyped-def]
    with TestClient(app, base_url="https://dashboard.example:8443") as c:
        login(c)
        r = c.post("/logout", follow_redirects=False)
    assert "; secure" in r.headers["set-cookie"].lower()


def test_sign_in_starts_a_fresh_session(client: TestClient) -> None:
    """Anything in the session before sign-in (for example planted by someone else) is dropped."""
    signer = itsdangerous.TimestampSigner(TEST_SECRET)
    planted = signer.sign(base64.b64encode(json.dumps({"planted": "x"}).encode())).decode()
    client.cookies.set("session", planted)
    r = login(client, follow=False)
    value = r.headers["set-cookie"].split("session=", 1)[1].split(";", 1)[0]
    data = json.loads(base64.b64decode(signer.unsign(value)))
    assert data == {"username": TEST_USER}


# --- throttling ----------------------------------------------------------------------


def test_throttle_allows_five_attempts_then_locks_for_15_minutes() -> None:
    now = [1000.0]
    t = LoginThrottle(clock=lambda: now[0])
    for _ in range(5):
        assert t.try_attempt("james")
    assert not t.try_attempt("james")
    assert t.is_locked("james")
    assert t.try_attempt("someone-else")
    now[0] += 15 * 60 - 1
    assert not t.try_attempt("james")
    now[0] += 2
    assert t.try_attempt("james")


def test_throttle_counts_only_attempts_within_15_minutes() -> None:
    now = [0.0]
    t = LoginThrottle(clock=lambda: now[0])
    for _ in range(4):
        t.try_attempt("james")
    now[0] += 15 * 60 + 1
    t.try_attempt("james")
    assert not t.is_locked("james")


def test_success_clears_the_count() -> None:
    t = LoginThrottle()
    for _ in range(4):
        t.try_attempt("james")
    t.record_success("james")
    for _ in range(4):
        t.try_attempt("james")
    assert not t.is_locked("james")


def test_throttle_is_atomic_under_threads() -> None:
    t = LoginThrottle()
    barrier = threading.Barrier(40)

    def go() -> bool:
        barrier.wait()
        return t.try_attempt("james")

    with ThreadPoolExecutor(max_workers=40) as pool:
        results = list(pool.map(lambda _: go(), range(40)))
    assert results.count(True) == 5


def test_parallel_wrong_passwords_check_at_most_five_hashes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sixty wrong passwords at once: only five reach the (slow) password check."""
    calls: list[str] = []
    lock = threading.Lock()
    real = accounts.verify_password

    def slow_verify(password: str, stored: str) -> bool:
        with lock:
            calls.append(password)
        time.sleep(0.05)
        return real(password, stored)

    monkeypatch.setattr(accounts, "verify_password", slow_verify)
    with ThreadPoolExecutor(max_workers=30) as pool:
        codes = list(pool.map(lambda i: login(client, password=f"wrong-{i}").status_code, range(60)))
    assert len(calls) == 5
    assert codes.count(401) == 5 and codes.count(429) == 55


def test_login_route_refuses_after_five_failures_even_with_the_right_password(client: TestClient) -> None:
    for _ in range(5):
        assert login(client, password="wrong").status_code == 401
    r = login(client)
    assert r.status_code == 429
    assert LOGIN_THROTTLED in r.text
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_a_right_password_clears_earlier_failures(client: TestClient) -> None:
    for _ in range(4):
        login(client, password="wrong")
    assert login(client).status_code == 200
    client.post("/logout")
    for _ in range(4):
        assert login(client, password="wrong").status_code == 401
    assert login(client).status_code == 200


def test_an_unknown_username_still_runs_a_password_check(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So a wrong username takes as long as a wrong password and does not reveal who exists."""
    seen: list[str] = []
    real = accounts.verify_password

    def recording(password: str, stored: str) -> bool:
        seen.append(stored)
        return real(password, stored)

    monkeypatch.setattr(accounts, "verify_password", recording)
    store = AccountStore(config_dir / "accounts.yaml")
    assert store.authenticate("nobody", "anything") is None
    assert seen == [accounts._DUMMY_HASH]


# --- cross-site requests (CSRF) --------------------------------------------------------


def _signed_in(client: TestClient) -> bool:
    return client.get("/", follow_redirects=False).status_code == 200


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "http://localhost:9999"},                     # same name, different port
    {"Origin": "null"},                                      # sandboxed or privacy-stripped
    {"Referer": "https://evil.example/page"},                # no Origin: the Referer decides
])
def test_cross_site_sign_in_is_refused(client: TestClient, headers: dict[str, str]) -> None:
    """Another site's page must not sign the visitor into an account it controls."""
    r = client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD},
                    headers=headers, follow_redirects=False)
    assert r.status_code == 403
    assert CROSS_SITE_LOGIN.split(",")[0] in r.text  # the apostrophe later on is HTML-escaped
    assert "set-cookie" not in r.headers
    assert not _signed_in(client)


def test_same_origin_sign_in_and_sign_out_succeed(client: TestClient) -> None:
    own = {"Origin": LOOPBACK_URL}
    r = client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD},
                    headers=own, follow_redirects=False)
    assert r.status_code == 303 and _signed_in(client)
    r = client.post("/logout", headers={"Referer": LOOPBACK_URL + "/features"},
                    follow_redirects=False)
    assert r.status_code == 303 and not _signed_in(client)


def test_cross_site_sign_out_is_refused(client: TestClient) -> None:
    login(client)
    for headers in ({"Origin": "https://evil.example"}, {"Referer": "https://evil.example/x"}):
        r = client.post("/logout", headers=headers, follow_redirects=False)
        assert r.status_code == 403
        assert CROSS_SITE_LOGOUT in r.text
        assert _signed_in(client)


def test_a_link_cannot_sign_anyone_out(client: TestClient) -> None:
    login(client)
    assert client.get("/logout", follow_redirects=False).status_code == 405
    assert _signed_in(client)


def test_tailnet_address_through_the_proxy_is_same_origin(app) -> None:  # type: ignore[no-untyped-def]
    """Behind tailscale serve: the browser's Origin is the tailnet HTTPS address; the board sees
    plain HTTP, with the address in Host or X-Forwarded-Host."""
    tailnet = "https://dashboard.example:8443"
    with TestClient(app, base_url=tailnet) as c:  # Host passed through
        r = c.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD},
                   headers={"Origin": tailnet}, follow_redirects=False)
        assert r.status_code == 303
    with TestClient(app, base_url="http://127.0.0.1:8090") as c:  # Host rewritten, X-Forwarded-Host kept
        fwd = {"Origin": tailnet, "X-Forwarded-Host": "dashboard.example:8443", "X-Forwarded-Proto": "https"}
        r = c.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD},
                   headers=fwd, follow_redirects=False)
        assert r.status_code == 303
        r = c.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD},
                   headers={**fwd, "Origin": "https://evil.example"}, follow_redirects=False)
        assert r.status_code == 403


def test_default_ports_compare_equal(app) -> None:  # type: ignore[no-untyped-def]
    with TestClient(app, base_url="https://dashboard.example") as c:
        r = c.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD},
                   headers={"Origin": "https://dashboard.example:443"}, follow_redirects=False)
        assert r.status_code == 303
