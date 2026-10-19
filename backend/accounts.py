"""Accounts, the session secret and login throttling (design 2026-10-17, requirement 2).

Accounts live in ``accounts.yaml`` in a configuration folder OUTSIDE the repository
(``FACTORY_DASH_CONFIG_DIR``, default ``/config`` inside the container, where compose mounts
``~/.config/factory-dashboard`` read-only). The same folder holds ``session_secret``.

``accounts.yaml``::

    accounts:
      - username: james
        display_name: James
        password_hash: "pbkdf2_sha256$120000$<salt hex>$<digest hex>"
        tenant: operator               # optional, default operator; must be in tenants.yaml
        approver_identity: "<id>"      # optional; operator accounts only (see below)

The file is read at start and re-read whenever its modification time or size changes. An
account's ``tenant`` decides what it sees, exactly as the July users table did: ``operator`` sees
the factory pages; a client tenant sees only its own client layout. Unknown keys are refused, so a
mistyped ``tenant`` can never fall back to operator access.

``approver_identity`` (design of 19 October 2026) is the identity an operator's answers to the
factory's cards are sent under (``decided_by``), the one Forge checks: for Rich his existing Slack
member id, for another approver the ``dashboard:<name>`` Forge lists in ``approval.additional_approvers``.
An account without it sees the cards but no buttons. A client account may not have one.

Command line: ``python -m backend.accounts hash`` asks for a password twice and prints its hash.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

import yaml

from backend.auth import make_credential_hash, verify_password

log = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path("/config")
ACCOUNTS_FILE = "accounts.yaml"
SESSION_SECRET_FILE = "session_secret"
MIN_SECRET_LENGTH = 32
OPERATOR = "operator"
_KEYS = frozenset({"username", "display_name", "password_hash", "tenant", "approver_identity"})
_IDENTITY = re.compile(r"[^\s]{1,200}")

# A fixed hash checked when the username is unknown, so a wrong username takes as long as a wrong
# password and the response time does not reveal which usernames exist.
_DUMMY_HASH = make_credential_hash("not-a-real-password", salt=b"factory-dashboard")


class ConfigError(RuntimeError):
    """The configuration folder is missing something the board needs to start."""


@dataclass(frozen=True)
class Account:
    username: str
    display_name: str
    password_hash: str
    tenant: str = OPERATOR
    approver_identity: str = ""


def config_dir() -> Path:
    raw = os.environ.get("FACTORY_DASH_CONFIG_DIR")
    return Path(raw) if raw else DEFAULT_CONFIG_DIR


def read_session_secret(folder: Path) -> str:
    path = folder / SESSION_SECRET_FILE
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise ConfigError(
            f"The factory dashboard cannot start: there is no session secret at {path}. "
            "Create one as the README's 'Accounts and access' section describes."
        ) from None
    except OSError as exc:
        raise ConfigError(
            f"The factory dashboard cannot start: the session secret at {path} cannot be read ({exc})."
        ) from None
    if len(value) < MIN_SECRET_LENGTH:
        raise ConfigError(
            f"The factory dashboard cannot start: the session secret at {path} is shorter than "
            f"{MIN_SECRET_LENGTH} characters. Make a longer one as the README describes."
        )
    return value


def parse_accounts(
    text: str, source: str = ACCOUNTS_FILE, known_tenants: Collection[str] | None = None
) -> dict[str, Account]:
    """Parse accounts.yaml text into {username: Account}. Raises ConfigError on a malformed file,
    including an account whose tenant is not in ``known_tenants`` (when given)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source} is not valid YAML: {exc}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{source} must contain an 'accounts:' list.")
    rows = data.get("accounts") or []
    if not isinstance(rows, list):
        raise ConfigError(f"{source}: 'accounts' must be a list.")
    accounts: dict[str, Account] = {}
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ConfigError(f"{source}: account {i} is not a mapping.")
        username = str(row.get("username") or "").strip()
        stored = str(row.get("password_hash") or "").strip()
        if not username:
            raise ConfigError(f"{source}: account {i} has no username.")
        if not stored.startswith("pbkdf2_sha256$") or stored.count("$") != 3:
            raise ConfigError(
                f"{source}: account '{username}' needs a password_hash made by "
                "'python -m backend.accounts hash'."
            )
        if username in accounts:
            raise ConfigError(f"{source}: the username '{username}' appears twice.")
        unknown = sorted(str(k) for k in row if k not in _KEYS)
        if unknown:
            raise ConfigError(
                f"{source}: account '{username}' has unknown field(s) {', '.join(unknown)}; "
                f"allowed: {', '.join(sorted(_KEYS))}."
            )
        tenant = str(row.get("tenant") or OPERATOR).strip()
        if known_tenants is not None and tenant not in known_tenants:
            raise ConfigError(
                f"{source}: account '{username}' names the tenant '{tenant}', "
                "which is not in tenants.yaml."
            )
        identity = _approver_identity(row, username, tenant, source)
        accounts[username] = Account(
            username=username,
            display_name=str(row.get("display_name") or username).strip(),
            password_hash=stored,
            tenant=tenant,
            approver_identity=identity,
        )
    return accounts


def _approver_identity(row: dict[object, object], username: str, tenant: str, source: str) -> str:
    """The identity this account answers the factory's cards as, or "". Only an operator may have one."""
    identity = str(row.get("approver_identity") or "").strip()
    if identity and not _IDENTITY.fullmatch(identity):
        raise ConfigError(f"{source}: account '{username}' has an approver_identity with spaces in it.")
    if identity and tenant != OPERATOR:
        raise ConfigError(
            f"{source}: account '{username}' is a client account, so it cannot have an approver_identity."
        )
    return identity


class AccountStore:
    """accounts.yaml, re-read when its modification time or size changes.

    A file that becomes unreadable or malformed after start leaves NO account able to sign in
    (fail closed: an edit that was meant to remove someone must not leave them in) and logs why.
    """

    def __init__(self, path: Path, known_tenants: Collection[str] | None = None) -> None:
        self.path = path
        self.known_tenants = None if known_tenants is None else frozenset(known_tenants)
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._accounts: dict[str, Account] = {}
        try:
            self._load(self._stat())
        except (OSError, ConfigError) as exc:
            raise ConfigError(
                f"The factory dashboard cannot start: the accounts file at {path} "
                f"is missing or unusable ({exc})."
            ) from None

    def _stat(self) -> tuple[int, int]:
        st = self.path.stat()
        return (st.st_mtime_ns, st.st_size)

    def _load(self, stamp: tuple[int, int]) -> None:
        self._accounts = parse_accounts(
            self.path.read_text(encoding="utf-8"), str(self.path), self.known_tenants
        )
        self._stamp = stamp

    def _refresh(self) -> None:
        with self._lock:
            try:
                stamp = self._stat()
            except OSError as exc:
                if self._stamp is not None:
                    log.error("accounts file %s cannot be read (%s); nobody can sign in", self.path, exc)
                self._accounts, self._stamp = {}, None
                return
            if stamp == self._stamp:
                return
            try:
                self._load(stamp)
                log.info("accounts file %s re-read: %d account(s)", self.path, len(self._accounts))
            except (OSError, ConfigError) as exc:
                log.error("accounts file %s is unusable (%s); nobody can sign in", self.path, exc)
                self._accounts, self._stamp = {}, stamp

    def get(self, username: str) -> Account | None:
        self._refresh()
        return self._accounts.get(username)

    def all(self) -> dict[str, Account]:
        self._refresh()
        return dict(self._accounts)

    def authenticate(self, username: str, password: str) -> Account | None:
        account = self.get(username)
        if account is None:
            verify_password(password, _DUMMY_HASH)
            return None
        return account if verify_password(password, account.password_hash) else None


class LoginThrottle:
    """At most ``limit`` sign-in attempts for one username within ``window`` seconds; reaching the
    limit locks the username for ``lockout`` seconds. An attempt is counted BEFORE its password is
    checked, in one step under the lock, so parallel guesses cannot overrun the limit while the
    slow hash runs. A successful sign-in clears the count. Kept in memory; a restart clears it."""

    def __init__(
        self,
        limit: int = 5,
        window: float = 15 * 60,
        lockout: float = 15 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit, self.window, self.lockout, self.clock = limit, window, lockout, clock
        self._lock = threading.Lock()
        self._attempts: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}

    def _prune(self, now: float) -> None:
        for name in [n for n, until in self._locked_until.items() if until <= now]:
            del self._locked_until[name]
        for name in list(self._attempts):
            times = self._attempts[name]
            while times and times[0] <= now - self.window:
                times.popleft()
            if not times:
                del self._attempts[name]

    def is_locked(self, username: str) -> bool:
        with self._lock:
            self._prune(self.clock())
            return username in self._locked_until

    def try_attempt(self, username: str) -> bool:
        """Check and count one attempt atomically. False means refuse it without checking the
        password (the username is locked)."""
        with self._lock:
            now = self.clock()
            self._prune(now)
            if username in self._locked_until:
                return False
            times = self._attempts.setdefault(username, deque())
            times.append(now)
            if len(times) >= self.limit:
                self._locked_until[username] = now + self.lockout
                del self._attempts[username]
            return True

    def record_success(self, username: str) -> None:
        with self._lock:
            self._attempts.pop(username, None)
            self._locked_until.pop(username, None)


# --- command line -----------------------------------------------------------


def _hash_command() -> int:
    first = getpass.getpass("New password: ")
    if not first:
        print("No password given; nothing printed.", file=sys.stderr)
        return 1
    second = getpass.getpass("Same password again: ")
    if first != second:
        print("The two passwords do not match; nothing printed.", file=sys.stderr)
        return 1
    print(make_credential_hash(first))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["hash"]:
        return _hash_command()
    print(
        "Usage: python -m backend.accounts hash\n"
        "  Asks for a password twice and prints a password_hash line for accounts.yaml.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
