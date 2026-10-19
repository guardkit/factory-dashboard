"""View models: the computed shapes the query layer hands to templates.

Templates render; the query layer computes. Every number, colour, state and formatted time
arrives here already worked out, so a page and (later) the chat can never disagree. These are
plain frozen dataclasses; the only behaviour is the display formatting at the bottom, the one
place times and ages are formatted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from backend.readmodel.cards import Card
    from backend.readmodel.factory import MissionFeature

LONDON = ZoneInfo("Europe/London")

# Freshness. The feed reads Forge's record every 15 s; an as-of chip turns amber after a minute
# and red after two, and the stale-data banner appears once the last good read is two minutes old.
MIRROR_INTERVAL_SECS = 15
MIRROR_FRESH_SECS = 60
STALE_AFTER_SECS = 120
P6_POLL_SECS = 30
P6_FRESH_SECS = 2 * P6_POLL_SECS
PROJECTOR_STALE_SECS = 60


class PanelState(StrEnum):
    LIVE = "live"            # read recently, has rows
    LAGGING = "lagging"      # the last good read is old: rows are the last known ones
    TRUE_EMPTY = "empty"     # read recently, genuinely nothing to show


@dataclass(frozen=True)
class AsOf:
    """The as-of chip: when the data on a card was read. Never omitted.

    `asof_epoch` and `threshold_secs` let static/freshness.js turn the dot amber and then red on
    the wall clock without a refetch."""

    prefix: str
    label: str
    title_iso: str
    dot_state: str           # 'live' | 'lagging' | 'stale' | 'empty'
    asof_epoch: float | None = None
    threshold_secs: int | None = None


@dataclass(frozen=True)
class BandChip:
    """On-track colour, always with a word: colour is never the only signal."""

    band: str                # 'G' | 'A' | 'R' | '—'
    label: str = ""


@dataclass(frozen=True)
class SpendSlot:
    """A cost figure, or the reason there is none. An unmeasured cost is a gap, never a zero.

    States:
    - 'measured': `value` holds the nominal figure ("$2.20"); `value` is "" when the record holds
      no model calls (`label` then says "No model calls recorded").
    - 'not_measured': the calls were not tagged by feature (before 17 October 2026) or have no
      price; `reason` says which.
    - 'not_available': LiteLLM's spend log has never been read.

    `estimated` is set when some calls were priced from their tokens because LiteLLM recorded no
    spend for them. `stale` is set when the last good read of the spend log is over 10 minutes
    old, with `stale_text` to show. `price_label` names the price source and date."""

    state: str = "not_measured"
    value: str = ""
    label: str = "Not measured"
    reason: str = ""
    amount_usd: float | None = None
    calls: int | None = None
    failed_calls: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    estimated: bool = False
    estimated_calls: int = 0
    unpriced_calls: int = 0
    price_label: str = ""
    stale: bool = False
    stale_text: str = ""
    scope: str = ""          # 'feature' | 'project' | 'factory' | ''

    @property
    def measured(self) -> bool:
        return self.state == "measured"

    @property
    def empty(self) -> bool:
        """No figure to show (a stat tile's --empty state)."""
        return not self.value

    @property
    def text(self) -> str:
        """One line for simple markup: "$2.20 nominal", "Not measured", "Cost data not available"."""
        if self.value:
            return f"{self.value} nominal" + (" (part estimated)" if self.estimated else "")
        return self.label

    # Usage (calls and tokens) is shown whenever the record holds it, apart from the price: calls on
    # a model with no price still have known counts, and only their cost is "Not measured". The
    # counts are None (shown "—") only when no calls were read: before tagging, or no import yet.

    @property
    def calls_text(self) -> str:
        """Model calls for a table cell: "312", or "—" when no calls were read."""
        return str(self.calls) if self.calls is not None else "—"

    @property
    def tokens_in_text(self) -> str:
        return fmt_tokens(self.tokens_in) if self.tokens_in is not None else "—"

    @property
    def tokens_out_text(self) -> str:
        return fmt_tokens(self.tokens_out) if self.tokens_out is not None else "—"

    @property
    def detail(self) -> str:
        """Calls and tokens: "312 model calls, 13.4 M tokens in, 183 k tokens out"."""
        if self.calls is None:
            return ""
        noun = "call" if self.calls == 1 else "calls"
        tokens_in, tokens_out = fmt_tokens(self.tokens_in or 0), fmt_tokens(self.tokens_out or 0)
        return f"{self.calls} model {noun}, {tokens_in} tokens in, {tokens_out} tokens out"


@dataclass(frozen=True)
class TimeSegment:
    """One period of a feature's (or project's) time. `seg` is the bar class suffix: plan, build,
    review (merge and check), wait, none (between steps)."""

    key: str
    label: str
    kind: str                # 'machine' | 'wait' | 'none'
    seg: str
    seconds: float
    text: str
    share: float             # of elapsed, 0..1
    open: bool = False       # still going on now


@dataclass(frozen=True)
class TimeSlot:
    """Elapsed, machine and waiting time. 'not_measured' when the record has no time it was sent.
    Waiting is what James can act on, so `waiting_parts` lists it by kind, longest first."""

    state: str = "not_measured"
    elapsed: str = ""
    elapsed_secs: float = 0.0
    machine: str = ""
    machine_secs: float = 0.0
    waiting: str = ""
    waiting_secs: float = 0.0
    so_far: bool = False
    segments: tuple[TimeSegment, ...] = ()
    reason: str = ""
    not_measured_count: int = 0   # features left out of a sum because their time is not measured

    @property
    def measured(self) -> bool:
        return self.state == "measured"

    @property
    def waiting_parts(self) -> tuple[TimeSegment, ...]:
        return tuple(sorted((s for s in self.segments if s.kind == "wait"), key=lambda s: -s.seconds))

    @property
    def machine_parts(self) -> tuple[TimeSegment, ...]:
        return tuple(s for s in self.segments if s.kind == "machine")


@dataclass(frozen=True)
class StageCostRow:
    """Cost of one stage of a feature: planning, building, merge and check."""

    key: str                 # 'planning' | 'build' | 'review'
    label: str
    seg: str                 # bar class suffix: plan | build | review
    spend: SpendSlot
    share: float             # of the feature's priced cost, 0..1


@dataclass(frozen=True)
class WeekRow:
    """One London week (Monday to Sunday) of a project, or of the whole factory."""

    week_from: str           # ISO date of the Monday
    week_label: str          # "13 Oct"
    sent: int                # features sent that week
    delivered: int           # merged and checked that week
    spend: SpendSlot         # tagged calls that started that week
    time: TimeSlot           # summed over the features sent that week
    untagged: SpendSlot | None = None   # factory-wide calls that week with no feature tag


@dataclass(frozen=True)
class Panel:
    panel_id: str
    title: str
    state: PanelState
    as_of: AsOf


# --- the journey ------------------------------------------------------------------------------


@dataclass(frozen=True)
class JourneyStation:
    name: str
    glyph_state: str         # 'passed' | 'current' | 'waiting' (on a person) | 'future' | 'stopped'


@dataclass(frozen=True)
class JourneyBar:
    stations: tuple[JourneyStation, ...]
    current_label: str


# --- feature lists (Now: in progress; Features; project page) ----------------------------------


@dataclass(frozen=True)
class FeatureRow:
    title: str
    code: str
    link: str
    project: str
    project_link: str
    status_text: str
    journey: JourneyBar
    band: BandChip
    elapsed: str             # "2 h 10 min" or "2 h 10 min so far"
    sent_label: str
    sent_title: str
    in_progress: bool
    spend: SpendSlot = field(default_factory=SpendSlot)
    time: TimeSlot = field(default_factory=TimeSlot)


@dataclass(frozen=True)
class FeaturesPanel(Panel):
    rows: tuple[FeatureRow, ...] = ()
    empty_copy: str = "Nothing in progress."
    note: str = ""


# --- Now: waiting for a person, recent activity, planning ------------------------------------


@dataclass(frozen=True)
class NeedsItem:
    what: str                # "Waiting for the merge decision"
    subject: str             # the feature's label, or "mission m-…'s merge"
    link: str
    age_label: str
    age_title: str
    band: BandChip
    cards: tuple[Card, ...] = ()      # the open cards behind this wait, with what an answer needs


@dataclass(frozen=True)
class NeedsPanel(Panel):
    items: tuple[NeedsItem, ...] = ()
    empty_copy: str = "Nothing is waiting for a person."

    @property
    def card_ids(self) -> tuple[str, ...]:
        return tuple(c.request_id for it in self.items for c in it.cards)


@dataclass(frozen=True)
class ActivityRow:
    time_label: str
    time_title: str
    subject: str
    link: str
    step: str                # "Merge decision", "Build started", "Spec approval"
    result: str              # "approved", "declined", "passed", "failed"


@dataclass(frozen=True)
class ActivityPanel(Panel):
    rows: tuple[ActivityRow, ...] = ()
    empty_copy: str = "No recent activity."


@dataclass(frozen=True)
class PlanningRow:
    title: str
    link: str
    project: str
    status_text: str
    age: str
    requester: str


@dataclass(frozen=True)
class PlanningPanel(Panel):
    rows: tuple[PlanningRow, ...] = ()
    empty_copy: str = "No planning in the last 7 days."
    more: int = 0            # rows left off the Now page (they are all on the Features page)


# --- machinery ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceRow:
    name: str
    endpoint: str
    dot: str                 # 'ok' | 'fail' | 'unknown'
    detail: str
    resident_models: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServingPanel(Panel):
    services: tuple[ServiceRow, ...] = ()


@dataclass(frozen=True)
class FeedPanel(Panel):
    """Is the dashboard itself up to date? The feed's heartbeat and its last read of Forge."""

    heartbeat_ok: bool = False
    heartbeat_label: str = ""
    read_label: str = ""
    error: str = ""


# --- Delivered -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveredRow:
    delivered_date: str
    delivered_title: str
    title: str
    code: str
    link: str
    project: str
    project_link: str
    commit: str              # short id of the commit that was checked and published, or ""
    spend: SpendSlot
    time: TimeSlot = field(default_factory=TimeSlot)


@dataclass(frozen=True)
class DeliveredPanel(Panel):
    rows: tuple[DeliveredRow, ...] = ()
    delivered_count: int = 0
    window_from: str = ""
    window_to: str = ""
    other_endings: str = ""  # "Also finished in this window: 9 built, not merged; 1 merge check failed."
    empty_copy: str = "Nothing was merged and checked in this window."


# --- Issues ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueRow:
    kind: str                # plain words: "Build failed", "Waiting for a person", "Stalled"
    band: BandChip
    subject: str
    link: str
    project: str
    opened_label: str
    opened_title: str
    age: str
    detail: str
    closed_label: str = ""


# --- the feature page ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalRow:
    name: str
    value: str
    band: BandChip
    norm: str
    note: str


@dataclass(frozen=True)
class AttemptRow:
    build_id: str
    queued_label: str
    queued_title: str
    status_text: str
    started_by: str          # "a request", "the factory (a fix)"


@dataclass(frozen=True)
class WaitRow:
    kind: str
    started_label: str
    started_title: str
    ended_label: str         # "waiting now" while open
    duration: str
    open: bool = False       # True while the wait is still going on


@dataclass(frozen=True)
class FeaturePage:
    title: str
    code: str
    key: str
    project: str
    project_link: str
    request_text: str
    sent_label: str
    sent_title: str
    status_text: str
    mission_line: str
    journey: JourneyBar
    as_of: AsOf
    verdict: BandChip
    verdict_heading: str     # "On track", or "How it ended" for a feature finished without delivery
    verdict_reason: str
    signals: tuple[SignalRow, ...]
    norms_line: str
    elapsed: str
    attempts: tuple[AttemptRow, ...]
    waits: tuple[WaitRow, ...]
    issues: tuple[IssueRow, ...]
    activity: tuple[ActivityRow, ...]
    spend: SpendSlot
    time: TimeSlot = field(default_factory=TimeSlot)
    cost_by_stage: tuple[StageCostRow, ...] = ()
    cards: tuple[Card, ...] = ()       # the feature's cards, open and closed (backend/readmodel/cards.py)
    mission_link: str = ""


@dataclass(frozen=True)
class MissionPage:
    """A mission: its plan as Forge filed it, its features on record and its cards."""

    id: str
    title: str
    project: str
    project_link: str
    status_text: str
    status_detail: str       # the stop while it drains, or the reason it closed, in plain words
    created_label: str
    created_title: str
    as_of: AsOf
    plan: tuple[MissionFeature, ...]
    features: tuple[tuple[str, str, str], ...]   # (label, link, where it stands)
    cards: tuple[Card, ...]


# --- the project page -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectView:
    chrome: PageChrome
    project: str
    features: FeaturesPanel
    delivered_7d: int
    delivered_total: int
    in_progress: int
    issues: tuple[IssueRow, ...]
    spend: SpendSlot
    open_issues_total: int = 0   # every unresolved issue, the listed ones and the older hidden ones
    hidden_line: str = ""        # how many old open failures the 14-day cut-off leaves out
    time: TimeSlot = field(default_factory=TimeSlot)        # summed over the project's features
    weeks: tuple[WeekRow, ...] = ()                          # newest first
    spend_week: SpendSlot = field(default_factory=SpendSlot)  # this week's tagged calls
    untagged_note: str = ""      # how much factory-wide usage had no feature tag this week
    features_total: int = 0      # every feature of the project on record
    linear_url: str = ""         # the project's Linear project (https://linear.app/ only), or ""

    @property
    def open_issues_words(self) -> str:
        """The open-issues line as the page says it, with the older-failures sentence after it."""
        n = self.open_issues_total
        words = f"{n} open {'issue' if n == 1 else 'issues'}." if n else "No open issues for this project."
        return f"{words} {self.hidden_line}".strip()


# --- weekly report ------------------------------------------------------------------------------


@dataclass(frozen=True)
class MilestoneRow:
    """One plan-vs-actual row. The four client-safe fields are milestone_id, title, target_window
    and band; `detail` is internal only. `no_baseline` marks work outside every milestone: shown as
    "no plan agreed", never as on target."""

    milestone_id: str
    title: str
    target_window: str
    band: BandChip
    deviation: str           # 'ahead' | 'on_target' | 'behind' | 'no_baseline'
    detail: str = ""
    feature_id: str = ""
    feature_link: str = ""


@dataclass(frozen=True)
class ReportHeadline:
    delivered_count: int
    in_flight_count: int
    behind_count: int
    on_target_count: int
    ahead_count: int
    spend: SpendSlot                 # tagged calls of the report's projects in the window
    withheld: bool = False
    withheld_since: str = ""
    waiting: TimeSlot = field(default_factory=TimeSlot)       # time of the features delivered in the window
    # Every call in the window, tagged or not: only on the operator's whole-factory report (None on a
    # client-scoped report, which shows nothing outside its own projects).
    factory_usage: SpendSlot | None = None


@dataclass(frozen=True)
class PlanVsActual:
    milestones: tuple[MilestoneRow, ...]
    withheld: bool = False
    withheld_since: str = ""


@dataclass(frozen=True)
class ReportIssueRow:
    kind: str
    band: BandChip
    headline: str
    feature_id: str
    scope_link: str
    is_open: bool


@dataclass(frozen=True)
class IssuesEncountered:
    opened: int
    closed: int
    oldest_open_age: str
    rows: tuple[ReportIssueRow, ...] = ()


@dataclass(frozen=True)
class WeeklyReport:
    chrome: PageChrome
    tenant_slug: str
    tenant_display: str
    window_from: str
    window_to: str
    headline: ReportHeadline
    plan: PlanVsActual
    delivered_rows: tuple[DeliveredRow, ...]
    in_flight_rows: tuple[FeatureRow, ...]
    issues: IssuesEncountered
    client_tenants: tuple[tuple[str, str], ...] = ()


# The client-safe export renders only these shapes: delivered rows (title, project, date, commit)
# and the four sanctioned plan fields. No issues, no internals, and no costs: Rich's approved brief
# for part B keeps cost out of the client export (it supersedes the design's §8 sentence about adding
# per-project spend to it).


@dataclass(frozen=True)
class ExportDeliveredRow:
    feature_id: str
    title: str
    project: str
    delivered_date: str
    commit: str


@dataclass(frozen=True)
class ExportMilestone:
    milestone_id: str
    title: str
    target_window: str
    band: BandChip


@dataclass(frozen=True)
class ExportReport:
    tenant_display: str
    window_from: str
    window_to: str
    as_of: AsOf
    delivered_rows: tuple[ExportDeliveredRow, ...]
    milestones: tuple[ExportMilestone, ...]
    milestones_withheld: bool = False
    withheld_since: str = ""


# --- page chrome ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Banner:
    kind: str                # 'stale' (server-rendered) | 'sse_paused' (added by sse.js)
    text: str


@dataclass(frozen=True)
class PageChrome:
    feed_state: str          # 'live' | 'stale'
    read_as_of: AsOf
    banner: Banner | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    version: str = ""        # the version of the data behind the page (see dbread.data_version)
    cost_banner: Banner | None = None   # the spend log's own staleness, apart from Forge's


# --- display formatting: the one place time and age are formatted ------------------------------------


def to_london(dt: datetime) -> datetime:
    return dt.astimezone(LONDON)


def fmt_wall(dt: datetime, *, now: datetime) -> str:
    """08:14:05 for today, 17 Oct 08:14 otherwise (London time)."""
    ldt = to_london(dt)
    if ldt.date() == to_london(now).date():
        return ldt.strftime("%H:%M:%S")
    return f"{ldt.day} {ldt.strftime('%b %H:%M')}"


def fmt_day(dt: datetime) -> str:
    ldt = to_london(dt)
    return f"{ldt.day} {ldt.strftime('%b %Y')}"


def fmt_iso(dt: datetime) -> str:
    return to_london(dt).isoformat()


def fmt_age(seconds: float) -> str:
    """'2 d 4 h', '2 h 14 min', '38 min', '4 s'."""
    secs = int(max(0, seconds))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, s = divmod(rem, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {mins:02d} min"
    if mins:
        return f"{mins} min"
    return f"{s} s"


def fmt_usd(amount: float) -> str:
    """'$2.20', '$0.04', '$0.0031' (small figures keep two significant digits), '$1,204'."""
    if amount >= 1000:
        return f"${amount:,.0f}"
    if amount >= 0.01 or amount == 0:
        return f"${amount:,.2f}"
    return f"${amount:.2g}" if amount >= 0.0001 else "under $0.0001"


def fmt_tokens(n: int) -> str:
    """'13.4 M', '183 k', '940'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} M"
    if n >= 1000:
        return f"{n / 1000:.0f} k"
    return str(n)


def dot_state_for(age_secs: float, threshold_secs: int) -> str:
    """Green while fresh, amber past the threshold, red past twice it."""
    if age_secs < threshold_secs:
        return "live"
    if age_secs < 2 * threshold_secs:
        return "lagging"
    return "stale"
