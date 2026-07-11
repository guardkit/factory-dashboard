"""THE query layer — the single seam the panels AND the (later D4) chat tools both consume.

ux §1.3 / ADR-DASH-005 rule 1: templates render, THIS computes. Every number, band, state, verdict,
lag figure, and formatted time is produced here (or in viewmodels' display helpers), never in Jinja.
Panels and chat read the same functions, so they can never disagree.

**S1 status:** there is no read DB and no projector yet, so every function below is backed by the
ux §4.8 fixtures (backend/readmodel/fixtures.py). The PUBLIC SIGNATURES are the stable contract; S2
replaces the bodies with real `readmodel.db` reads (opened URI mode=ro — design §7 / M-D4) without
touching callers. Freshness thresholds are the module-level constants imported from viewmodels.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from backend.readmodel import fixtures
from backend.readmodel.viewmodels import (
    AgentsPanel,
    BandChip,
    BuildBoardPanel,
    DeliveredPanel,
    FeaturePage,
    GateEventsPanel,
    NeedsYouPanel,
    PageChrome,
    Panel,
    PanelState,
    PlanningPanel,
    ProjectorPanel,
    ServingPanel,
    SpendSlot,
)

# --- panel-state resolution -------------------------------------------------

_STATE_ALIASES: dict[str, PanelState] = {
    "live": PanelState.LIVE,
    "lagging": PanelState.LAGGING,
    "empty": PanelState.TRUE_EMPTY,
    "pending": PanelState.FEED_PENDING,
    "unmeasured": PanelState.UNMEASURED,
}

# One builder per matrix row (plus the projector liveness sub-panel).
_PANEL_BUILDERS: dict[str, Callable[[PanelState], Panel]] = {
    "p1": fixtures.agents,
    "p2": fixtures.build_board,
    "p3": fixtures.gate_events,
    "p4": fixtures.needs_you,
    "p5": fixtures.planning,
    "p6": fixtures.serving,
    "p7": fixtures.delivered,
    "proj": fixtures.projector,
}

# Which of the five §5.2 states each panel can render (acceptance item 2). UNMEASURED is per-signal
# and is exercised WITHIN live renders (P1 kv/register rows; P2 wave-only progress; feature Q2).
APPLICABLE_STATES: dict[str, tuple[PanelState, ...]] = {
    "p1": (PanelState.LIVE, PanelState.LAGGING, PanelState.TRUE_EMPTY),
    "p2": (PanelState.LIVE, PanelState.LAGGING, PanelState.TRUE_EMPTY, PanelState.FEED_PENDING),
    "p3": (PanelState.LIVE, PanelState.LAGGING, PanelState.TRUE_EMPTY),
    "p4": (PanelState.LIVE, PanelState.LAGGING, PanelState.TRUE_EMPTY),
    "p5": (PanelState.LIVE, PanelState.LAGGING, PanelState.TRUE_EMPTY),
    "p6": (PanelState.LIVE, PanelState.LAGGING),
    "p7": (PanelState.LIVE, PanelState.LAGGING, PanelState.TRUE_EMPTY, PanelState.FEED_PENDING),
}

ALL_PANELS: tuple[str, ...] = ("p1", "p2", "p3", "p4", "p5", "p6", "p7")


class UnknownPanel(Exception):
    """Raised for an unknown panel id (the route maps this to 404)."""


def parse_state(raw: str | None) -> PanelState:
    if raw is None:
        return PanelState.LIVE
    if raw == "hostile":  # dev/test-only: exercises the autoescape acceptance (fence 6)
        return PanelState.LIVE
    try:
        return _STATE_ALIASES[raw]
    except KeyError as exc:
        raise UnknownPanel(f"unknown state: {raw}") from exc


def panel_view(panel_id: str, raw_state: str | None = None) -> Panel:
    """Return a single panel's view model in the requested state. Backs `/fragments/{panel}`."""
    if raw_state == "hostile" and panel_id == "p2":
        return fixtures.hostile_board()
    if panel_id not in _PANEL_BUILDERS:
        raise UnknownPanel(panel_id)
    return _PANEL_BUILDERS[panel_id](parse_state(raw_state))


# --- page chrome ------------------------------------------------------------


def chrome(raw_state: str | None = None) -> PageChrome:
    return fixtures.page_chrome(parse_state(raw_state))


# --- home -------------------------------------------------------------------


@dataclass(frozen=True)
class HomeView:
    chrome: PageChrome
    needs_you: NeedsYouPanel
    build_board: BuildBoardPanel
    agents: AgentsPanel
    serving: ServingPanel
    projector: ProjectorPanel
    gate_events: GateEventsPanel
    planning: PlanningPanel


def home_view() -> HomeView:
    return HomeView(
        chrome=fixtures.page_chrome(),
        needs_you=fixtures.needs_you(PanelState.LIVE),
        build_board=fixtures.build_board(PanelState.LIVE),
        agents=fixtures.agents(PanelState.LIVE),
        serving=fixtures.serving(PanelState.LIVE),
        projector=fixtures.projector(PanelState.LIVE),
        gate_events=fixtures.gate_events(PanelState.LIVE),
        planning=fixtures.planning(PanelState.LIVE),
    )


# --- features / feature page ------------------------------------------------


@dataclass(frozen=True)
class FeaturesView:
    chrome: PageChrome
    build_board: BuildBoardPanel


def features_view() -> FeaturesView:
    """`/features` — the build board expanded to all known features (same fragment as P2,
    unfiltered, plus terminal builds — ux §2). At S1 it renders the P2 fixture unfiltered."""
    return FeaturesView(chrome=fixtures.page_chrome(), build_board=fixtures.build_board(PanelState.LIVE))


# Which feature ids the S1 fixtures know how to render as a full feature page.
_KNOWN_FEATURES: dict[str, Callable[[], FeaturePage]] = {
    "FEAT-3ED2": fixtures.feature_page_feat_3ed2,
}


def feature_page(feature_id: str) -> FeaturePage | None:
    builder = _KNOWN_FEATURES.get(feature_id)
    return builder() if builder else None


# --- delivered --------------------------------------------------------------


@dataclass(frozen=True)
class DeliveredView:
    """`/delivered` composed view (ux §4.4): the page chrome + the P7 panel for a window. The
    LIVE realisation is `dbread.delivered_view`; this fixture path backs the `?state=` demo."""

    chrome: PageChrome
    panel: DeliveredPanel


def delivered_view(raw_state: str | None = None) -> DeliveredPanel:
    return fixtures.delivered(parse_state(raw_state))


# --- projects ---------------------------------------------------------------


@dataclass(frozen=True)
class ProjectView:
    chrome: PageChrome
    project: str
    build_board: BuildBoardPanel
    delivered: DeliveredPanel
    open_issue_count: int
    spend: SpendSlot


def project_view(project: str) -> ProjectView:
    """`/projects/{project}` — one card per question at fleet-of-features scale (ux §4.3),
    composed entirely of §5 components + the FEED-PENDING spend block."""
    return ProjectView(
        chrome=fixtures.page_chrome(),
        project=project,
        build_board=fixtures.build_board(PanelState.LIVE),
        delivered=fixtures.delivered(PanelState.LIVE),
        open_issue_count=0,
        spend=SpendSlot(),
    )


# --- issues -----------------------------------------------------------------


@dataclass(frozen=True)
class KindLegendEntry:
    kind: str
    band: BandChip
    feed_pending: bool = False
    gate: str = ""


@dataclass(frozen=True)
class IssueRow:
    kind: str
    band: BandChip
    scope_id: str
    scope_link: str
    age: str
    detail: str
    source_ref: str


@dataclass(frozen=True)
class IssuesView:
    chrome: PageChrome
    rows: tuple[IssueRow, ...]
    legend: tuple[KindLegendEntry, ...]


def issues_view() -> IssuesView:
    legend = (
        KindLegendEntry("escalation", BandChip("R")),
        KindLegendEntry("gate_rejected", BandChip("R")),
        KindLegendEntry("approval_waiting", BandChip("A")),
        KindLegendEntry("stalled", BandChip("R")),
        KindLegendEntry("build_failed", BandChip("R")),
        # No producer exists yet — FEED-PENDING in the legend, never a fake row (§4.5).
        KindLegendEntry(
            "seam_finding",
            BandChip("—"),
            feed_pending=True,
            gate="becomes machine-visible with WS2 F14 / WS3-S5 — ask A-7",
        ),
    )
    rows = (
        IssueRow(
            kind="gate_rejected",
            band=BandChip("R"),
            scope_id="FEAT-9A21",
            scope_link="/features/FEAT-9A21",
            age="41m",
            detail="Payments API webhooks · build-3 REJECTED",
            source_ref="stage_log",
        ),
        IssueRow(
            kind="approval_waiting",
            band=BandChip("A"),
            scope_id="forge",
            scope_link="/issues",
            age="2h 14m",
            detail='forge agent · "apply migration to prod schema" · risk:high · expected: rich',
            source_ref="approvals/req-…",
        ),
    )
    return IssuesView(chrome=fixtures.page_chrome(), rows=rows, legend=legend)


# --- fleet ------------------------------------------------------------------


@dataclass(frozen=True)
class FleetView:
    chrome: PageChrome
    agents: AgentsPanel
    serving: ServingPanel
    projector: ProjectorPanel


def fleet_view() -> FleetView:
    return FleetView(
        chrome=fixtures.page_chrome(),
        agents=fixtures.agents(PanelState.LIVE),
        serving=fixtures.serving(PanelState.LIVE),
        projector=fixtures.projector(PanelState.LIVE),
    )
