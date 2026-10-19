"""Answers sent from the dashboard that the factory has not recorded yet (design of 19 October 2026).

A small SQLite database, ``answers.db``, beside the read model and ``suggestions.db`` in the board's
data folder. Only the board writes it; the feed never opens it. It holds two things:

* **Claims.** Before the board sends an answer it claims the card's request id here. The claim is
  unique per request id, so a second press, or a second person, finds it and is told an answer is
  waiting; only one answer is ever sent from the dashboard. A claim is a *pending submission*, never
  an answer: what the page shows as answered is always Forge's own record. A claim is settled from
  the record: when the card closes it is removed (the page then shows what Forge recorded); when the
  card is still open after two successful feed passes that began after the answer was sent, it is
  marked "not recorded" and no longer blocks another press. Only successful reads are counted
  (``mirror_status.read_count``), and only while the copy is fresh, so a stopped feed never clears a
  claim or declares an answer unrecorded.
* **Rebuild marks.** "Rebuild the preview" is not an answer and claims nothing. Each merge card has
  its own mark holding the offer's rebuild count when the rebuild was asked for, so a second press
  while one is waiting is ignored, and the page can say what the record proves since.

Every write is one short transaction (BEGIN IMMEDIATE), so two presses at once cannot both claim.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from backend.auth import ResolvedUser
from backend.readmodel import cards as cardmodel
from backend.readmodel import dbread
from backend.readmodel.factory import load_factory, parse_dt

log = logging.getLogger("factory_dashboard.claims")

DB_NAME = "answers.db"

#: Passes of the feed that must begin after an answer was sent before the board says the factory has
#: not recorded it. The count is read just after the send; the pass after that one may already have
#: been under way, so "two passes after the send" is the read count plus three.
NOT_RECORDED_AFTER = 3
#: The same for a rebuild, which is slower: "three passes or more" (plus the one that may straddle).
REBUILD_UNCONFIRMED_AFTER = 4

WAITING = "waiting"
NOT_RECORDED = "not_recorded"
REQUESTED = "requested"
DONE = "done"
UNCONFIRMED = "unconfirmed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS answer_claims (
    request_id   TEXT PRIMARY KEY,
    card_kind    TEXT NOT NULL,
    answer       TEXT NOT NULL,
    decision     TEXT NOT NULL,
    username     TEXT NOT NULL,
    display_name TEXT NOT NULL,
    claimed_at   TEXT NOT NULL,
    read_count   INTEGER NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('waiting', 'not_recorded')),
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rebuild_marks (
    request_id   TEXT PRIMARY KEY,
    rebuilds     INTEGER NOT NULL,
    username     TEXT NOT NULL,
    display_name TEXT NOT NULL,
    asked_at     TEXT NOT NULL,
    read_count   INTEGER NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('requested', 'done', 'unconfirmed')),
    updated_at   TEXT NOT NULL
);
"""

_BUSY_TIMEOUT_SECS = 3.0


class Unavailable(Exception):
    """answers.db could not be read or written."""


@dataclass(frozen=True)
class Claim:
    request_id: str
    card_kind: str
    answer: str
    decision: str
    username: str
    display_name: str
    claimed_at: datetime
    read_count: int
    state: str


@dataclass(frozen=True)
class Mark:
    request_id: str
    rebuilds: int
    username: str
    display_name: str
    asked_at: datetime
    read_count: int
    state: str


def db_path_for(data_dir: Path) -> Path:
    return data_dir / DB_NAME


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="seconds")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_SECS, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: Path) -> bool:
    """Create answers.db and its tables if missing (start-up). A file that cannot be opened is
    logged; the board still starts, and an answer then says it could not be sent."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(path)
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        log.warning("claims: %s cannot be opened (%s); answers are switched off until it can", path.name, exc)
        return False
    return True


@contextlib.contextmanager
def _write(path: Path) -> Iterator[sqlite3.Connection]:
    try:
        conn = _connect(path)
    except sqlite3.Error as exc:
        raise Unavailable(str(exc)) from None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as exc:
        log.warning("claims: could not write (%s)", exc)
        raise Unavailable(str(exc)) from None
    finally:
        conn.close()


def _claim(row: sqlite3.Row) -> Claim:
    return Claim(row["request_id"], row["card_kind"], row["answer"], row["decision"], row["username"],
                 row["display_name"], parse_dt(row["claimed_at"]) or datetime.now(UTC), int(row["read_count"]),
                 row["state"])


def _mark(row: sqlite3.Row) -> Mark:
    return Mark(row["request_id"], int(row["rebuilds"]), row["username"], row["display_name"],
                parse_dt(row["asked_at"]) or datetime.now(UTC), int(row["read_count"]), row["state"])


# --- claims --------------------------------------------------------------------------------------------


def claim(path: Path, *, request_id: str, card_kind: str, answer: str, decision: str, user: ResolvedUser,
          read_count: int, now: datetime | None = None) -> tuple[bool, Claim]:
    """(True, the new claim), or (False, the claim already waiting for this request id). One
    transaction, so of two presses at once exactly one claims."""
    now = now or datetime.now(UTC)
    with _write(path) as conn:
        row = conn.execute("SELECT * FROM answer_claims WHERE request_id = ?", (request_id,)).fetchone()
        if row is not None and row["state"] == WAITING:
            return False, _claim(row)
        conn.execute(
            "INSERT OR REPLACE INTO answer_claims (request_id, card_kind, answer, decision, username, display_name, "
            "claimed_at, read_count, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?)",
            (request_id, card_kind, answer, decision, user.username, user.display_name, _iso(now), read_count,
             _iso(now)),
        )
        new = conn.execute("SELECT * FROM answer_claims WHERE request_id = ?", (request_id,)).fetchone()
    return True, _claim(new)


def set_read_count(path: Path, request_id: str, read_count: int, *, table: str = "answer_claims") -> None:
    """The feed's read count just after the send: passes are counted from here."""
    assert table in ("answer_claims", "rebuild_marks")
    with _write(path) as conn:
        conn.execute(f"UPDATE {table} SET read_count = ? WHERE request_id = ?", (read_count, request_id))


def release(path: Path, request_id: str) -> None:
    """Clear a claim whose answer could not be sent, so the person can try again."""
    with _write(path) as conn:
        conn.execute("DELETE FROM answer_claims WHERE request_id = ?", (request_id,))


# --- rebuild marks ----------------------------------------------------------------------------------------


def ask_rebuild(path: Path, *, request_id: str, rebuilds: int, user: ResolvedUser, read_count: int,
                now: datetime | None = None) -> tuple[bool, Mark]:
    """(True, the new mark), or (False, the mark of a rebuild already waiting)."""
    now = now or datetime.now(UTC)
    with _write(path) as conn:
        row = conn.execute("SELECT * FROM rebuild_marks WHERE request_id = ?", (request_id,)).fetchone()
        if row is not None and row["state"] == REQUESTED:
            return False, _mark(row)
        conn.execute(
            "INSERT OR REPLACE INTO rebuild_marks (request_id, rebuilds, username, display_name, asked_at, read_count, "
            "state, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'requested', ?)",
            (request_id, rebuilds, user.username, user.display_name, _iso(now), read_count, _iso(now)),
        )
        new = conn.execute("SELECT * FROM rebuild_marks WHERE request_id = ?", (request_id,)).fetchone()
    return True, _mark(new)


def drop_rebuild(path: Path, request_id: str) -> None:
    with _write(path) as conn:
        conn.execute("DELETE FROM rebuild_marks WHERE request_id = ?", (request_id,))


# --- reading ---------------------------------------------------------------------------------------------


def _read(path: Path) -> tuple[list[Claim], list[Mark]]:
    """Every claim and mark, read-only. A missing database is empty and is not created."""
    if not path.exists():
        return [], []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_SECS)
        conn.row_factory = sqlite3.Row
        try:
            claims_ = [_claim(r) for r in conn.execute("SELECT * FROM answer_claims")]
            marks = [_mark(r) for r in conn.execute("SELECT * FROM rebuild_marks")]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("claims: could not read (%s)", exc)
        return [], []
    return claims_, marks


def read_all(path: Path) -> tuple[dict[str, Claim], dict[str, Mark]]:
    claims_, marks = _read(path)
    return {c.request_id: c for c in claims_}, {m.request_id: m for m in marks}


def token(path: Path) -> str:
    """A short version of the store, for the page's data version: when a claim or a mark changes,
    an open page fetches its content again (static/freshness.js), as it does when the record changes."""
    claims_, marks = _read(path)
    parts = sorted(f"{c.request_id}={c.state}" for c in claims_) + sorted(
        f"r:{m.request_id}={m.state}" for m in marks)
    if not parts:
        return "a0"
    return "a" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:10]


# --- settling from the record ---------------------------------------------------------------------------


_Deletes = list[tuple[str, str]]
_Updates = list[tuple[str, str, str]]


def _settle_claims(found: list[Claim], every: dict[str, cardmodel.Card], counted: int | None
                   ) -> tuple[_Deletes, _Updates]:
    deletes: _Deletes = []
    updates: _Updates = []
    for c in found:
        card = every.get(c.request_id)
        if card is None or not card.open:
            deletes.append(("answer_claims", c.request_id))       # closed: the record says how
        elif c.state == WAITING and counted is not None and counted >= c.read_count + NOT_RECORDED_AFTER:
            updates.append(("answer_claims", c.request_id, NOT_RECORDED))
    return deletes, updates


def _settle_marks(found: list[Mark], every: dict[str, cardmodel.Card], counted: int | None
                  ) -> tuple[_Deletes, _Updates]:
    deletes: _Deletes = []
    updates: _Updates = []
    for m in found:
        card = every.get(m.request_id)
        if card is None or not card.open or card.rebuild is None:
            deletes.append(("rebuild_marks", m.request_id))
        elif m.state != REQUESTED:
            continue
        elif card.rebuild.count > m.rebuilds:
            updates.append(("rebuild_marks", m.request_id, DONE))         # a rebuild attempt finished
        elif (not card.rebuild.wanted_at and counted is not None
              and counted >= m.read_count + REBUILD_UNCONFIRMED_AFTER):
            updates.append(("rebuild_marks", m.request_id, UNCONFIRMED))  # long enough with no change
    return deletes, updates


def settle(store: Path, db_path: Path, now: datetime | None = None) -> None:
    """Bring every claim and mark up to date with the copy of Forge's record. Reads one snapshot of
    the record; writes only when something changed. Never raises: a page must still render."""
    claims_, marks = _read(store)
    if not claims_ and not marks:
        return
    now = now or datetime.now(UTC)
    try:
        conn = dbread.snapshot(db_path)
        try:
            fr = dbread.freshness(conn)
            factory = load_factory(conn, now)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("claims: could not read the record to settle answers (%s)", exc)
        return
    every = {c.request_id: c for c in cardmodel.all_cards(factory)}
    # Only successful reads of a copy that is fresh now are counted (a stopped feed counts nothing).
    counted = fr.read_count if fr.fresh(now) and not fr.error else None
    deletes, updates = _settle_claims(claims_, every, counted)
    more_deletes, more_updates = _settle_marks(marks, every, counted)
    deletes += more_deletes
    updates += more_updates
    if not deletes and not updates:
        return
    try:
        with _write(store) as w:
            for table, rid in deletes:
                w.execute(f"DELETE FROM {table} WHERE request_id = ?", (rid,))
            for table, rid, state in updates:
                w.execute(f"UPDATE {table} SET state = ?, updated_at = ? WHERE request_id = ?", (state, _iso(now), rid))
    except Unavailable:
        return
