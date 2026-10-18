"""Cost per feature and per project, from the copy of LiteLLM's spend log (requirements 3–5).

**The join.** Each factory model call carries Forge's correlation id in its `x-feature-id` tag
(checked against the live spend log on 18 October 2026: every factory tag value is a correlation id
in Forge's record). A planning run and the build it queues share one correlation id; a mission's
parts carry the mission's id with a part suffix (`…-B-a2`), which is again that part's own planning
and build correlation id. So a feature's calls are the calls tagged with any of its correlation
ids: its queue item's, its planning run's and each of its builds'. A mission's own merge has the
mission's correlation id; its calls (if any) count for the project, not for a feature.

**Not measured, never zero.**
- No import has ever succeeded: every cost cell says "Cost data not available".
- A feature sent before tagging started (17 October 2026, 06:57 UTC) has no per-feature cost:
  "Not measured: calls before 17 October were not tagged by feature". (Design §3: splitting older
  calls by time window would hand one feature's calls to another, because builds overlap.)
- Calls whose model has no price are "not priced" and are left out of the sum, and the slot says so.
- If the last good import is more than 10 minutes old, every figure carries "cost data last read at
  08:14 — may be incomplete". A Forge outage and a cost outage are reported separately.

**Stages.** Within one feature, a call belongs to planning if it started before the feature's first
build started, to building while a build was running, and to "merge and check" after the build
finished. Calls of one feature cannot be confused with another's, because they are joined by tag
first. The model name (which shows the role) is stored with every call but is not shown (Rich,
17 October 2026).

The chat's read functions (`dbread.cost_summary`, `dbread.time_summary`, for part D) return the
`CostSummary` and `TimeSummary` shapes defined at the bottom of this module.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from backend.prices import PriceList, load_prices
from backend.projector.cost_import import stamp
from backend.readmodel import viewmodels as vm
from backend.readmodel.factory import Factory, Feature, Mission, parse_dt
from backend.readmodel.timing import build_start

#: Cost figures carry a "may be incomplete" label once the last good import is this old.
COST_STALE_SECS = 600


# --- freshness ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class CostFreshness:
    read_at: datetime | None
    attempted_at: datetime | None
    error: str
    earliest: datetime | None

    def state(self, now: datetime) -> str:
        """'never' (no import has succeeded), 'fresh', or 'stale' (last good import over 10 min ago)."""
        if self.read_at is None:
            return "never"
        return "fresh" if (now - self.read_at).total_seconds() < COST_STALE_SECS else "stale"

    def stale_text(self, now: datetime) -> str:
        if self.read_at is None:
            return ""
        return f"cost data last read at {vm.fmt_wall(self.read_at, now=now)} — may be incomplete"


def cost_freshness(conn: sqlite3.Connection) -> CostFreshness:
    try:
        row = conn.execute(
            "SELECT last_success_at, last_attempt_at, last_error, earliest_start FROM cost_feed_status "
            "WHERE source='litellm'").fetchone()
    except sqlite3.Error:
        row = None
    if row is None:
        return CostFreshness(None, None, "", None)
    return CostFreshness(read_at=parse_dt(row[0]), attempted_at=parse_dt(row[1]), error=str(row[2] or ""),
                         earliest=parse_dt(row[3]))


# --- totals ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Totals:
    calls: int = 0
    failed: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0           # nominal USD, priced calls only
    estimated: int = 0          # calls priced here from tokens (LiteLLM recorded no spend)
    unpriced: int = 0           # calls with tokens on a model that has no price

    def __add__(self, other: Totals) -> Totals:
        return Totals(self.calls + other.calls, self.failed + other.failed, self.tokens_in + other.tokens_in,
                      self.tokens_out + other.tokens_out, self.cost + other.cost,
                      self.estimated + other.estimated, self.unpriced + other.unpriced)


def _sum(items: Iterable[Totals]) -> Totals:
    out = Totals()
    for t in items:
        out = out + t
    return out


_AGG = ("COUNT(*), SUM(status = 'failure'), SUM(prompt_tokens), SUM(completion_tokens), "
        "SUM(COALESCE(cost_usd, 0)), SUM(cost_basis = 'estimated'), SUM(cost_basis = 'not priced')")


def _totals(row: tuple[object, ...]) -> Totals:
    def i(v: object) -> int:
        return int(v) if isinstance(v, int | float) else 0

    c = row[4]
    return Totals(i(row[0]), i(row[1]), i(row[2]), i(row[3]), float(c) if isinstance(c, int | float) else 0.0,
                  i(row[5]), i(row[6]))


def window_totals(conn: sqlite3.Connection, lo: datetime, hi: datetime, *, tagged_only: bool = False) -> Totals:
    """Every call that started in [lo, hi): factory-wide usage, tagged or not."""
    extra = " AND feature_tag IS NOT NULL" if tagged_only else ""
    row = conn.execute(f"SELECT {_AGG} FROM spend_calls WHERE started_at >= ? AND started_at < ?{extra}",
                       (stamp(lo), stamp(hi))).fetchone()
    return _totals(row)


@dataclass(frozen=True)
class Call:
    tag: str
    started_at: datetime
    totals: Totals


def _parse_stamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


# --- the cost book: one read per page ------------------------------------------------------------


@dataclass
class CostBook:
    """Everything the pages need about cost, from one read of the store."""

    freshness: CostFreshness
    prices: PriceList
    now: datetime
    by_tag_hour: dict[str, dict[str, Totals]] = field(default_factory=dict)  # tag -> UTC hour 'YYYY-MM-DDTHH' -> totals

    @property
    def state(self) -> str:
        return self.freshness.state(self.now)

    def tag_totals(self, tags: Iterable[str]) -> Totals:
        return _sum(t for tag in tags for t in self.by_tag_hour.get(tag, {}).values())

    def tag_hours(self, tags: Iterable[str]) -> dict[str, Totals]:
        out: dict[str, Totals] = defaultdict(Totals)
        for tag in tags:
            for hour, t in self.by_tag_hour.get(tag, {}).items():
                out[hour] = out[hour] + t
        return dict(out)

    # --- slots ---

    def not_available(self) -> vm.SpendSlot:
        why = "LiteLLM's spend log has not been read yet"
        if self.freshness.error:
            why += f": {self.freshness.error}"
        return vm.SpendSlot(state="not_available", label="Cost data not available", reason=why + ".")

    def slot(self, t: Totals, *, scope: str = "") -> vm.SpendSlot:
        """A measured figure (or the reason there is none) for some calls."""
        if self.state == "never":
            return self.not_available()
        stale = self.state == "stale"
        common = dict(price_label=self.prices.label, stale=stale,
                      stale_text=self.freshness.stale_text(self.now) if stale else "",
                      calls=t.calls, failed_calls=t.failed, tokens_in=t.tokens_in, tokens_out=t.tokens_out,
                      estimated_calls=t.estimated, unpriced_calls=t.unpriced, scope=scope)
        if t.calls == 0:
            return vm.SpendSlot(state="measured", label="No model calls recorded", amount_usd=0.0, **common)  # type: ignore[arg-type]
        if t.unpriced and t.unpriced == t.calls - t.failed:
            return vm.SpendSlot(state="not_measured", label="Not measured",
                                reason="These calls were made on a model with no price in the list.", **common)  # type: ignore[arg-type]
        reason = (f"{t.unpriced} of these calls were on a model with no price and are not counted."
                  if t.unpriced else "")
        return vm.SpendSlot(state="measured", value=vm.fmt_usd(t.cost), label="nominal", amount_usd=t.cost,
                            estimated=t.estimated > 0, reason=reason, **common)  # type: ignore[arg-type]

    def tagging_started(self) -> datetime:
        return self.prices.tagging_started_at

    def before_tagging(self, f: Feature) -> bool:
        start = f.sent_at or (f.planning.started_at if f.planning else None)
        return start is None or start < self.tagging_started()

    def feature_slot(self, f: Feature) -> vm.SpendSlot:
        if self.state == "never":
            return self.not_available()
        if self.before_tagging(f):
            return vm.SpendSlot(state="not_measured", label="Not measured",
                                reason=f"Calls before {_day(self.tagging_started())} were not tagged by feature.",
                                price_label=self.prices.label)
        return self.slot(self.tag_totals(feature_tags(f)), scope="feature")


def _day(dt: datetime) -> str:
    local = vm.to_london(dt)
    return f"{local.day} {local.strftime('%B')}"


def feature_tags(f: Feature) -> set[str]:
    """The correlation ids a feature's calls are tagged with."""
    tags = {b.correlation_id for b in f.builds}
    if f.planning is not None:
        tags.add(f.planning.correlation_id)
    if f.queue is not None:
        tags.add(f.queue.correlation_id)
    return {t for t in tags if t}


def mission_tags(m: Mission) -> set[str]:
    tags = {m.correlation_id} if m.correlation_id else set()
    if m.merge_build is not None and m.merge_build.correlation_id:
        tags.add(m.merge_build.correlation_id)
    return tags


def load_cost_book(conn: sqlite3.Connection, now: datetime, prices: PriceList | None = None) -> CostBook:
    """Tagged calls summed by tag and UTC hour (hours are enough for London days and weeks)."""
    book = CostBook(freshness=cost_freshness(conn), prices=prices or load_prices(), now=now)
    try:
        rows = conn.execute(
            f"SELECT feature_tag, substr(started_at, 1, 13), {_AGG} FROM spend_calls "
            "WHERE feature_tag IS NOT NULL GROUP BY 1, 2").fetchall()
    except sqlite3.Error:
        rows = []
    for r in rows:
        book.by_tag_hour.setdefault(str(r[0]), {})[str(r[1])] = _totals(r[2:])
    return book


# --- stages within one feature ---------------------------------------------------------------------

STAGES = (("planning", "Planning"), ("build", "Building"), ("review", "Merge and check"))


def feature_calls(conn: sqlite3.Connection, f: Feature) -> list[Call]:
    tags = sorted(feature_tags(f))
    if not tags:
        return []
    marks = ", ".join("?" * len(tags))
    rows = conn.execute(
        f"SELECT feature_tag, started_at, 1, status = 'failure', prompt_tokens, completion_tokens, "
        f"COALESCE(cost_usd, 0), cost_basis = 'estimated', cost_basis = 'not priced' "
        f"FROM spend_calls WHERE feature_tag IN ({marks}) ORDER BY started_at", tags).fetchall()
    return [Call(str(r[0]), _parse_stamp(str(r[1])), _totals(r[2:])) for r in rows]


def stage_of(f: Feature, at: datetime) -> str:
    """planning | build | review, by when the call started against the feature's builds."""
    starts = [(build_start(b), b) for b in f.builds]
    for start, b in starts:
        if start is None or at < start:
            continue
        end = b.completed_at if b.status not in ("QUEUED", "PREPARING", "RUNNING", "PAUSED", "FINALISING") else None
        if end is None or at <= end:
            return "build"
    first = min((s for s, _b in starts if s), default=None)
    if first is None or at < first:
        return "planning"
    return "review"


def split_by_stage(f: Feature, calls: Iterable[Call]) -> dict[str, Totals]:
    out: dict[str, Totals] = {k: Totals() for k, _ in STAGES}
    for c in calls:
        key = stage_of(f, c.started_at)
        out[key] = out[key] + c.totals
    return out


# --- London calendar helpers -----------------------------------------------------------------------


def week_start(dt: datetime) -> datetime:
    """Monday 00:00 London time of dt's week, as UTC."""
    local = vm.to_london(dt)
    monday = local.date() - timedelta(days=local.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=vm.LONDON).astimezone(UTC)


def day_start(dt: datetime) -> datetime:
    local = vm.to_london(dt).date()
    return datetime(local.year, local.month, local.day, tzinfo=vm.LONDON).astimezone(UTC)


def hour_to_dt(hour: str) -> datetime:
    return datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=UTC)


# --- read functions for the chat (part D) ----------------------------------------------------------


@dataclass(frozen=True)
class CostSummary:
    """Plain figures for a feature or a project over a period, with the records they came from."""

    scope: str                     # 'feature' | 'project'
    name: str                      # feature label, or the project
    period: str                    # 'all time' or 'YYYY-MM-DD to YYYY-MM-DD'
    state: str                     # 'measured' | 'not_measured' | 'not_available'
    reason: str
    calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: float | None
    estimated: bool
    stale: bool
    price_label: str
    refs: tuple[str, ...]          # feature codes / request ids the figures cover


@dataclass(frozen=True)
class TimeSummary:
    scope: str
    name: str
    period: str
    features: int
    elapsed_secs: float
    machine_secs: float
    waiting_secs: float
    waiting_parts: tuple[tuple[str, float], ...]   # (label, seconds), waiting first: what James can act on
    so_far: bool
    refs: tuple[str, ...]


def scope(factory: Factory, feature: str | None, project: str | None) -> tuple[str, str, list[Feature]]:
    if feature:
        f = factory.feature(feature)
        if f is None:
            for cand in factory.features:
                if feature.lower() in cand.title.lower():
                    f = cand
                    break
        return ("feature", f.label, [f]) if f else ("feature", feature, [])
    name = project or ""
    return "project", name, [f for f in factory.features if f.project == name]


def period(lo: datetime | None, hi: datetime | None) -> str:
    if lo is None or hi is None:
        return "all time"
    return f"{vm.to_london(lo).date().isoformat()} to {vm.to_london(hi - timedelta(seconds=1)).date().isoformat()}"
