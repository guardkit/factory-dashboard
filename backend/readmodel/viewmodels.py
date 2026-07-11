"""View models — the pre-computed shapes the query layer hands to templates.

ux-spec-2026-07-11.md §1.3 (ADR-DASH-005 rule 1): *templates render, the query layer computes.*
Every number, band, state, freshness figure, and formatted time arrives here already computed;
Jinja does no aggregation, no verdict logic, no freshness arithmetic. Panels and the (later) D4
chat tools consume the same query layer, so they can never disagree.

These are plain frozen dataclasses (mypy --strict clean, trivially renderable by attribute access
in Jinja). No behaviour lives here beyond the small display-formatting helpers at the bottom,
which are the ONE place wall-clock/age formatting is done (never in a template).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

# Freshness thresholds (ux §5.2 defaults; the real constants live beside the query layer and are
# tuned by dated note). Kept here so the display helpers and fixtures agree on one source.
BUS_FRESH_SECS = 15
MIRROR_INTERVAL_SECS = 30
MIRROR_FRESH_SECS = 2 * MIRROR_INTERVAL_SECS
P6_POLL_SECS = 30
P6_FRESH_SECS = 2 * P6_POLL_SECS
PROJECTOR_STALE_SECS = 60


class PanelState(StrEnum):
    """The five panel states every panel declares (ux §5.2)."""

    LIVE = "live"
    LAGGING = "lagging"
    TRUE_EMPTY = "empty"
    FEED_PENDING = "pending"
    UNMEASURED = "unmeasured"  # per-signal; a panel is never wholly UNMEASURED


# ---------------------------------------------------------------------------
# Shared honesty-surface components (ux §5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AsOf:
    """The as-of chip (§5.1). Never omitted; a panel without one is a build defect.

    On a FEED-PENDING panel `label` is the inert marker "— feed pending" (no timestamp — a dark
    feed has no recency to claim) and asof_epoch is None. Otherwise `label` is wall-clock text and
    `title_iso` carries the full ISO-8601 for the title attribute. `asof_epoch`/`threshold_secs`
    are read client-side by static/freshness.js to drift the dot colour green->amber->red on the
    wall clock without a refetch — display-time comparison only (§1.3 holds; the timestamp and
    threshold still arrive from the query layer).
    """

    prefix: str          # "as of" | "events" | "mirror" | "checked" — matches the wireframes
    label: str           # formatted wall time, or "— feed pending"
    title_iso: str       # full ISO-8601 for the title attr ("" when feed pending)
    dot_state: str       # 'live' | 'lagging' | 'stale' | 'pending' | 'empty'
    asof_epoch: float | None = None
    threshold_secs: int | None = None


@dataclass(frozen=True)
class BandChip:
    """Norm band chip (§5.5): colour + letter ALWAYS together — colour is never the only signal."""

    band: str            # 'G' | 'A' | 'R' | '—'
    label: str = ""      # optional trailing word, e.g. "GREEN", "BEHIND", "ON TARGET", "AHEAD"


@dataclass(frozen=True)
class JourneyStation:
    """One station on the Q1 journey bar (§5.3)."""

    name: str            # verbatim design §1 station name — no renames
    glyph_state: str     # 'passed' | 'current' | 'future' | 'pending'


# Design §1 Q1 / §5.3 — the 8 stations, verbatim. Last two are FEED-PENDING in v1.
JOURNEY_STATIONS: tuple[str, ...] = (
    "intake",
    "planning",
    "approval",
    "spec/plan",
    "build",
    "merged+PR",
    "deployed",
    "live-verified",
)


@dataclass(frozen=True)
class JourneyBar:
    """The Q1 journey bar (§5.3): all 8 stations, never a truncated subset."""

    stations: tuple[JourneyStation, ...]
    current_label: str            # the current station's name (or "delivered")
    progress_line: str = ""       # e.g. "11/11 tasks · 6/6 waves · 74m55s" or "wave 4/6 · 64%"


@dataclass(frozen=True)
class ProvenanceBadge:
    """src: bus | forge_sqlite | both (§5.4)."""

    source: str          # 'bus' | 'forge_sqlite' | 'both'

    @property
    def short(self) -> str:
        return {"bus": "⌗bus", "forge_sqlite": "⌗sq", "both": "⌗both"}.get(
            self.source, self.source
        )


@dataclass(frozen=True)
class SpendSlot:
    """The FEED-PENDING spend/cost slot (§7.3), present from D0; D5 populates it.

    Always FEED-PENDING in v1: hatched, names its gate, carries captured-share. NEVER a numeric 0.
    On operator surfaces it cites ask ids; on client surfaces the plain-language variant is used.
    """

    gate_label: str = "cost capture lands with asks A-4 / IN-5"
    captured_share: str = "0%"
    plain_language: bool = False


# ---------------------------------------------------------------------------
# Panel container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Panel:
    """Common panel envelope every fragment shares. Panel-specific rows hang off the subclasses
    below via the `rows`/typed fields. Keeping the envelope uniform lets the SSE refetch and the
    state machinery treat panels alike."""

    panel_id: str        # 'p1'..'p7'
    title: str
    state: PanelState
    as_of: AsOf


# --- P1 agents roster -------------------------------------------------------


@dataclass(frozen=True)
class AgentRow:
    name: str
    status_dot: str          # 'ok' | 'idle' | 'unknown'
    status_text: str         # 'active' | 'idle' | 'registered'
    liveness: str            # e.g. "q:2", "kv 12s", "registered 13:02 — no heartbeat feed"
    source_kind: str         # 'fleet' | 'kv' | 'register_only'
    queue_depth: str = ""    # "" when unmeasured
    unmeasured_note: str = ""  # named gap for kv/register-only columns


@dataclass(frozen=True)
class AgentsPanel(Panel):
    agents: tuple[AgentRow, ...] = ()
    empty_copy: str = "No agents registered."


# --- P2 build board ---------------------------------------------------------


@dataclass(frozen=True)
class BuildRow:
    title: str               # "" -> template falls back to the id (§5.8 honest fallback)
    feature_id: str
    feature_link: str
    project: str
    provenance: ProvenanceBadge
    journey: JourneyBar
    band: BandChip
    current_station_label: str
    detail: str              # e.g. "started 12:58 · 38m" or "waiting on approval 22m"
    terminal: bool = False   # terminal builds (last 24h) render dimmed


@dataclass(frozen=True)
class BuildBoardPanel(Panel):
    rows: tuple[BuildRow, ...] = ()
    empty_copy: str = "No builds in flight."
    title_gap_note: str = ""  # "N features have no projected title — title feed pending" (§5.8)


# --- P3 gate events ---------------------------------------------------------


@dataclass(frozen=True)
class GateEventRow:
    time_label: str
    time_title: str
    feature_id: str
    feature_link: str
    stage_label: str
    status: str              # PASSED | GATED | REJECTED | FAILED
    gate_mode: str           # auto | coach
    coach_score: str         # operator-view ONLY (DF-008) — never in a client template
    duration: str
    origin: ProvenanceBadge


@dataclass(frozen=True)
class GateEventsPanel(Panel):
    rows: tuple[GateEventRow, ...] = ()
    empty_copy: str = "No gate events recorded."


# --- P4 needs-you strip -----------------------------------------------------


@dataclass(frozen=True)
class NeedsYouItem:
    kind: str                # approval_waiting | escalation | gate_rejected
    kind_glyph: str          # ⏳ | ✖ | ⚠
    age_label: str
    age_title: str
    band: BandChip
    headline: str            # "approval WAITING 2h14m — forge agent · '…'"
    scope_link: str          # e.g. "/issues" or "/features/FEAT-9A21"
    meta: str = ""           # "risk:high · expected: rich" etc.
    observes_only: bool = True


@dataclass(frozen=True)
class NeedsYouPanel(Panel):
    items: tuple[NeedsYouItem, ...] = ()
    count: int = 0
    # ux §4.1: the true-empty copy enumerates exactly the kinds this strip watches.
    empty_copy: str = (
        "Nothing needs you — 0 approvals · 0 escalations · 0 gate rejections."
    )


# --- P5 planning runs -------------------------------------------------------


@dataclass(frozen=True)
class PlanningRunRow:
    plan_id: str
    project: str
    state: str               # PLANNED_HANDOFF etc.
    originating_user: str
    age: str
    deferred_count: int


@dataclass(frozen=True)
class PlanningPanel(Panel):
    rows: tuple[PlanningRunRow, ...] = ()
    empty_copy: str = (
        "None recorded — planning front half is one operator session from live."
    )


# --- P6 serving health ------------------------------------------------------


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


# --- Projector liveness (§5.6) ---------------------------------------------


@dataclass(frozen=True)
class StreamRecency:
    name: str
    label: str               # "ev 14:13:31" | "no events since 13:40"
    dot_state: str           # 'live' | 'lagging' | 'quiet'


@dataclass(frozen=True)
class ProjectorPanel(Panel):
    heartbeat_state: str = "live"     # 'live' | 'stale'
    heartbeat_label: str = ""         # "live 4s" | "STALE since 14:02:11"
    streams: tuple[StreamRecency, ...] = ()
    forge_mirror_label: str = ""


# --- P7 delivered ledger ----------------------------------------------------


@dataclass(frozen=True)
class DeliveredRow:
    delivered_date: str
    title: str
    feature_id: str
    feature_link: str
    project: str
    bar_label: str           # "Delivered — merged ⟨change⟩" | "complete — merge unverified"
    change_link: str         # merge-commit / PR receipt url, or "" when unverified
    change_verified: bool
    provenance: ProvenanceBadge
    spend: SpendSlot


@dataclass(frozen=True)
class DeliveredPanel(Panel):
    rows: tuple[DeliveredRow, ...] = ()
    delivered_count: int = 0
    upgrades_pending: bool = True     # upgrades slot is FEED-PENDING (never numeric 0) until B7/B8
    upgrades_gate: str = "WS2 B7/B8 — becomes the separate count then"
    window_from: str = ""
    window_to: str = ""
    parity_footer: str = "M-D2 parity: not yet run"
    title_gap_note: str = ""          # "N features have no projected title — title feed pending"
    empty_copy: str = "No deliveries in window."


# ---------------------------------------------------------------------------
# Feature page (§4.2) — the feature_status twin
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalRow:
    metric_label: str
    value: str               # "1" | "0" | "—"
    norm: str                # "1 / 2–3 / >3"
    band: BandChip
    measured: bool
    reason: str = ""         # named feed gap when unmeasured


@dataclass(frozen=True)
class FeaturePage:
    title: str
    feature_id: str
    project: str
    created: str
    as_of_events: AsOf
    as_of_mirror: AsOf
    title_missing: bool
    # Q1
    journey: JourneyBar
    q1_as_of: AsOf
    q1_source: str
    delivered_line: str
    feed_pending_note: str
    # Q2
    q2_as_of: AsOf
    verdict: BandChip
    verdict_qualifier: str   # "on the measured signals"
    signals: tuple[SignalRow, ...]
    context_counters: str
    q2_coverage_gaps: tuple[str, ...]
    # Q3
    q3_as_of: AsOf
    issue_rows: tuple[str, ...]
    q3_empty_copy: str
    q3_coverage_gaps: tuple[str, ...]
    # stage history + spend
    stage_history: tuple[GateEventRow, ...]
    spend: SpendSlot


# ---------------------------------------------------------------------------
# Weekly delivery report (§4.7 / S4) — weekly_report(tenant, window) rendered
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MilestoneRow:
    """One plan-vs-actual row (§4.7). The FOUR client-safe fields are milestone_id, title,
    target_window, and the deviation band; `detail` is operator-only (feature/merge counts) and
    NEVER renders in the client-safe export. `no_baseline` marks work outside every milestone —
    a named gap `[—] NO BASELINE`, never fake-on-target."""

    milestone_id: str
    title: str
    target_window: str
    band: BandChip           # [G] AHEAD / [G] ON TARGET / [R] BEHIND / [—] NO BASELINE
    deviation: str           # 'ahead' | 'on_target' | 'behind' | 'no_baseline'
    detail: str = ""         # operator-only: "target w/c 07-06 · 1/3 features merged"
    feature_id: str = ""     # for NO BASELINE rows (the untracked feature); "" for milestones
    feature_link: str = ""


@dataclass(frozen=True)
class ReportHeadline:
    """The one-line headline (§4.7). Under a stale ledger/build watermark the counts are WITHHELD
    (never dimmed-green): `withheld` renders the "plan status unavailable — projection lagging"
    copy in place of the counts (§5.6/F-5 discipline applied to plan data)."""

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
    """The plan-vs-actual card (§4.7). `withheld` withholds ALL deviation chips under a stale
    projection; the milestone rows still list (id/title/target) but no green/red claim is made."""

    milestones: tuple[MilestoneRow, ...]
    withheld: bool = False
    withheld_since: str = ""


@dataclass(frozen=True)
class ReportIssueRow:
    kind: str
    band: BandChip
    headline: str            # "gate REJECTED Payments API webhooks ⟨FEAT-9A21⟩ · build-3"
    feature_id: str
    scope_link: str
    is_open: bool


@dataclass(frozen=True)
class IssuesEncountered:
    """Issues opened OR closed in the window, scoped to the tenant's projects (§4.7). Leads with
    counts; lists open reds first (§4.5 bands)."""

    opened: int
    closed: int
    oldest_open_age: str     # "2d" | "" when nothing open
    rows: tuple[ReportIssueRow, ...] = ()


@dataclass(frozen=True)
class WeeklyReport:
    """The internal weekly-report view model (§4.7). One query-layer composition; the client-safe
    export (`ExportReport`) is a strict subset of this, rendered by a separate template."""

    chrome: PageChrome
    tenant_slug: str
    tenant_display: str
    window_from: str
    window_to: str
    headline: ReportHeadline
    plan: PlanVsActual
    delivered_rows: tuple[DeliveredRow, ...]
    in_flight_rows: tuple[BuildRow, ...]
    issues: IssuesEncountered
    client_tenants: tuple[tuple[str, str], ...] = ()  # (slug, display) picker for the operator


# --- the client-safe export (§4.7 dated note) — DF-008 firewall, MECHANIZED --
# ONLY DF-008-permitted fields (feature id/title/project/bar/date/change link + per-build spend
# totals) PLUS the four sanctioned plan-milestone fields (id/title/target window/deviation state).
# No issues, no in-flight internals, no coach scores, no agent/norm/planning internals. The
# export template renders EXCLUSIVELY these shapes (coach greps it + positive-asserts the four).


@dataclass(frozen=True)
class ExportDeliveredRow:
    feature_id: str
    title: str
    project: str
    bar_label: str
    change_link: str
    change_verified: bool
    delivered_date: str
    spend: SpendSlot


@dataclass(frozen=True)
class ExportMilestone:
    """The FOUR sanctioned client-safe milestone fields ONLY (§4.7 dated note)."""

    milestone_id: str
    title: str
    target_window: str
    band: BandChip           # the deviation-state, the fourth sanctioned field


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


# ---------------------------------------------------------------------------
# Page chrome (top bar + banner) — every operator page
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Banner:
    kind: str                # 'projector_stalled' | 'sse_paused'
    text: str


@dataclass(frozen=True)
class PageChrome:
    projector_state: str     # 'live' | 'stale'
    projector_label: str     # "projector ● live"
    events_as_of: AsOf
    banner: Banner | None = None


# ---------------------------------------------------------------------------
# Display formatting helpers — the ONE place time/age is formatted (never in Jinja).
# All wall time is Europe/London; the title attr carries full ISO-8601 (ux §2).
# ---------------------------------------------------------------------------


def to_london(dt: datetime) -> datetime:
    return dt.astimezone(LONDON)


def fmt_wall(dt: datetime, *, now: datetime) -> str:
    """HH:MM:SS for today, MM-DD HH:MM otherwise (ux §2)."""
    ldt = to_london(dt)
    lnow = to_london(now)
    if ldt.date() == lnow.date():
        return ldt.strftime("%H:%M:%S")
    return ldt.strftime("%m-%d %H:%M")


def fmt_iso(dt: datetime) -> str:
    return to_london(dt).isoformat()


def fmt_age(seconds: float) -> str:
    """Humanised age, e.g. '2h 14m', '38m', '4s' (ux §2)."""
    secs = int(max(0, seconds))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, s = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins:02d}m"
    if mins:
        return f"{mins}m"
    return f"{s}s"


def dot_state_for(age_secs: float, threshold_secs: int) -> str:
    """Green while fresh, amber past 1x, red past 2x the freshness threshold."""
    if age_secs < threshold_secs:
        return "live"
    if age_secs < 2 * threshold_secs:
        return "lagging"
    return "stale"


def make_asof(
    dt: datetime,
    *,
    now: datetime,
    prefix: str,
    threshold_secs: int,
) -> AsOf:
    """Build a live/lagging as-of chip from a data-recency timestamp."""
    age = (now - dt).total_seconds()
    return AsOf(
        prefix=prefix,
        label=fmt_wall(dt, now=now),
        title_iso=fmt_iso(dt),
        dot_state=dot_state_for(age, threshold_secs),
        asof_epoch=dt.timestamp(),
        threshold_secs=threshold_secs,
    )


def feed_pending_asof(prefix: str = "as of") -> AsOf:
    """A dark feed has no recency to claim — the inert marker, no timestamp (§5.1)."""
    return AsOf(
        prefix=prefix,
        label="— feed pending",
        title_iso="",
        dot_state="pending",
        asof_epoch=None,
        threshold_secs=None,
    )


def empty_asof(dt: datetime, *, now: datetime, prefix: str = "as of") -> AsOf:
    """TRUE-EMPTY: evidence is fresh (green dot) but zero rows."""
    return AsOf(
        prefix=prefix,
        label=fmt_wall(dt, now=now),
        title_iso=fmt_iso(dt),
        dot_state="empty",
        asof_epoch=dt.timestamp(),
        threshold_secs=BUS_FRESH_SECS,
    )


def journey_from_states(states: dict[str, str], current_label: str, progress: str = "") -> JourneyBar:
    """Build an 8-station journey bar from a {station_name: glyph_state} map.

    Any station absent from the map defaults to 'future'; the last two default to 'pending'
    (FEED-PENDING in v1, §5.3) unless explicitly overridden.
    """
    stations: list[JourneyStation] = []
    for i, name in enumerate(JOURNEY_STATIONS):
        default = "pending" if i >= 6 else "future"
        stations.append(JourneyStation(name=name, glyph_state=states.get(name, default)))
    return JourneyBar(stations=tuple(stations), current_label=current_label, progress_line=progress)
