"""Auth + session->tenant resolution mechanics (design §7).

Server-side session (signed cookie via Starlette SessionMiddleware). The session carries ONLY the
authenticated username; the tenant is RE-READ from `users.tenant_slug` by that username on every
request (gate finding F-2f) — never trusted from the cookie. The tenant then selects which read
store the request opens and (D3+) which chat registry / SSE channels it may use.

v1 skeleton auth: PBKDF2-HMAC-SHA256 credential hashes, dev passwords seeded by db.init_db. This is
a scaffold — real credential provisioning + Tailscale network boundary are the operational posture
(arch §5); hardening is deferred, but the session->tenant seam is the real one.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from dataclasses import dataclass

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 120_000


def make_credential_hash(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if algo != _ALGO:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters_s)
    )
    return hmac.compare_digest(digest.hex(), hash_hex)


@dataclass(frozen=True)
class ResolvedUser:
    username: str
    tenant_slug: str

    @property
    def is_operator(self) -> bool:
        return self.tenant_slug == "operator"


def resolve_user(conn: sqlite3.Connection, username: str) -> ResolvedUser | None:
    """Re-read the tenant binding for an authenticated username (F-2f). conn is a mode=ro handle."""
    row = conn.execute(
        "SELECT username, tenant_slug FROM users WHERE username = ?", (username,)
    ).fetchone()
    if row is None:
        return None
    return ResolvedUser(username=str(row[0]), tenant_slug=str(row[1]))


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> ResolvedUser | None:
    """Verify credentials against the users table (mode=ro handle). Returns the resolved user."""
    row = conn.execute(
        "SELECT username, tenant_slug, credential_hash FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        return None
    if not verify_password(password, str(row[2])):
        return None
    return ResolvedUser(username=str(row[0]), tenant_slug=str(row[1]))
