"""Copy LiteLLM's spend log into the dashboard's store, read-only, every 60 seconds.

Design of 17 October 2026, requirement 3. LiteLLM stores each model call's `x-feature-id` header
(Forge's correlation id) as a tag. The feed reads the log through a database login that may only
SELECT from "LiteLLM_SpendLogs", and keeps one summary row per call: the feature tag, the model
name (which shows the role), tokens in and out, LiteLLM's spend, the times and the call id.

- **Idempotent and overlapping.** Every row is stored under LiteLLM's unique `request_id` (insert,
  or ignore if present). LiteLLM writes a call's row after the call ends, in batches, so rows can
  arrive late and out of start-time order. Each pass therefore re-reads every row whose start
  time is after the newest stored start time minus 3 hours, in pages ordered by
  (start time, request id). After an outage the same rule applies, so nothing is skipped.
- **Once a day** the feed re-reads the previous 3 full days if LiteLLM's row count for any of those
  days differs from its own, and inserts what is missing.
- **Prices.** LiteLLM's `spend` is used where it is set. A call with tokens but no spend (made before
  the prices were set) is priced here from its tokens with the dated list in prices.yaml and
  marked "estimated". A call whose model has no price is "not priced": never counted as 0.
- **Freshness** is recorded in `cost_feed_status`, apart from Forge's: one can be healthy while the
  other is stale. A pass that fails records a plain sentence (never the connection string) and
  keeps the last good copy.

The database address comes from `FACTORY_DASH_SPEND_DB_URL` in the feed's private environment file.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from backend.prices import PriceList, load_prices
from backend.projector.store import iso, record_change

IMPORT_INTERVAL_SECS = 60
OVERLAP = timedelta(hours=3)
BATCH = 5000
RECONCILE_EVERY = timedelta(hours=24)
RECONCILE_DAYS = 3
TAG_PREFIX = "x-feature-id:"
SOURCE = "litellm"
ENV_URL = "FACTORY_DASH_SPEND_DB_URL"

# Page sections showing cost are told to refresh when new calls arrive. (Every open page also
# refreshes its content when the change count moves: dbread.data_version.)
COST_PANELS: frozenset[str] = frozenset({"p2", "p7"})


def stamp(dt: datetime) -> str:
    """Fixed-width UTC text, so text order is time order: 2026-10-18T08:33:41.148000Z."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class SpendRow:
    """One row of LiteLLM's spend log, as read. Times are UTC (LiteLLM stores them without a zone)."""

    request_id: str
    start: datetime
    end: datetime | None
    model_name: str
    provider_model: str
    key_alias: str
    tags: Sequence[str]
    status: str
    prompt_tokens: int
    completion_tokens: int
    spend: float


class SpendSource(Protocol):
    """Where spend-log rows come from: LiteLLM's database in production, a fake in tests."""

    def rows_after(self, start: datetime, after_id: str, limit: int,
                   before: datetime | None = None) -> list[SpendRow]:
        """Rows with (start time, request id) after the given pair, oldest first, at most `limit`;
        only those starting before `before` when it is given."""
        ...

    def counts_by_day(self, since: datetime) -> dict[str, int]:
        """Rows per UTC day ('YYYY-MM-DD') for days starting at or after `since`."""
        ...

    def close(self) -> None: ...


def feature_tag(tags: Sequence[str] | None) -> str | None:
    """The x-feature-id value from LiteLLM's request tags ('x-feature-id: <correlation id>')."""
    for t in tags or ():
        if isinstance(t, str) and t.lower().startswith(TAG_PREFIX):
            value = t[len(TAG_PREFIX):].strip()
            return value or None
    return None


def price_call(row: SpendRow, prices: PriceList) -> tuple[float | None, str, str | None]:
    """(nominal cost in USD, basis, price reference) for one call."""
    price = prices.find(row.provider_model)
    ref = prices.ref(price) if price else None
    if row.spend > 0:
        return row.spend, "litellm", ref or "LiteLLM's own price"
    if row.prompt_tokens + row.completion_tokens == 0:
        return 0.0, "no tokens", None
    if price is None:
        return None, "not priced", None
    cost = row.prompt_tokens * price.input_per_token + row.completion_tokens * price.output_per_token
    return cost, "estimated", ref


_INSERT = """INSERT OR IGNORE INTO spend_calls (request_id, started_at, ended_at, model_name, provider_model,
    key_alias, feature_tag, status, prompt_tokens, completion_tokens, litellm_spend, cost_usd, cost_basis, price_ref)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def _store(dash: sqlite3.Connection, rows: Sequence[SpendRow], prices: PriceList) -> int:
    """Insert a page of rows in one transaction; returns how many were new."""
    if not rows:
        return 0
    before = dash.total_changes
    dash.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            cost, basis, ref = price_call(r, prices)
            dash.execute(_INSERT, (
                r.request_id, stamp(r.start), stamp(r.end) if r.end else None, r.model_name or None,
                r.provider_model or None, r.key_alias or None, feature_tag(r.tags), r.status or None,
                int(r.prompt_tokens or 0), int(r.completion_tokens or 0), float(r.spend or 0.0), cost, basis, ref,
            ))
        dash.execute("COMMIT")
    except BaseException:
        dash.execute("ROLLBACK")
        raise
    return dash.total_changes - before


def _newest_start(dash: sqlite3.Connection) -> datetime | None:
    row = dash.execute("SELECT MAX(started_at) FROM spend_calls").fetchone()
    if not row or not row[0]:
        return None
    return datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _page_through(dash: sqlite3.Connection, source: SpendSource, prices: PriceList, start: datetime,
                  before: datetime | None = None) -> int:
    """Read every row from `start` (up to `before`) in keyset pages; returns how many were new."""
    cursor_start, cursor_id = start, ""
    inserted = 0
    while True:
        rows = source.rows_after(cursor_start, cursor_id, BATCH, before)
        inserted += _store(dash, rows, prices)
        if len(rows) < BATCH:
            return inserted
        cursor_start, cursor_id = rows[-1].start, rows[-1].request_id


def _status(dash: sqlite3.Connection) -> tuple[str | None, str | None]:
    row = dash.execute(
        "SELECT last_success_at, last_reconcile_at FROM cost_feed_status WHERE source=?", (SOURCE,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _stamp_ok(dash: sqlite3.Connection, now: datetime) -> None:
    earliest = dash.execute("SELECT MIN(started_at) FROM spend_calls").fetchone()[0]
    dash.execute(
        """INSERT INTO cost_feed_status (source, last_attempt_at, last_success_at, last_error, earliest_start)
           VALUES (?, ?, ?, NULL, ?)
           ON CONFLICT(source) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
               last_success_at=excluded.last_success_at, last_error=NULL, earliest_start=excluded.earliest_start""",
        (SOURCE, iso(now), iso(now), earliest),
    )


def stamp_error(dash: sqlite3.Connection, now: datetime, error: str) -> None:
    dash.execute(
        """INSERT INTO cost_feed_status (source, last_attempt_at, last_error) VALUES (?, ?, ?)
           ON CONFLICT(source) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
               last_error=excluded.last_error""",
        (SOURCE, iso(now), error),
    )


def _plain_error(exc: BaseException) -> str:
    """A sentence for the store and the pages. Database errors can quote the server's address, so
    only the kind of error is kept."""
    return f"could not read LiteLLM's spend log ({type(exc).__name__})"


@dataclass(frozen=True)
class ImportResult:
    ok: bool
    inserted: int = 0
    reconciled: int = 0
    error: str = ""


def reconcile(dash: sqlite3.Connection, source: SpendSource, prices: PriceList, now: datetime) -> tuple[int, str]:
    """Compare the last 3 full days (and today) row by row count with LiteLLM's; re-read any day
    that differs. Returns (rows inserted, a note)."""
    today = datetime(now.year, now.month, now.day, tzinfo=UTC)
    since = today - timedelta(days=RECONCILE_DAYS)
    theirs = source.counts_by_day(since)
    ours = dict(dash.execute(
        "SELECT substr(started_at, 1, 10), COUNT(*) FROM spend_calls WHERE started_at >= ? GROUP BY 1",
        (stamp(since),)).fetchall())
    differing = sorted(d for d, n in theirs.items() if ours.get(d, 0) != n)
    inserted = 0
    for day in differing:
        lo = datetime.fromisoformat(day).replace(tzinfo=UTC)
        inserted += _page_through(dash, source, prices, lo, lo + timedelta(days=1))
    if not differing:
        note = f"the last {RECONCILE_DAYS} days match LiteLLM's row counts"
    else:
        note = f"re-read {', '.join(differing)}: {inserted} missing calls added"
    dash.execute(
        """INSERT INTO cost_feed_status (source, last_reconcile_at, reconcile_note) VALUES (?, ?, ?)
           ON CONFLICT(source) DO UPDATE SET last_reconcile_at=excluded.last_reconcile_at,
               reconcile_note=excluded.reconcile_note""",
        (SOURCE, iso(now), note),
    )
    return inserted, note


def import_once(dash: sqlite3.Connection, source: SpendSource, now: datetime | None = None,
                prices: PriceList | None = None) -> ImportResult:
    """One pass: read from the newest stored start time minus 3 hours to now, then reconcile once a
    day. Never raises for a source failure; it records the failure and keeps the last good copy."""
    now = now or datetime.now(UTC)
    prices = prices or load_prices()
    newest = _newest_start(dash)
    start = (newest - OVERLAP) if newest else datetime(1970, 1, 1, tzinfo=UTC)
    try:
        inserted = _page_through(dash, source, prices, start)
        reconciled = 0
        _last_ok, last_rec = _status(dash)
        rec_at = datetime.fromisoformat(last_rec) if last_rec else None
        if rec_at is None or now - rec_at >= RECONCILE_EVERY:
            reconciled, _note = reconcile(dash, source, prices, now)
    except Exception as exc:
        stamp_error(dash, now, _plain_error(exc))
        return ImportResult(ok=False, error=_plain_error(exc))
    _stamp_ok(dash, now)
    if inserted or reconciled:
        record_change(dash, set(COST_PANELS), [], now)
    return ImportResult(ok=True, inserted=inserted, reconciled=reconciled)


# --- LiteLLM's database --------------------------------------------------------------------------


_SELECT = """SELECT request_id, "startTime", "endTime", COALESCE(model_group, ''), COALESCE(model, ''),
    COALESCE(metadata->>'user_api_key_alias', ''), request_tags, COALESCE(status, ''), prompt_tokens,
    completion_tokens, spend
  FROM "LiteLLM_SpendLogs"
 WHERE ("startTime", request_id) > (%s, %s) {before}
 ORDER BY "startTime", request_id
 LIMIT %s"""


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


class PostgresSpendSource:
    """LiteLLM's spend log through the dashboard's SELECT-only login. One connection per pass, in a
    read-only transaction, closed at the end of the pass."""

    def __init__(self, url: str) -> None:
        import psycopg  # noqa: PLC0415 (only the feed needs the driver; the pages never import it)

        self._conn: Any = psycopg.connect(url, connect_timeout=5, application_name="factory-dashboard",
                                          options="-c statement_timeout=30000")
        self._conn.read_only = True

    def rows_after(self, start: datetime, after_id: str, limit: int,
                   before: datetime | None = None) -> list[SpendRow]:
        sql = _SELECT.format(before='AND "startTime" < %s' if before else "")
        params: tuple[object, ...] = (_naive_utc(start), after_id)
        if before:
            params += (_naive_utc(before),)
        params += (limit,)
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            out = [SpendRow(
                request_id=r[0], start=r[1].replace(tzinfo=UTC), end=r[2].replace(tzinfo=UTC) if r[2] else None,
                model_name=r[3], provider_model=r[4], key_alias=r[5], tags=list(r[6] or []), status=r[7],
                prompt_tokens=int(r[8] or 0), completion_tokens=int(r[9] or 0), spend=float(r[10] or 0.0),
            ) for r in cur.fetchall()]
        return out

    def counts_by_day(self, since: datetime) -> dict[str, int]:
        with self._conn.cursor() as cur:
            cur.execute(
                """SELECT to_char(date_trunc('day', "startTime"), 'YYYY-MM-DD'), COUNT(*)
                     FROM "LiteLLM_SpendLogs" WHERE "startTime" >= %s GROUP BY 1""",
                (_naive_utc(since),),
            )
            return {str(d): int(n) for d, n in cur.fetchall()}

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.rollback()
            self._conn.close()


def run_pass(dash: sqlite3.Connection, now: datetime | None = None) -> ImportResult:
    """One production pass: connect, import, close."""
    now = now or datetime.now(UTC)
    url = os.environ.get(ENV_URL, "")
    if not url:
        stamp_error(dash, now, "the spend log is not configured")
        return ImportResult(ok=False, error="the spend log is not configured")
    try:
        source = PostgresSpendSource(url)
    except Exception as exc:
        stamp_error(dash, now, _plain_error(exc))
        return ImportResult(ok=False, error=_plain_error(exc))
    try:
        return import_once(dash, source, now)
    finally:
        source.close()


async def cost_loop(dash: sqlite3.Connection, *, interval: int = IMPORT_INTERVAL_SECS) -> None:  # pragma: no cover
    while True:
        # A failed pass is recorded by run_pass; anything unexpected must not stop the loop. The
        # pass runs in a thread so a long first import does not hold up the other loops.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(run_pass, dash)
        await asyncio.sleep(interval)
