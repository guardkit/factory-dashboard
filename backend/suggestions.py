"""Suggestions and "what changed" (design 2026-10-17, requirement 11: James can refine it).

A signed-in operator writes a short suggestion for a change to the dashboard. It is kept in its own
small SQLite database, ``suggestions.db``, beside the read model in the board's data folder. This is
the only thing the web side ever writes: Forge's record, LiteLLM and the read model stay read-only
to it, and the feed (the projector) never opens this file.

Each suggestion records who (the signed-in account and its display name), when (UTC), the page it
came from, the text, and a status: new, seen, done or declined. Any operator may set the status.
A person may send at most ``PER_HOUR`` suggestions in any hour, each at most ``MAX_CHARS``
characters. Control characters are removed before the text is stored, so nothing typed can act on a
terminal when the list is printed.

"What changed" is ``CHANGES.md`` at the top of the repository: a short dated list in plain words.
Only a tiny part of Markdown is read (``## date`` headings and ``- item`` lines); the words are
handed to the templates as plain strings, which escape them, so nothing in it can become HTML.

Command line (read-only; it never creates the database)::

    python -m backend.suggestions list [--data DIR] [--status new] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.auth import ResolvedUser

BASE_DIR = Path(__file__).resolve().parent.parent
CHANGES_PATH = BASE_DIR / "CHANGES.md"
DB_NAME = "suggestions.db"

MAX_CHARS = 2000
PER_HOUR = 10
MAX_FROM_CHARS = 300
STATUSES = ("new", "seen", "done", "declined")
STATUS_WORDS = {"new": "New", "seen": "Seen", "done": "Done", "declined": "Declined"}

# Every refusal is one plain sentence.
EMPTY = "Please write what you would like changed."
TOO_LONG = f"Please keep a suggestion under {MAX_CHARS} characters."
LIMITED = f"You have sent {PER_HOUR} suggestions in the last hour, so please wait a little before sending another."
CROSS_SITE = "This came from another website, so it was refused."
UNKNOWN = "That suggestion or status was not recognised, so nothing was changed."

_SCHEMA = """
CREATE TABLE IF NOT EXISTS suggestions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    username          TEXT NOT NULL,
    display_name      TEXT NOT NULL,
    from_page         TEXT NOT NULL DEFAULT '',
    text              TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'seen', 'done', 'declined')),
    status_changed_at TEXT,
    status_changed_by TEXT
);
CREATE INDEX IF NOT EXISTS suggestions_by_user ON suggestions (username, created_at);
"""

_BUSY_TIMEOUT_SECS = 10.0
# A page address on this board: a path from the root, no scheme, no host, no spaces.
_PAGE = re.compile(r"/(?!/)[A-Za-z0-9/_.~%?=&+-]*")


@dataclass(frozen=True)
class Suggestion:
    id: int
    created_at: str
    username: str
    display_name: str
    from_page: str
    text: str
    status: str
    status_changed_at: str | None
    status_changed_by: str | None

    @property
    def status_word(self) -> str:
        return STATUS_WORDS.get(self.status, self.status)

    @property
    def when(self) -> str:
        """'18 Oct 2026, 19:40 UTC' for the page."""
        try:
            at = datetime.fromisoformat(self.created_at)
        except ValueError:
            return self.created_at
        return f"{at.day} {at:%b %Y, %H:%M} UTC"


def db_path_for(data_dir: Path) -> Path:
    return data_dir / DB_NAME


def default_data_dir() -> Path:
    """The board's data folder: the folder of its read model (FACTORY_DASH_DB), as the app decides it."""
    env = os.environ.get("FACTORY_DASH_DB")
    return Path(env).parent if env else BASE_DIR


def clean_text(raw: str) -> str:
    """Line endings made plain, control characters (escape codes included) removed, tabs as spaces,
    trailing space trimmed. Newlines are kept: the page shows the lines as written."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp"))
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def clean_page(raw: str) -> str:
    """The page a suggestion came from, kept only when it is a plain address on this board."""
    page = raw.strip()
    if len(page) > MAX_FROM_CHARS or not _PAGE.fullmatch(page):
        return ""
    return page


def _connect_rw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_SECS, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: Path) -> None:
    """Create suggestions.db and its table if missing (the board's start-up; idempotent)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect_rw(path)
    try:
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="seconds")


def add(path: Path, user: ResolvedUser, text: str, from_page: str, *, now: datetime | None = None,
        per_hour: int = PER_HOUR) -> tuple[int | None, str]:
    """(id, "") when stored, or (None, the sentence saying why not). The hourly count and the insert are
    one transaction (BEGIN IMMEDIATE), so parallel posts cannot pass the limit together."""
    body = clean_text(text)
    if not body:
        return None, EMPTY
    if len(body) > MAX_CHARS:
        return None, TOO_LONG
    now = now or datetime.now(UTC)
    conn = _connect_rw(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM suggestions WHERE username = ? AND created_at > ?",
                (user.username, _iso(now - timedelta(hours=1))),
            ).fetchone()
            if count >= per_hour:
                conn.execute("ROLLBACK")
                return None, LIMITED
            cur = conn.execute(
                "INSERT INTO suggestions (created_at, username, display_name, from_page, text) VALUES (?, ?, ?, ?, ?)",
                (_iso(now), user.username, clean_text(user.display_name) or user.username, clean_page(from_page),
                 body),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return int(cur.lastrowid or 0), ""
    finally:
        conn.close()


def set_status(path: Path, suggestion_id: int, status: str, user: ResolvedUser, *,
               now: datetime | None = None) -> bool:
    """Set one suggestion's status. False when the status or the suggestion is not known."""
    if status not in STATUSES:
        return False
    conn = _connect_rw(path)
    try:
        cur = conn.execute(
            "UPDATE suggestions SET status = ?, status_changed_at = ?, status_changed_by = ? WHERE id = ?",
            (status, _iso(now or datetime.now(UTC)), user.username, suggestion_id),
        )
        return cur.rowcount == 1
    finally:
        conn.close()


def list_all(path: Path, status: str | None = None) -> list[Suggestion]:
    """Newest first, read-only. A missing database is an empty list, and is not created."""
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_SECS)
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM suggestions"
        args: tuple[str, ...] = ()
        if status:
            sql, args = sql + " WHERE status = ?", (status,)
        rows = conn.execute(sql + " ORDER BY id DESC", args).fetchall()
    finally:
        conn.close()
    return [Suggestion(**dict(row)) for row in rows]


# --- what changed (CHANGES.md) ----------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeDay:
    date: str
    items: tuple[str, ...]


def parse_changes(source: str) -> list[ChangeDay]:
    """The dated list in CHANGES.md: each ``## <date>`` heading starts a day, each ``- <words>`` line under
    it is one change. Everything else (the title, the introduction) is ignored. Only plain strings come
    out; the templates escape them, so no HTML or link in the file is ever passed through."""
    days: list[ChangeDay] = []
    date: str | None = None
    items: list[str] = []
    for line in source.splitlines():
        if line.startswith("## "):
            if date is not None and items:
                days.append(ChangeDay(date, tuple(items)))
            date, items = clean_text(line[3:]), []
        elif line.startswith("- ") and date is not None:
            item = clean_text(line[2:])
            if item:
                items.append(item)
    if date is not None and items:
        days.append(ChangeDay(date, tuple(items)))
    return days


def read_changes(path: Path = CHANGES_PATH) -> list[ChangeDay]:
    try:
        return parse_changes(path.read_text(encoding="utf-8"))
    except OSError:
        return []


def recent_changes(days: list[ChangeDay], count: int = 3) -> list[tuple[str, str]]:
    """The newest ``count`` changes, each with its date."""
    return [(d.date, item) for d in days for item in d.items][:count]


# --- command line -----------------------------------------------------------------------------------


def _print_plain(rows: Sequence[Suggestion]) -> None:
    if not rows:
        print("No suggestions.")
        return
    for s in rows:
        page = s.from_page or "an unknown page"
        print(f"#{s.id}  {s.status_word}  {s.when}  {s.display_name} ({s.username})  from {page}")
        for line in s.text.split("\n"):
            print(f"    {line}")
        print()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.suggestions",
                                     description="Read the dashboard's suggestions (read-only).")
    sub = parser.add_subparsers(dest="command", required=True)
    ls = sub.add_parser("list", help="list suggestions, newest first")
    ls.add_argument("--data", type=Path, default=None,
                    help="the board's data folder (default: the folder of FACTORY_DASH_DB, as the board uses)")
    ls.add_argument("--status", choices=STATUSES, default=None, help="only suggestions with this status")
    ls.add_argument("--json", action="store_true", help="one JSON list instead of text")
    args = parser.parse_args(argv)
    path = db_path_for(args.data or default_data_dir())
    if not path.exists():
        print(f"No suggestions yet: there is no {DB_NAME} in {path.parent}.", file=sys.stderr)
        if args.json:
            print("[]")
        return 0
    rows = list_all(path, args.status)
    if args.json:
        print(json.dumps([asdict(s) for s in rows], indent=2, ensure_ascii=False))
    else:
        _print_plain(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
