"""The ux §4.8 pinned fixtures, pre-computed as view models.

S1 (D0) is a scaffold: there is no read DB and no projector yet, so the query layer (queries.py)
is backed by THESE fixtures. Tests and coach checks render against them. S2 replaces the internals
of queries.py with real reads of readmodel.db; these fixtures then move to tests/ as the pinned
render targets. The view-model SHAPES are the stable seam — they do not change when the data source
does.

Every timestamp is anchored to a single pinned NOW = 2026-07-11 14:13:34 Europe/London (the
wireframe "now"), so renders are deterministic and freshness states are stable in tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from backend.readmodel import viewmodels as vm
from backend.readmodel.viewmodels import (
    AgentRow,
    AgentsPanel,
    AsOf,
    BandChip,
    Banner,
    BuildBoardPanel,
    BuildRow,
    DeliveredPanel,
    DeliveredRow,
    FeaturePage,
    GateEventRow,
    GateEventsPanel,
    NeedsYouItem,
    NeedsYouPanel,
    PageChrome,
    PanelState,
    PlanningPanel,
    PlanningRunRow,
    ProjectorPanel,
    ProvenanceBadge,
    ServiceRow,
    ServingPanel,
    SignalRow,
    SpendSlot,
    StreamRecency,
)

NOW: datetime = datetime(2026, 7, 11, 14, 13, 34, tzinfo=vm.LONDON)


def _ago(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


def _events(seconds_ago: float, prefix: str = "events") -> AsOf:
    return vm.make_asof(_ago(seconds_ago), now=NOW, prefix=prefix, threshold_secs=vm.BUS_FRESH_SECS)


def _mirror(seconds_ago: float, prefix: str = "mirror") -> AsOf:
    return vm.make_asof(
        _ago(seconds_ago), now=NOW, prefix=prefix, threshold_secs=vm.MIRROR_FRESH_SECS
    )


def _checked(seconds_ago: float, prefix: str = "checked") -> AsOf:
    return vm.make_asof(_ago(seconds_ago), now=NOW, prefix=prefix, threshold_secs=vm.P6_FRESH_SECS)


def _lagging_asof(prefix: str, seconds_ago: float) -> AsOf:
    """Force a stale (lagging) as-of chip regardless of the pinned NOW."""
    dt = _ago(seconds_ago)
    return AsOf(
        prefix=prefix,
        label=vm.fmt_wall(dt, now=NOW),
        title_iso=vm.fmt_iso(dt),
        dot_state="lagging",
        asof_epoch=dt.timestamp(),
        threshold_secs=vm.BUS_FRESH_SECS,
    )


BUS = ProvenanceBadge("bus")
SQ = ProvenanceBadge("forge_sqlite")


# ---------------------------------------------------------------------------
# P4 — needs-you strip (ordered RED first — acceptance item 8)
# ---------------------------------------------------------------------------


def _needs_you_items() -> tuple[NeedsYouItem, ...]:
    gate = NeedsYouItem(
        kind="gate_rejected",
        kind_glyph="✖",
        age_label="41m",
        age_title=vm.fmt_iso(_ago(41 * 60)),
        band=BandChip("R"),
        headline='gate REJECTED — Payments API webhooks ⟨FEAT-9A21⟩ · build-3 · 0.42',
        scope_link="/features/FEAT-9A21",
        meta="",
        observes_only=False,
    )
    approval = NeedsYouItem(
        kind="approval_waiting",
        kind_glyph="⏳",
        age_label="2h 14m",
        age_title=vm.fmt_iso(_ago(2 * 3600 + 14 * 60)),
        band=BandChip("A"),
        headline='approval WAITING — forge agent · "apply migration to prod schema"',
        scope_link="/issues",
        meta="risk:high · expected: rich",
        observes_only=True,
    )
    return (gate, approval)  # red band first, then amber


def needs_you(state: PanelState) -> NeedsYouPanel:
    base = dict(panel_id="p4", title="Needs you")
    if state is PanelState.TRUE_EMPTY:
        return NeedsYouPanel(
            **base, state=state, as_of=vm.empty_asof(_ago(2), now=NOW), items=(), count=0
        )
    if state is PanelState.LAGGING:
        return NeedsYouPanel(
            **base,
            state=state,
            as_of=_lagging_asof("as of", 95),
            items=_needs_you_items(),
            count=2,
        )
    return NeedsYouPanel(
        **base, state=state, as_of=_events(2, "as of"), items=_needs_you_items(), count=2
    )


# ---------------------------------------------------------------------------
# P2 — build board
# ---------------------------------------------------------------------------


def _build_rows() -> tuple[BuildRow, ...]:
    voice = BuildRow(
        title="Voice session summaries",
        feature_id="FEAT-51B0",
        feature_link="/features/FEAT-51B0",
        project="study-tutor",
        provenance=BUS,
        journey=vm.journey_from_states(
            {
                "intake": "passed",
                "planning": "passed",
                "approval": "passed",
                "spec/plan": "passed",
                "build": "current",
            },
            current_label="build",
            progress="wave 4/6 · 64%",
        ),
        band=BandChip("G"),
        current_station_label="build",
        detail="started 12:58 · 38m",
    )
    lpa = BuildRow(
        title="LPA document intake",
        feature_id="FEAT-77AD",
        feature_link="/features/FEAT-77AD",
        project="lpa-poc",
        provenance=BUS,
        journey=vm.journey_from_states(
            {
                "intake": "passed",
                "planning": "passed",
                "approval": "current",
            },
            current_label="approval",
        ),
        band=BandChip("A"),
        current_station_label="approval",
        detail="waiting on approval 22m",
    )
    # Title-LESS feature — §5.8 honest id-fallback; the gap is named in the footer note.
    titleless = BuildRow(
        title="",
        feature_id="FEAT-B17E",
        feature_link="/features/FEAT-B17E",
        project="lpa-poc",
        provenance=BUS,
        journey=vm.journey_from_states(
            {"intake": "passed", "planning": "current"}, current_label="planning"
        ),
        band=BandChip("G"),
        current_station_label="planning",
        detail="started 13:55 · 18m",
    )
    return (voice, lpa, titleless)


def build_board(state: PanelState) -> BuildBoardPanel:
    base = dict(panel_id="p2", title="Work in flight")
    note = "1 feature has no projected title — title feed pending"
    if state is PanelState.TRUE_EMPTY:
        return BuildBoardPanel(
            **base, state=state, as_of=vm.empty_asof(_ago(2), now=NOW), rows=()
        )
    if state is PanelState.LAGGING:
        return BuildBoardPanel(
            **base,
            state=state,
            as_of=_lagging_asof("as of", 95),
            rows=_build_rows(),
            title_gap_note=note,
        )
    if state is PanelState.FEED_PENDING:
        # A hostile-strings row rides the FEED... no — hostile is its own fixture. Here the
        # feed-pending demonstration is a board whose producer has not landed for a lane.
        return BuildBoardPanel(
            **base,
            state=state,
            as_of=vm.feed_pending_asof(),
            rows=(),
            empty_copy="build-lifecycle feed pending",
        )
    return BuildBoardPanel(
        **base, state=state, as_of=_events(2, "as of"), rows=_build_rows(), title_gap_note=note
    )


def hostile_board() -> BuildBoardPanel:
    """The `hostile_strings` fixture (§4.8): event-derived strings carrying <script>, onerror=,
    hx-get=. Jinja autoescape must render every one inert (handoff §5 / fence 6)."""
    row = BuildRow(
        title="<script>alert('xss')</script>",
        feature_id="FEAT-EVIL",
        feature_link="/features/FEAT-EVIL",
        project='"><img src=x onerror=alert(1)>',
        provenance=BUS,
        journey=vm.journey_from_states(
            {"intake": "passed", "build": "current"}, current_label="build"
        ),
        band=BandChip("G"),
        current_station_label="build",
        detail='evidence: <a hx-get="/steal" hx-trigger="load">x</a> onerror=alert(2)',
    )
    return BuildBoardPanel(
        panel_id="p2",
        title="Work in flight",
        state=PanelState.LIVE,
        as_of=_events(2, "as of"),
        rows=(row,),
    )


# ---------------------------------------------------------------------------
# P3 — recent gate events (dense table: id-first is permitted, §5.8)
# ---------------------------------------------------------------------------


def _gate_rows() -> tuple[GateEventRow, ...]:
    return (
        GateEventRow(
            time_label="14:02",
            time_title=vm.fmt_iso(_ago(11 * 60)),
            feature_id="FEAT-3ED2",
            feature_link="/features/FEAT-3ED2",
            stage_label="build-2",
            status="PASSED",
            gate_mode="auto",
            coach_score="0.87",
            duration="12m",
            origin=BUS,
        ),
        GateEventRow(
            time_label="13:44",
            time_title=vm.fmt_iso(_ago(29 * 60)),
            feature_id="FEAT-3ED2",
            feature_link="/features/FEAT-3ED2",
            stage_label="build-1",
            status="GATED",
            gate_mode="coach",
            coach_score="0.71",
            duration="18m",
            origin=BUS,
        ),
        GateEventRow(
            time_label="13:12",
            time_title=vm.fmt_iso(_ago(61 * 60)),
            feature_id="FEAT-9A21",
            feature_link="/features/FEAT-9A21",
            stage_label="build-3",
            status="REJECTED",
            gate_mode="coach",
            coach_score="0.42",
            duration="",
            origin=SQ,
        ),
    )


def gate_events(state: PanelState) -> GateEventsPanel:
    base = dict(panel_id="p3", title="Recent gate events")
    if state is PanelState.TRUE_EMPTY:
        return GateEventsPanel(**base, state=state, as_of=vm.empty_asof(_ago(2), now=NOW), rows=())
    if state is PanelState.LAGGING:
        return GateEventsPanel(
            **base, state=state, as_of=_lagging_asof("as of", 95), rows=_gate_rows()
        )
    return GateEventsPanel(**base, state=state, as_of=_events(2, "as of"), rows=_gate_rows())


# ---------------------------------------------------------------------------
# P1 — agents roster (dual-source; kv/register-only render honest named gaps)
# ---------------------------------------------------------------------------


def _agent_rows() -> tuple[AgentRow, ...]:
    return (
        AgentRow(
            name="forge-orch",
            status_dot="ok",
            status_text="active",
            liveness="q:2",
            source_kind="fleet",
            queue_depth="2",
        ),
        AgentRow(
            name="jarvis",
            status_dot="ok",
            status_text="kv",
            liveness="kv 12s",
            source_kind="kv",
            queue_depth="",
            unmeasured_note="queue/tasks unmeasured (KV re-put freshness)",
        ),
        AgentRow(
            name="forge",
            status_dot="unknown",
            status_text="registered",
            liveness="registered 13:02 — no heartbeat feed",
            source_kind="register_only",
            queue_depth="",
            unmeasured_note="no heartbeat feed (ask A-10)",
        ),
    )


def agents(state: PanelState) -> AgentsPanel:
    base = dict(panel_id="p1", title="Agents")
    if state is PanelState.TRUE_EMPTY:
        return AgentsPanel(**base, state=state, as_of=vm.empty_asof(_ago(3), now=NOW), agents=())
    if state is PanelState.LAGGING:
        return AgentsPanel(
            **base, state=state, as_of=_lagging_asof("as of", 95), agents=_agent_rows()
        )
    return AgentsPanel(**base, state=state, as_of=_events(3, "as of"), agents=_agent_rows())


# ---------------------------------------------------------------------------
# P6 — serving health (dot demotion to '?' unknown when checked_at goes stale, §5.2)
# ---------------------------------------------------------------------------


def _serving_rows_live() -> tuple[ServiceRow, ...]:
    return (
        ServiceRow(name="litellm", endpoint=":4000", dot="ok", detail="ok"),
        ServiceRow(
            name="llama-swap",
            endpoint=":9000",
            dot="ok",
            detail="ok",
            resident_models=("qwen3.6-35b",),
        ),
        ServiceRow(name="nats", endpoint=":8222", dot="ok", detail="ok"),
    )


def _serving_rows_demoted() -> tuple[ServiceRow, ...]:
    last_ok = vm.fmt_wall(_ago(140), now=NOW)
    return (
        ServiceRow(
            name="litellm", endpoint=":4000", dot="unknown", detail=f"unknown — last ok {last_ok}"
        ),
        ServiceRow(
            name="llama-swap",
            endpoint=":9000",
            dot="unknown",
            detail=f"unknown — last ok {last_ok}",
            resident_models=("qwen3.6-35b",),
        ),
        ServiceRow(
            name="nats", endpoint=":8222", dot="unknown", detail=f"unknown — last ok {last_ok}"
        ),
    )


def serving(state: PanelState) -> ServingPanel:
    base = dict(panel_id="p6", title="Serving")
    if state is PanelState.LAGGING:
        return ServingPanel(
            **base, state=state, as_of=_lagging_asof("checked", 150), services=_serving_rows_demoted()
        )
    return ServingPanel(**base, state=state, as_of=_checked(8), services=_serving_rows_live())


# ---------------------------------------------------------------------------
# Projector liveness (§5.6) — the page-level honesty anchor
# ---------------------------------------------------------------------------


def _streams_live() -> tuple[StreamRecency, ...]:
    return (
        StreamRecency(name="PIPELINE", label=f"ev {vm.fmt_wall(_ago(3), now=NOW)}", dot_state="live"),
        StreamRecency(name="AGENTS", label=f"ev {vm.fmt_wall(_ago(14), now=NOW)}", dot_state="live"),
        StreamRecency(name="FLEET", label=f"ev {vm.fmt_wall(_ago(4), now=NOW)}", dot_state="live"),
    )


def projector(state: PanelState) -> ProjectorPanel:
    base = dict(panel_id="proj", title="Projector")
    if state is PanelState.LAGGING:
        return ProjectorPanel(
            **base,
            state=state,
            as_of=_lagging_asof("heartbeat", 95),
            heartbeat_state="stale",
            heartbeat_label="STALE since 14:02:11",
            streams=_streams_live(),
            forge_mirror_label=f"forge mirror {vm.fmt_wall(_ago(36), now=NOW)}",
        )
    return ProjectorPanel(
        **base,
        state=state,
        as_of=_events(4, "heartbeat"),
        heartbeat_state="live",
        heartbeat_label="live 4s",
        streams=_streams_live(),
        forge_mirror_label=f"forge mirror {vm.fmt_wall(_ago(36), now=NOW)}",
    )


# ---------------------------------------------------------------------------
# P5 — planning runs (TRUE-EMPTY asserted ONLY off a fresh mirror; stale -> LAGGING)
# ---------------------------------------------------------------------------


def _planning_rows() -> tuple[PlanningRunRow, ...]:
    return (
        PlanningRunRow(
            plan_id="plan-8c41",
            project="lpa-poc",
            state="PLANNED_HANDOFF",
            originating_user="rich",
            age="1h 02m",
            deferred_count=1,
        ),
    )


def planning(state: PanelState) -> PlanningPanel:
    base = dict(panel_id="p5", title="Planning runs")
    if state is PanelState.TRUE_EMPTY:
        # fresh mirror pass + zero rows -> genuinely no runs
        return PlanningPanel(**base, state=state, as_of=_mirror(36), rows=())
    if state is PanelState.LAGGING:
        # stale/never-run mirror -> LAGGING, never TRUE-EMPTY (§4.1)
        return PlanningPanel(
            **base, state=state, as_of=_lagging_asof("mirror", 300), rows=_planning_rows()
        )
    return PlanningPanel(**base, state=state, as_of=_mirror(36), rows=_planning_rows())


# ---------------------------------------------------------------------------
# P7 — delivered ledger (§4.4)
# ---------------------------------------------------------------------------


def _delivered_rows() -> tuple[DeliveredRow, ...]:
    return (
        DeliveredRow(
            delivered_date="2026-07-06",
            title="Study tutor planning chain",
            feature_id="FEAT-3ED2",
            feature_link="/features/FEAT-3ED2",
            project="study-tutor",
            bar_label="Delivered — merged ⟨change⟩",
            change_link="/change/study-tutor/34b17d0",
            change_verified=True,
            provenance=SQ,
            spend=SpendSlot(),
        ),
        DeliveredRow(
            delivered_date="2026-07-06",
            title="Planning wiring fix",
            feature_id="FEAT-DD4F",
            feature_link="/features/FEAT-DD4F",
            project="study-tutor",
            bar_label="Delivered — merged ⟨change⟩",
            change_link="/change/study-tutor/1ad98c0",
            change_verified=True,
            provenance=SQ,
            spend=SpendSlot(),
        ),
        # A COMPLETE build with NEITHER receipt does not clear the bar — named gap (§4.4).
        DeliveredRow(
            delivered_date="2026-07-07",
            title="Report export polish",
            feature_id="FEAT-9E59",
            feature_link="/features/FEAT-9E59",
            project="study-tutor",
            bar_label="complete — merge unverified",
            change_link="",
            change_verified=False,
            provenance=SQ,
            spend=SpendSlot(),
        ),
    )


def delivered(state: PanelState) -> DeliveredPanel:
    win_from, win_to = "2026-07-01", "2026-07-08"
    if state is PanelState.TRUE_EMPTY:
        return DeliveredPanel(
            panel_id="p7", title="Delivered", state=state, as_of=vm.empty_asof(_ago(2), now=NOW),
            rows=(), delivered_count=0, window_from=win_from, window_to=win_to,
        )
    if state is PanelState.LAGGING:
        return DeliveredPanel(
            panel_id="p7", title="Delivered", state=state, as_of=_lagging_asof("as of", 95),
            rows=_delivered_rows(), delivered_count=2, window_from=win_from, window_to=win_to,
        )
    if state is PanelState.FEED_PENDING:
        # The upgrades + spend slots are the dark feeds; the whole panel in FEED-PENDING demo mode.
        return DeliveredPanel(
            panel_id="p7", title="Delivered", state=state, as_of=vm.feed_pending_asof(),
            rows=(), delivered_count=0,
            empty_copy="delivery feed pending — fills as forge-daemon builds complete (WS2-V1)",
            window_from=win_from, window_to=win_to,
        )
    return DeliveredPanel(
        panel_id="p7", title="Delivered", state=state, as_of=_events(2, "as of"),
        rows=_delivered_rows(), delivered_count=2, window_from=win_from, window_to=win_to,
    )


# ---------------------------------------------------------------------------
# Feature page — the FEAT-3ED2 fixture (design §10 verbatim; §4.2 wireframe exactly)
# ---------------------------------------------------------------------------


def feature_page_feat_3ed2() -> FeaturePage:
    journey = vm.journey_from_states(
        {
            "intake": "passed",
            "planning": "passed",
            "approval": "passed",
            "spec/plan": "passed",
            "build": "passed",
            "merged+PR": "passed",
        },
        current_label="delivered",
        progress="11/11 tasks · 6/6 waves · 74m55s",
    )
    signals = (
        SignalRow(
            metric_label="runs (attempts)",
            value="1",
            norm="1 / 2–3 / >3",
            band=BandChip("G"),
            measured=True,
        ),
        SignalRow(
            metric_label="gate FAILED/GATED",
            value="0",
            norm="0–1 / 2–3 / >3",
            band=BandChip("G"),
            measured=True,
        ),
        SignalRow(
            metric_label="turns per task",
            value="—",
            norm="≤2 / 2–4 / >4",
            band=BandChip("—"),
            measured=False,
            reason="no live feed until the A-4 loop-stats ask lands",
        ),
        SignalRow(
            metric_label="SDK ceiling hits",
            value="—",
            norm="0 / — / ≥1",
            band=BandChip("—"),
            measured=False,
            reason="same A-4 gap",
        ),
    )
    stage_history = (
        GateEventRow(
            time_label="14:13",
            time_title=vm.fmt_iso(_ago(0)),
            feature_id="FEAT-3ED2",
            feature_link="/features/FEAT-3ED2",
            stage_label="build-6",
            status="PASSED",
            gate_mode="auto",
            coach_score="0.91",
            duration="",
            origin=SQ,
        ),
        GateEventRow(
            time_label="13:58",
            time_title=vm.fmt_iso(_ago(15 * 60)),
            feature_id="FEAT-3ED2",
            feature_link="/features/FEAT-3ED2",
            stage_label="build-5",
            status="PASSED",
            gate_mode="auto",
            coach_score="0.88",
            duration="",
            origin=SQ,
        ),
    )
    return FeaturePage(
        title="Study tutor spaced-repetition planning chain",
        feature_id="FEAT-3ED2",
        project="study-tutor",
        created="2026-07-06 11:29",
        as_of_events=_events(0, "events"),
        as_of_mirror=_mirror(36),
        title_missing=False,
        journey=journey,
        q1_as_of=_mirror(36),
        q1_source="forge_sqlite",
        delivered_line="✔ DELIVERED — merged ⟨change⟩ · 2026-07-06 14:13",
        feed_pending_note=(
            "deployed / live-verified: NOT INSTRUMENTED — deploy tracking lands with "
            "WS2 B7/B8 (asks A-1..A-3 filed)"
        ),
        q2_as_of=_events(0, "events"),
        verdict=BandChip("G", "GREEN"),
        verdict_qualifier="on the measured signals",
        signals=signals,
        context_counters="0/11 task failures · 74m55s wall",
        q2_coverage_gaps=(
            "re-work linkage is not machine-computable in v1 (a follow-up fix build, FEAT-DD4F, "
            "targeted this feature — lands with spec_survival episodes, WS4-S7/WS1-E)",
        ),
        q3_as_of=_events(0, "events"),
        issue_rows=(),
        q3_empty_copy="No open issues in the register for this feature.",
        q3_coverage_gaps=(
            "deferred operator items (e.g. runtime-validation follow-ups) are not machine-visible "
            "until ask A-5 / WS1-E lands",
            "no machine-readable merge-review verdicts exist for this period (ask A-7)",
        ),
        stage_history=stage_history,
        spend=SpendSlot(),
    )


# ---------------------------------------------------------------------------
# Page chrome + banner
# ---------------------------------------------------------------------------


def page_chrome(state: PanelState = PanelState.LIVE) -> PageChrome:
    if state is PanelState.LAGGING:
        return PageChrome(
            projector_state="stale",
            projector_label="projector ● STALE",
            events_as_of=_lagging_asof("events as-of", 95),
            banner=Banner(
                kind="projector_stalled",
                text="PROJECTION LAGGING since 14:02:11 — panels show last projected state",
            ),
        )
    return PageChrome(
        projector_state="live",
        projector_label="projector ● live",
        events_as_of=_events(0, "events as-of"),
        banner=None,
    )
