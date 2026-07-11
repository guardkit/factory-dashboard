"""The LIVE query layer — panel view models built from `readmodel.db`, opened URI `mode=ro`.

This is the S2 realisation of ux §1.3 / ADR-DASH-005 rule 1: *templates render, THIS computes.*
Every state, band, age, and freshness figure is produced here from the projected rows; the same
functions a D4 chat tool would call, so panels and chat can never disagree.

All opens are `mode=ro` (fence 4 / M-D4) via `db.connect_ro`. The five §5.2 states and the F-5
layering (ux §5.6) are computed by `readmodel.freshness`. On an un-projected DB (no projector
heartbeat) every panel renders honest LAGGING with its as-of chip — never a fake-live green.

The pinned §4.8 fixtures (`fixtures.py`) remain the deterministic render targets for the
`?state=` states-pack demo and the feature-page acceptance; this module is the live path the
projector feeds.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from backend import db
from backend.readmodel import freshness as fr
from backend.readmodel import viewmodels as vm
from backend.readmodel.queries import FleetView, HomeView
from backend.readmodel.viewmodels import (
    AgentRow,
    AgentsPanel,
    AsOf,
    BandChip,
    Banner,
    BuildBoardPanel,
    BuildRow,
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
    StreamRecency,
)


def _now() -> datetime:
    return datetime.now(vm.LONDON)


def _dt(value: object) -> datetime | None:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _chip(prefix: str, ts: datetime | None, now: datetime, threshold: int, state: PanelState) -> AsOf:
    """Build the panel's as-of chip. The dot reflects the COMPUTED §5.2 state immediately;
    freshness.js drifts it further on the wall clock (§5.1). A panel with no recency to claim
    still carries a chip (never omitted — coach-checked)."""
    if ts is None:
        dot = "empty" if state is PanelState.TRUE_EMPTY else "lagging"
        return AsOf(prefix=prefix, label="no events", title_iso="", dot_state=dot,
                    asof_epoch=None, threshold_secs=threshold)
    dot = {
        PanelState.LIVE: vm.dot_state_for((now - ts).total_seconds(), threshold),
        PanelState.LAGGING: "lagging",
        PanelState.TRUE_EMPTY: "empty",
    }.get(state, "lagging")
    return AsOf(prefix=prefix, label=vm.fmt_wall(ts, now=now), title_iso=vm.fmt_iso(ts),
                dot_state=dot, asof_epoch=ts.timestamp(), threshold_secs=threshold)


# --- projector liveness (the page-level honesty anchor, §5.6) ----------------


def _heartbeat_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT checked_at FROM service_health WHERE service='projector'").fetchone()
    return _dt(row[0]) if row else None


def _watermark(conn: sqlite3.Connection, stream: str) -> datetime | None:
    row = conn.execute(
        "SELECT last_event_at FROM consumer_watermarks WHERE stream=? AND consumer IN ('dashboard_ro','mirror')",
        (stream,),
    ).fetchone()
    return _dt(row[0]) if row else None


def page_chrome(conn: sqlite3.Connection, now: datetime | None = None) -> PageChrome:
    now = now or _now()
    hb = _heartbeat_at(conn)
    alive = fr.projector_alive(hb, now)
    events_ts = max(
        [t for t in (_watermark(conn, s) for s in ("PIPELINE", "AGENTS", "FLEET")) if t is not None],
        default=None,
    )
    if not alive:
        since = vm.fmt_wall(hb, now=now) if hb else "startup"
        return PageChrome(
            projector_state="stale",
            projector_label="projector ● STALE",
            events_as_of=_chip("events as-of", events_ts, now, vm.BUS_FRESH_SECS, PanelState.LAGGING),
            banner=Banner(
                kind="projector_stalled",
                text=f"PROJECTION LAGGING since {since} — panels show last projected state",
            ),
        )
    return PageChrome(
        projector_state="live",
        projector_label="projector ● live",
        events_as_of=_chip("events as-of", events_ts, now, vm.BUS_FRESH_SECS, PanelState.LIVE),
        banner=None,
    )


def projector_panel(conn: sqlite3.Connection, now: datetime | None = None) -> ProjectorPanel:
    now = now or _now()
    hb = _heartbeat_at(conn)
    alive = fr.projector_alive(hb, now)
    streams: list[StreamRecency] = []
    for name in ("PIPELINE", "AGENTS", "FLEET"):
        ts = _watermark(conn, name)
        if ts is None:
            streams.append(StreamRecency(name=name, label="no events yet", dot_state="quiet"))
        else:
            state = "live" if fr.watermark_fresh(ts, now) else "lagging"
            streams.append(StreamRecency(name=name, label=f"ev {vm.fmt_wall(ts, now=now)}", dot_state=state))
    mirror_ts = _watermark(conn, "FORGE_MIRROR")
    mirror_label = f"forge mirror {vm.fmt_wall(mirror_ts, now=now)}" if mirror_ts else "forge mirror — not yet run"
    if alive and hb is not None:
        return ProjectorPanel(
            panel_id="proj", title="Projector", state=PanelState.LIVE,
            as_of=_chip("heartbeat", hb, now, vm.PROJECTOR_STALE_SECS, PanelState.LIVE),
            heartbeat_state="live", heartbeat_label=f"live {vm.fmt_age((now - hb).total_seconds())}",
            streams=tuple(streams), forge_mirror_label=mirror_label,
        )
    since = vm.fmt_wall(hb, now=now) if hb else "startup"
    return ProjectorPanel(
        panel_id="proj", title="Projector", state=PanelState.LAGGING,
        as_of=_chip("heartbeat", hb, now, vm.PROJECTOR_STALE_SECS, PanelState.LAGGING),
        heartbeat_state="stale", heartbeat_label=f"STALE since {since}",
        streams=tuple(streams), forge_mirror_label=mirror_label,
    )


# --- P1 agents ---------------------------------------------------------------


def agents_panel(conn: sqlite3.Connection, now: datetime | None = None) -> AgentsPanel:
    now = now or _now()
    alive = fr.projector_alive(_heartbeat_at(conn), now)
    wm = _watermark(conn, "FLEET")
    rows = conn.execute(
        """SELECT agent_id, name, status, queue_depth, active_tasks, last_heartbeat_at,
                  deregistered_at, source_kind
             FROM agents ORDER BY (deregistered_at IS NOT NULL), source_kind, agent_id"""
    ).fetchall()
    agent_rows = tuple(_agent_row(r, now) for r in rows)
    state = fr.panel_state(projector_is_alive=alive, last_event_at=wm, now=now, row_count=len(agent_rows))
    return AgentsPanel(
        panel_id="p1", title="Agents", state=state,
        as_of=_chip("as of", wm, now, vm.BUS_FRESH_SECS, state), agents=agent_rows,
    )


def _agent_row(r: Any, now: datetime) -> AgentRow:
    agent_id, name, status, queue_depth, _active, last_hb, dereg, source_kind = r
    name = name or agent_id
    if dereg:
        return AgentRow(name=name, status_dot="unknown", status_text="deregistered",
                        liveness="deregistered — 1h stream, no history claimed", source_kind=source_kind or "fleet")
    if source_kind == "kv":
        hb = _dt(last_hb)
        age = vm.fmt_age((now - hb).total_seconds()) if hb else "?"
        return AgentRow(name=name, status_dot="ok", status_text="kv", liveness=f"kv {age}",
                        source_kind="kv", queue_depth="",
                        unmeasured_note="queue/tasks unmeasured (KV re-put freshness)")
    if source_kind == "register_only":
        hb = _dt(last_hb)
        when = vm.fmt_wall(hb, now=now) if hb else "boot"
        return AgentRow(name=name, status_dot="unknown", status_text="registered",
                        liveness=f"registered {when} — no heartbeat feed", source_kind="register_only",
                        queue_depth="", unmeasured_note="no heartbeat feed (ask A-10)")
    qd = "" if queue_depth is None else str(queue_depth)
    return AgentRow(name=name, status_dot=_status_dot(status), status_text=status or "active",
                    liveness=f"q:{qd}" if qd else (status or "active"), source_kind="fleet", queue_depth=qd)


def _status_dot(status: str | None) -> str:
    return {"ready": "ok", "busy": "ok", "active": "ok", "degraded": "idle", "draining": "idle"}.get(
        status or "", "ok"
    )


# --- P2 build board ----------------------------------------------------------

_IN_FLIGHT = ("QUEUED", "RUNNING", "PAUSED")


def build_board(conn: sqlite3.Connection, now: datetime | None = None) -> BuildBoardPanel:
    now = now or _now()
    alive = fr.projector_alive(_heartbeat_at(conn), now)
    wm = _watermark(conn, "PIPELINE")
    rows = conn.execute(
        """SELECT build_id, feature_id, project, status, started_at, queued_at, current_wave,
                  wave_total, progress_pct, title
             FROM builds WHERE status IN ('QUEUED','RUNNING','PAUSED')
             ORDER BY (started_at IS NULL), started_at DESC, queued_at DESC"""
    ).fetchall()
    build_rows = tuple(_build_row(conn, r, now) for r in rows)
    missing = sum(1 for r in rows if not r[9])
    plural = "s" if missing != 1 else ""
    note = f"{missing} feature{plural} have no projected title — title feed pending" if missing else ""
    state = fr.panel_state(projector_is_alive=alive, last_event_at=wm, now=now, row_count=len(build_rows))
    return BuildBoardPanel(
        panel_id="p2", title="Work in flight", state=state,
        as_of=_chip("as of", wm, now, vm.BUS_FRESH_SECS, state), rows=build_rows, title_gap_note=note,
    )


def _build_row(conn: sqlite3.Connection, r: Any, now: datetime) -> BuildRow:
    _build_id, feature_id, project, status, started_at, queued_at, wave, wave_total, pct, title = r
    open_issue = conn.execute(
        "SELECT kind FROM issues WHERE scope_id=? AND closed_at IS NULL "
        "AND kind IN ('gate_rejected','build_failed','approval_waiting') LIMIT 1",
        (feature_id,),
    ).fetchone()
    band = BandChip("G")
    station = "build"
    if open_issue and open_issue[0] == "approval_waiting":
        band, station = BandChip("A"), "approval"
    elif open_issue:
        band, station = BandChip("R"), "build"
    elif status == "QUEUED":
        station = "intake"
    progress = ""
    if status == "RUNNING" and wave and wave_total:
        progress = f"wave {wave}/{wave_total} · {int(pct or 0)}%"
    journey = _journey_for(status, station, progress)
    started = _dt(started_at) or _dt(queued_at)
    if started:
        detail = f"started {vm.fmt_wall(started, now=now)} · {vm.fmt_age((now - started).total_seconds())}"
    else:
        detail = "queued"
    return BuildRow(
        title=title or "", feature_id=feature_id, feature_link=f"/features/{feature_id}",
        project=project or "", provenance=ProvenanceBadge("bus"), journey=journey, band=band,
        current_station_label=station, detail=detail,
    )


def _journey_for(status: str, station: str, progress: str) -> vm.JourneyBar:
    order = ["intake", "planning", "approval", "spec/plan", "build"]
    passed = order[: order.index(station)] if station in order else order[:-1]
    states = dict.fromkeys(passed, "passed")
    states[station] = "current"
    return vm.journey_from_states(states, current_label=station, progress=progress)


# --- P3 gate events ----------------------------------------------------------


def gate_events(conn: sqlite3.Connection, now: datetime | None = None) -> GateEventsPanel:
    now = now or _now()
    alive = fr.projector_alive(_heartbeat_at(conn), now)
    wm = _watermark(conn, "PIPELINE")
    rows = conn.execute(
        """SELECT feature_id, stage_label, status, gate_mode, coach_score, duration_secs, completed_at, origin
             FROM stage_events ORDER BY completed_at DESC LIMIT 8"""
    ).fetchall()
    gate_rows = tuple(_gate_row(r, now) for r in rows)
    state = fr.panel_state(projector_is_alive=alive, last_event_at=wm, now=now, row_count=len(gate_rows))
    return GateEventsPanel(
        panel_id="p3", title="Recent gate events", state=state,
        as_of=_chip("as of", wm, now, vm.BUS_FRESH_SECS, state), rows=gate_rows,
    )


def _gate_row(r: Any, now: datetime) -> GateEventRow:
    feature_id, stage_label, status, gate_mode, coach_score, duration, completed, origin = r
    ts = _dt(completed)
    return GateEventRow(
        time_label=vm.fmt_wall(ts, now=now) if ts else "—",
        time_title=vm.fmt_iso(ts) if ts else "",
        feature_id=feature_id or "", feature_link=f"/features/{feature_id}",
        stage_label=stage_label or "", status=status or "",
        gate_mode="auto" if (gate_mode or "").startswith("AUTO") else (gate_mode or ""),
        coach_score=f"{coach_score:.2f}" if coach_score is not None else "",
        duration=vm.fmt_age(duration) if duration else "",
        origin=ProvenanceBadge("forge_sqlite" if origin == "forge_sqlite" else "bus"),
    )


# --- P4 needs-you -------------------------------------------------------------

_NEEDS_KINDS = ("approval_waiting", "escalation", "gate_rejected")
_GLYPH = {"approval_waiting": "⏳", "escalation": "⚠", "gate_rejected": "✖"}


def needs_you(conn: sqlite3.Connection, now: datetime | None = None) -> NeedsYouPanel:
    now = now or _now()
    alive = fr.projector_alive(_heartbeat_at(conn), now)
    wm = _watermark(conn, "AGENTS")
    rows = conn.execute(
        f"""SELECT kind, scope_id, opened_at, detail FROM issues
             WHERE closed_at IS NULL AND kind IN ({','.join('?' * len(_NEEDS_KINDS))})""",
        _NEEDS_KINDS,
    ).fetchall()
    items = sorted((_needs_item(r, now) for r in rows), key=lambda it: (_band_rank(it.band.band), -_age_of(it)))
    items_t = tuple(items)
    state = fr.panel_state(projector_is_alive=alive, last_event_at=wm, now=now, row_count=len(items_t))
    return NeedsYouPanel(
        panel_id="p4", title="Needs you", state=state,
        as_of=_chip("as of", wm, now, vm.BUS_FRESH_SECS, state), items=items_t, count=len(items_t),
    )


def _needs_item(r: Any, now: datetime) -> NeedsYouItem:
    kind, scope_id, opened_at, detail = r
    opened = _dt(opened_at)
    age_secs = (now - opened).total_seconds() if opened else 0.0
    band = _band_for(kind, age_secs)
    verb = {"approval_waiting": "approval WAITING", "gate_rejected": "gate REJECTED", "escalation": "ESCALATION"}[kind]
    return NeedsYouItem(
        kind=kind, kind_glyph=_GLYPH.get(kind, "•"),
        age_label=vm.fmt_age(age_secs), age_title=vm.fmt_iso(opened) if opened else "",
        band=band, headline=f"{verb} — {detail or scope_id or ''}",
        scope_link=f"/features/{scope_id}" if (scope_id or "").startswith("FEAT-") else "/issues",
        meta="", observes_only=(kind == "approval_waiting"),
    )


def _band_for(kind: str, age_secs: float) -> BandChip:
    if kind == "approval_waiting":
        if age_secs > 4 * 3600:
            return BandChip("R")
        if age_secs >= 3600:
            return BandChip("A")
        return BandChip("G")
    return BandChip("R")  # escalation / gate_rejected always red (design §1)


def _band_rank(band: str) -> int:
    return {"R": 0, "A": 1, "G": 2}.get(band, 3)


def _age_of(it: NeedsYouItem) -> float:
    dt = _dt(it.age_title)
    return dt.timestamp() if dt else 0.0


# --- P5 planning -------------------------------------------------------------


def planning(conn: sqlite3.Connection, now: datetime | None = None) -> PlanningPanel:
    now = now or _now()
    alive = fr.projector_alive(_heartbeat_at(conn), now)
    mirror_ts = _watermark(conn, "FORGE_MIRROR")
    rows = conn.execute(
        """SELECT correlation_id, target_repo, state, originating_user, queued_at, started_at, defer_count
             FROM planning_mirror ORDER BY COALESCE(started_at, queued_at) DESC"""
    ).fetchall()
    run_rows = tuple(_planning_row(r, now) for r in rows)
    state = fr.mirror_panel_state(
        projector_is_alive=alive, mirrored_at=mirror_ts, now=now, row_count=len(run_rows),
        fresh_secs=vm.MIRROR_FRESH_SECS,
    )
    prefix = "mirror"
    return PlanningPanel(
        panel_id="p5", title="Planning runs", state=state,
        as_of=_chip(prefix, mirror_ts, now, vm.MIRROR_FRESH_SECS, state), rows=run_rows,
    )


def _planning_row(r: Any, now: datetime) -> PlanningRunRow:
    cid, repo, state, user, queued_at, started_at, defer = r
    ref = _dt(started_at) or _dt(queued_at)
    age = vm.fmt_age((now - ref).total_seconds()) if ref else "—"
    return PlanningRunRow(
        plan_id=f"plan-{str(cid)[:4]}", project=repo or "—", state=state or "—",
        originating_user=user or "—", age=age, deferred_count=int(defer or 0),
    )


# --- P6 serving --------------------------------------------------------------

_SERVICES = ("litellm", "llama-swap", "nats")
_ENDPOINTS = {"litellm": ":4000", "llama-swap": ":9000", "nats": ":8222"}


def serving(conn: sqlite3.Connection, now: datetime | None = None) -> ServingPanel:
    now = now or _now()
    rows = {r[0]: r for r in conn.execute("SELECT service, status, detail, checked_at FROM service_health")}
    services: list[ServiceRow] = []
    latest: datetime | None = None
    for name in _SERVICES:
        row = rows.get(name)
        if row is None:
            services.append(ServiceRow(name=name, endpoint=_ENDPOINTS[name], dot="unknown", detail="not yet checked"))
            continue
        _svc, status, detail, checked_at = row
        ts = _dt(checked_at)
        latest = ts if latest is None or (ts and ts > latest) else latest
        stale = ts is None or (now - ts).total_seconds() >= vm.P6_FRESH_SECS
        resident, text = _split_resident(detail or "")
        if stale:
            last_ok = vm.fmt_wall(ts, now=now) if ts else "never"
            services.append(ServiceRow(name=name, endpoint=_ENDPOINTS[name], dot="unknown",
                                       detail=f"unknown — last ok {last_ok}", resident_models=resident))
        else:
            dot = "ok" if status == "ok" else "fail"
            services.append(ServiceRow(name=name, endpoint=_ENDPOINTS[name], dot=dot,
                                       detail=text, resident_models=resident))
    state = PanelState.LIVE if latest and (now - latest).total_seconds() < vm.P6_FRESH_SECS else PanelState.LAGGING
    return ServingPanel(
        panel_id="p6", title="Serving", state=state,
        as_of=_chip("checked", latest, now, vm.P6_FRESH_SECS, state), services=tuple(services),
    )


def _split_resident(detail: str) -> tuple[tuple[str, ...], str]:
    if detail.startswith("resident:"):
        models = tuple(m.strip() for m in detail[len("resident:"):].split(",") if m.strip())
        return models, "ok"
    return (), detail or "ok"


# --- composed page views -----------------------------------------------------


def home_view(db_path: Path, now: datetime | None = None) -> HomeView:
    now = now or _now()
    conn = db.connect_ro(db_path)
    try:
        return HomeView(
            chrome=page_chrome(conn, now), needs_you=needs_you(conn, now),
            build_board=build_board(conn, now), agents=agents_panel(conn, now),
            serving=serving(conn, now), projector=projector_panel(conn, now),
            gate_events=gate_events(conn, now), planning=planning(conn, now),
        )
    finally:
        conn.close()


def fleet_view(db_path: Path, now: datetime | None = None) -> FleetView:
    now = now or _now()
    conn = db.connect_ro(db_path)
    try:
        return FleetView(chrome=page_chrome(conn, now), agents=agents_panel(conn, now),
                         serving=serving(conn, now), projector=projector_panel(conn, now))
    finally:
        conn.close()


_PANEL_BUILDERS = {
    "p1": agents_panel, "p2": build_board, "p3": gate_events, "p4": needs_you,
    "p5": planning, "p6": serving, "proj": projector_panel,
}


def panel_view(db_path: Path, panel_id: str, now: datetime | None = None) -> vm.Panel:
    """Live single-panel build for the SSE-driven `/fragments/{panel}` refetch (no `?state=`)."""
    now = now or _now()
    builder = _PANEL_BUILDERS[panel_id]
    conn = db.connect_ro(db_path)
    try:
        return builder(conn, now)
    finally:
        conn.close()


def scope_ids_from(scope_keys: str | None) -> list[str]:
    if not scope_keys:
        return []
    try:
        data = json.loads(scope_keys)
        return [str(x) for x in data] if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []
