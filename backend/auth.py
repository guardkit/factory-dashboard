"""Password hashing and the signed-in user (design 2026-10-17, requirement 2).

The signed cookie (Starlette SessionMiddleware) carries ONLY the username. Each request looks the
username up again in the accounts file (backend/accounts.py), so removing an account ends its
sessions, and its tenant is re-read from that file on every request (gate finding F-2f), never
trusted from the cookie. The tenant decides operator pages or the client layout, as in July.

Password hashes are PBKDF2-HMAC-SHA256 in the form ``pbkdf2_sha256$<iterations>$<salt>$<digest>``;
``python -m backend.accounts hash`` makes them with the same function that checks them.
"""

from __future__ import annotations

import hashlib
import hmac
import os
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
    try:
        salt, iterations = bytes.fromhex(salt_hex), int(iters_s)
    except ValueError:
        return False
    if iterations < 1:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest.hex(), hash_hex)


@dataclass(frozen=True)
class ResolvedUser:
    username: str
    display_name: str
    tenant_slug: str
    approver_identity: str = ""   # who this account answers the factory's cards as ("" = no buttons)

    @property
    def is_operator(self) -> bool:
        return self.tenant_slug == "operator"

    @property
    def can_answer(self) -> bool:
        """Only an operator account with an approver identity may answer a card."""
        return self.is_operator and bool(self.approver_identity)
