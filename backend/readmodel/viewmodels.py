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
from zoneinfo import ZoneInfo

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
    """Cost is not measured yet (it arrives with part B of the October design). Shown as a gap,
    never as a zero."""

    text: str = "Cost: not measured yet"
    plain_language: bool = False


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


@dataclass(frozen=True)
class NeedsPanel(Panel):
    items: tuple[NeedsItem, ...] = ()
    empty_copy: str = "Nothing is waiting for a person."


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
    spend: SpendSlot
    withheld: bool = False
    withheld_since: str = ""


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


# The client-safe export renders only these shapes: delivered rows (title, project, date, commit,
# the cost gap) and the four sanctioned plan fields. No issues, no internals.


@dataclass(frozen=True)
class ExportDeliveredRow:
    feature_id: str
    title: str
    project: str
    delivered_date: str
    commit: str
    spend: SpendSlot


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
    spend: SpendSlot
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


def dot_state_for(age_secs: float, threshold_secs: int) -> str:
    """Green while fresh, amber past the threshold, red past twice it."""
    if age_secs < threshold_secs:
        return "live"
    if age_secs < 2 * threshold_secs:
        return "lagging"
    return "stale"
