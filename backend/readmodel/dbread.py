"""The query layer: every page's view model, built from the copy of Forge's record.

Templates render; this computes. The same functions will back the chat (design requirement 9),
so a page and an answer can never disagree. Every open is read-only (`db.connect_ro`).

Freshness: each page carries the time Forge's record was last read. If that is more than two
minutes ago, or the feed itself has stopped, every page shows one banner line saying so and the
cards show the last known data with its time (design requirement 1).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from backend import db
from backend.readmodel import costs as cost
from backend.readmodel import viewmodels as vm
from backend.readmodel.costs import CostBook, load_cost_book
from backend.readmodel.factory import (
    STATIONS,
    Build,
    Factory,
    Feature,
    Outcome,
    load_factory,
    parse_dt,
)
from backend.readmodel.issues import Issue, find_issues, recent
from backend.readmodel.norms import GAP, OUTCOME_WORDS, Norms, load_norms, on_track
from backend.readmodel.timing import FeatureTime, feature_time
from backend.readmodel.viewmodels import (
    ActivityPanel,
    ActivityRow,
    AsOf,
    AttemptRow,
    BandChip,
    Banner,
    DeliveredPanel,
    DeliveredRow,
    ExportDeliveredRow,
    ExportMilestone,
    ExportReport,
    FeaturePage,
    FeatureRow,
    FeaturesPanel,
    FeedPanel,
    IssueRow,
    IssuesEncountered,
    JourneyBar,
    JourneyStation,
    MilestoneRow,
    NeedsItem,
    NeedsPanel,
    PageChrome,
    PanelState,
    PlanningPanel,
    PlanningRow,
    PlanVsActual,
    ProjectView,
    ReportHeadline,
    ReportIssueRow,
    ServiceRow,
    ServingPanel,
    SignalRow,
    SpendSlot,
    StageCostRow,
    TimeSegment,
    TimeSlot,
    WaitRow,
    WeeklyReport,
    WeekRow,
)


def _now() -> datetime:
    return datetime.now(UTC)


# --- freshness -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Freshness:
    read_at: datetime | None       # the last good read of Forge's record
    heartbeat_at: datetime | None  # the feed's own heartbeat
    error: str

    def feed_alive(self, now: datetime) -> bool:
        return self.heartbeat_at is not None and (now - self.heartbeat_at).total_seconds() < vm.PROJECTOR_STALE_SECS

    def fresh(self, now: datetime) -> bool:
        return (self.feed_alive(now) and self.read_at is not None
                and (now - self.read_at).total_seconds() < vm.STALE_AFTER_SECS)


def freshness(conn: sqlite3.Connection) -> Freshness:
    row = conn.execute("SELECT last_success_at, last_error FROM mirror_status WHERE source='forge'").fetchone()
    hb = conn.execute("SELECT checked_at FROM service_health WHERE service='projector'").fetchone()
    return Freshness(
        read_at=parse_dt(row[0]) if row else None,
        heartbeat_at=parse_dt(hb[0]) if hb else None,
        error=str(row[1]) if row and row[1] else "",
    )


def _read_chip(fr: Freshness, now: datetime, state: PanelState | None = None) -> AsOf:
    if fr.read_at is None:
        return AsOf(prefix="factory record", label="not read yet", title_iso="", dot_state="stale")
    if state is PanelState.TRUE_EMPTY:
        dot = "empty"
    elif fr.fresh(now):
        dot = vm.dot_state_for((now - fr.read_at).total_seconds(), vm.MIRROR_FRESH_SECS)
    else:
        dot = "stale"
    return AsOf(prefix="factory record read", label=vm.fmt_wall(fr.read_at, now=now),
                title_iso=vm.fmt_iso(fr.read_at), dot_state=dot, asof_epoch=fr.read_at.timestamp(),
                threshold_secs=vm.MIRROR_FRESH_SECS)


#: Ages on a page ("waiting 2 h so far", a stall crossing its threshold) grow without any change
#: to the record, so the data version also moves on every five minutes.
VERSION_TICK_SECS = 300


def data_version(conn: sqlite3.Connection, fr: Freshness, now: datetime) -> str:
    """The version of the data behind a page: the feed's change count (it only rises), whether the
    record is fresh or stale, and a five-minute clock tick. An open page whose version differs from
    the latest one fetches its content again (static/freshness.js)."""
    try:
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='change_log'").fetchone()
    except sqlite3.Error:
        row = None
    seq = int(row[0]) if row and row[0] is not None else 0
    state = "fresh" if fr.fresh(now) else "stale"
    cost_state = cost.cost_freshness(conn).state(now)
    return f"{seq}.{state}.{cost_state}.{int(now.timestamp() // VERSION_TICK_SECS)}"


def cost_banner(conn: sqlite3.Connection, now: datetime) -> Banner | None:
    """The spend log's own line, apart from Forge's: one can be healthy while the other is stale.
    Shown once the last good read is over 10 minutes old (design §3). Before any read succeeds there is
    no banner: every cost figure itself says "Cost data not available"."""
    cf = cost.cost_freshness(conn)
    if cf.state(now) != "stale":
        return None
    return Banner(kind="stale", text=f"Cost data last read at {vm.fmt_wall(cf.read_at, now=now)} — cost "  # type: ignore[arg-type]
                                           "figures may be incomplete.")


def page_chrome(conn: sqlite3.Connection, now: datetime | None = None) -> PageChrome:
    now = now or _now()
    fr = freshness(conn)
    chip = _read_chip(fr, now)
    version = data_version(conn, fr, now)
    cb = cost_banner(conn, now)
    if fr.fresh(now):
        return PageChrome(feed_state="live", read_as_of=chip, version=version, cost_banner=cb)
    when = vm.fmt_wall(fr.read_at, now=now) if fr.read_at else None
    if not fr.feed_alive(now):
        text = ("The dashboard's data feed is not running. "
                + (f"Factory data last read at {when}." if when else "It has not read the factory's record yet."))
    elif when is None:
        text = "The dashboard has not read the factory's record yet."
    else:
        text = f"Factory data last read at {when} — the dashboard cannot reach the factory's record."
    return PageChrome(feed_state="stale", read_as_of=chip, banner=Banner(kind="stale", text=text), version=version,
                      cost_banner=cb)


def _state(fr: Freshness, now: datetime, rows: int) -> PanelState:
    if not fr.fresh(now):
        return PanelState.LAGGING
    return PanelState.LIVE if rows else PanelState.TRUE_EMPTY


# --- shared row builders ---------------------------------------------------------------------------


def project_link(project: str) -> str:
    """Projects are `owner/name`; the route takes both parts as path segments."""
    return f"/projects/{project}" if project else ""


def journey(f: Feature) -> JourneyBar:
    stations = []
    for i, name in enumerate(STATIONS):
        if f.outcome is Outcome.DELIVERED or i < f.station:
            state = "passed"
        elif i == f.station:
            # A station where the feature is waiting on a person is shown as waiting.
            state = ("waiting" if f.open_wait() is not None else "current") if f.in_progress else "stopped"
        else:
            state = "future"
        stations.append(JourneyStation(name=name, glyph_state=state))
    return JourneyBar(stations=tuple(stations), current_label=f.status_text)


_BAND_WORDS = {"G": "on track", "A": "at risk", "R": "off track", "—": "not measured"}


# An issue's colour is how much it matters, not whether a feature is on track.
_ISSUE_WORDS = {"G": "low", "A": "moderate", "R": "serious"}


def _band(band: str, words: str = "") -> BandChip:
    return BandChip(band, words or _BAND_WORDS.get(band, ""))


def _issue_band(band: str) -> BandChip:
    return BandChip(band, _ISSUE_WORDS.get(band, ""))


def elapsed(f: Feature, now: datetime) -> str:
    if f.sent_at is None:
        return "—"
    end = f.ended_at if not f.in_progress else None
    secs = ((end or now) - f.sent_at).total_seconds()
    return vm.fmt_age(secs) + ("" if end else " so far")


def feature_row(f: Feature, norms: Norms, now: datetime, book: CostBook | None = None) -> FeatureRow:
    ot = on_track(f, norms, now)
    sent = f.sent_at
    return FeatureRow(
        title=f.title, code=f.code, link=f.link, project=f.project, project_link=project_link(f.project),
        status_text=f.status_text, journey=journey(f), band=_band(ot.band, ot.words.lower()),
        elapsed=elapsed(f, now),
        sent_label=vm.fmt_wall(sent, now=now) if sent else "—", sent_title=vm.fmt_iso(sent) if sent else "",
        in_progress=f.in_progress, spend=book.feature_slot(f) if book else SpendSlot(),
        time=time_slot(feature_time(f, now)),
    )


# --- time ---------------------------------------------------------------------------------------------

_SEG = {"planning": "plan", "building": "build", "merge_check": "review", "not_placed": "none"}


def time_slot(ft: FeatureTime | None) -> TimeSlot:
    """A feature's elapsed, machine and waiting time as display data (requirement 6)."""
    if ft is None:
        return TimeSlot(reason="The record has no time this was sent.")
    if ft.unmeasured:
        return TimeSlot(reason=ft.unmeasured, not_measured_count=1)
    return _time_from(ft.elapsed, ft.so_far, [(p.key, p.label, p.kind, p.seconds, p.open) for p in ft.parts])


def sum_time(times: Iterable[FeatureTime | None]) -> TimeSlot:
    """Several features' times added together (a project, a week)."""
    given = [t for t in times if t is not None]
    known = [t for t in given if not t.unmeasured]
    left_out = len(given) - len(known)
    if not known:
        return TimeSlot(reason="Not measured: no feature here has time the record can account for."
                        if left_out else "No features with a recorded start.", not_measured_count=left_out)
    parts: dict[str, list[object]] = {}
    for t in known:
        for p in t.parts:
            row = parts.setdefault(p.key, [p.label, p.kind, 0.0, False])
            row[2] = float(row[2]) + p.seconds  # type: ignore[arg-type]
            row[3] = bool(row[3]) or p.open
    slot = _time_from(sum(t.elapsed for t in known), any(t.so_far for t in known),
                      [(k, str(v[0]), str(v[1]), float(v[2]), bool(v[3])) for k, v in parts.items()])  # type: ignore[arg-type]
    if not left_out:
        return slot
    noun = "feature is" if left_out == 1 else "features are"
    return replace(slot, not_measured_count=left_out,
                   reason=f"{left_out} {noun} left out: a build was closed in a later clean-up, so the record does "
                          "not say how long it ran.")


def _time_from(elapsed_secs: float, so_far: bool, parts: list[tuple[str, str, str, float, bool]]) -> TimeSlot:
    suffix = " so far" if so_far else ""
    machine = sum(p[3] for p in parts if p[2] == "machine")
    waiting = sum(p[3] for p in parts if p[2] == "wait")
    segs = tuple(TimeSegment(key=k, label=label, kind=kind, seg=_SEG.get(k, "wait"), seconds=secs,
                             text=vm.fmt_age(secs) + (" so far" if is_open else ""),
                             share=(secs / elapsed_secs) if elapsed_secs else 0.0, open=is_open)
                 for k, label, kind, secs, is_open in parts)
    return TimeSlot(state="measured", elapsed=vm.fmt_age(elapsed_secs) + suffix, elapsed_secs=elapsed_secs,
                    machine=vm.fmt_age(machine), machine_secs=machine, waiting=vm.fmt_age(waiting),
                    waiting_secs=waiting, so_far=so_far, segments=segs)


def issue_row(i: Issue, now: datetime) -> IssueRow:
    return IssueRow(
        kind=i.kind, band=_issue_band(i.band), subject=i.subject,
        link=i.link, project=i.project, opened_label=vm.fmt_wall(i.opened_at, now=now),
        opened_title=vm.fmt_iso(i.opened_at),
        age=vm.fmt_age(((i.closed_at or now) - i.opened_at).total_seconds()), detail=i.detail,
        closed_label=vm.fmt_wall(i.closed_at, now=now) if i.closed_at else "",
    )


# --- recent activity: Forge's stage record in words ------------------------------------------------

_STEP_WORDS = {
    "autobuild": "Build",
    "gate-resume": "Go-ahead to build",
    "the merge word": "Merge decision",
    "merge-deploy": "Merge and check",
    "mission landing": "Joining the mission's branch",
    "task-work": "Build task",
    "task-review": "Task review",
}


def _activity_words(  # noqa: PLR0911 (one return per kind of row)
    b: Build, s_label: str, s_status: str, details: dict[str, object],
) -> tuple[str, str] | None:
    """(step, result) for a stage row worth showing, or None for bookkeeping rows."""
    if "gate_pause" in details:
        return "Go-ahead to build", "asked for"
    if "gate_resume" in details:
        return "Go-ahead to build", "given"
    if "merge_offer" in details:
        return "Merge decision", "asked for"
    decision = details.get("merge_decision")
    if isinstance(decision, dict) and "decision" in decision:
        word = str(decision["decision"])
        return "Merge decision", {"approve": "approved", "reject": "declined"}.get(word, word)
    if s_label == "merge-deploy":
        if s_status == "PASSED" and "merged_sha" in details:
            if details.get("result") == "published-to-the-mission-branch":
                return "Joined onto the mission's branch", "passed"
            return "Merge and check", "passed"
        if s_status == "FAILED":
            return "Merge and check", "failed"
        return None
    if s_label in ("task-work", "task-review") and s_status in ("PASSED", "FAILED"):
        return _STEP_WORDS[s_label], s_status.lower()
    return None


def _subject_for(b: Build, factory: Factory) -> tuple[str, str]:
    if b.is_mission_merge:
        mission = next((m for m in factory.missions if m.id == b.mission_id), None)
        if mission is not None:
            return (f"{mission.label} ({mission.title})" if mission.title else mission.label), ""
        return f"mission {b.mission_id}'s merge", ""
    f = factory.feature(b.code)
    return (f.label, f.link) if f else (b.code, "")


def activity_rows(factory: Factory, builds: Iterable[Build], now: datetime, limit: int) -> tuple[ActivityRow, ...]:
    events: list[tuple[datetime, ActivityRow]] = []
    for b in builds:
        subject, link = _subject_for(b, factory)
        if b.started_at:
            events.append((b.started_at, ActivityRow(vm.fmt_wall(b.started_at, now=now), vm.fmt_iso(b.started_at),
                                                     subject, link, "Build", "started")))
        if b.completed_at and b.status in ("COMPLETE", "FAILED", "CANCELLED") and not b.is_mission_merge:
            result = {"COMPLETE": "finished", "FAILED": "failed", "CANCELLED": "cancelled"}[b.status]
            events.append((b.completed_at, ActivityRow(vm.fmt_wall(b.completed_at, now=now),
                                                       vm.fmt_iso(b.completed_at), subject, link, "Build", result)))
        for s in b.stages:
            words = _activity_words(b, s.stage_label, s.status, s.details)
            if words is None:
                continue
            events.append((s.at, ActivityRow(vm.fmt_wall(s.at, now=now), vm.fmt_iso(s.at), subject, link, *words)))
    events.sort(key=lambda e: e[0], reverse=True)
    return tuple(row for _, row in events[:limit])


# --- the Now page panels -------------------------------------------------------------------------


def in_flight(conn: sqlite3.Connection, now: datetime, factory: Factory, norms: Norms,
              book: CostBook | None = None) -> FeaturesPanel:
    fr = freshness(conn)
    book = book or load_cost_book(conn, now)
    rows = tuple(feature_row(f, norms, now, book) for f in factory.features if f.in_progress)
    return FeaturesPanel(panel_id="p2", title="In progress", state=_state(fr, now, len(rows)),
                         as_of=_read_chip(fr, now), rows=rows, empty_copy="Nothing is in progress.")


def needs_you(conn: sqlite3.Connection, now: datetime, factory: Factory, norms: Norms) -> NeedsPanel:
    fr = freshness(conn)
    items = []
    for i in find_issues(factory, norms, now):
        if not i.is_open or not i.kind.startswith("Waiting"):
            continue
        items.append(NeedsItem(what=i.kind, subject=i.subject, link=i.link,
                               age_label=vm.fmt_age((now - i.opened_at).total_seconds()),
                               age_title=vm.fmt_iso(i.opened_at), band=_issue_band(i.band)))
    items.sort(key=lambda it: it.age_title)
    return NeedsPanel(panel_id="p4", title="Waiting for a person", state=_state(fr, now, len(items)),
                      as_of=_read_chip(fr, now), items=tuple(items))


def recent_activity(conn: sqlite3.Connection, now: datetime, factory: Factory) -> ActivityPanel:
    fr = freshness(conn)
    since = now - timedelta(days=2)
    builds = [b for b in factory.builds if (b.queued_at or now) >= since - timedelta(days=5)]
    rows = activity_rows(factory, builds, now, limit=12)
    return ActivityPanel(panel_id="p3", title="Recent activity", state=_state(fr, now, len(rows)),
                         as_of=_read_chip(fr, now), rows=rows)


def planning(conn: sqlite3.Connection, now: datetime, factory: Factory) -> PlanningPanel:
    """Planning in the last 7 days, plus any still open, newest first."""
    fr = freshness(conn)
    cut = now - timedelta(days=7)
    rows = []
    for f in factory.features:
        run = f.planning
        if run is None:
            continue
        ref = run.started_at or run.queued_at
        if not (run.active or (ref and ref >= cut)):
            continue
        rows.append(PlanningRow(
            title=f.label, link=f.link, project=f.project, status_text=_planning_words(f),
            age=vm.fmt_age((now - ref).total_seconds()) if ref else "—", requester="the requester",
        ))
    shown = rows[:PLANNING_ROWS]
    return PlanningPanel(panel_id="p5", title="Planning", state=_state(fr, now, len(rows)),
                         as_of=_read_chip(fr, now), rows=tuple(shown), more=len(rows) - len(shown))


PLANNING_ROWS = 15


def _planning_words(f: Feature) -> str:
    run = f.planning
    assert run is not None
    if run.active or not f.builds:
        return f.status_text
    return "Planned; now " + f.status_text[0].lower() + f.status_text[1:]


# --- services and the feed (Fleet) ----------------------------------------------------------------

_SERVICES = ("litellm", "llama-swap", "nats")
_ENDPOINTS = {"litellm": ":4000", "llama-swap": ":9000", "nats": ":8222"}
_SERVICE_NAMES = {"litellm": "Model gateway (LiteLLM)", "llama-swap": "Model server (llama-swap)",
                  "nats": "Message bus (NATS)"}


def serving(conn: sqlite3.Connection, now: datetime | None = None, *_: object) -> ServingPanel:
    now = now or _now()
    rows = {r[0]: r for r in conn.execute("SELECT service, status, detail, checked_at FROM service_health")}
    services: list[ServiceRow] = []
    latest: datetime | None = None
    for name in _SERVICES:
        label = _SERVICE_NAMES[name]
        row = rows.get(name)
        if row is None:
            services.append(ServiceRow(name=label, endpoint=_ENDPOINTS[name], dot="unknown", detail="not checked yet"))
            continue
        _svc, status, detail, checked_at = row
        ts = parse_dt(checked_at)
        latest = ts if latest is None or (ts and ts > latest) else latest
        stale = ts is None or (now - ts).total_seconds() >= vm.P6_FRESH_SECS
        resident, text = _split_resident(detail or "")
        if stale:
            last = vm.fmt_wall(ts, now=now) if ts else "never"
            services.append(ServiceRow(name=label, endpoint=_ENDPOINTS[name], dot="unknown",
                                       detail=f"unknown — last checked {last}", resident_models=resident))
        else:
            services.append(ServiceRow(name=label, endpoint=_ENDPOINTS[name], dot="ok" if status == "ok" else "fail",
                                       detail="answering" if status == "ok" else f"not answering ({text})",
                                       resident_models=resident))
    fresh = latest is not None and (now - latest).total_seconds() < vm.P6_FRESH_SECS
    chip = (AsOf(prefix="checked", label=vm.fmt_wall(latest, now=now), title_iso=vm.fmt_iso(latest),
                 dot_state="live" if fresh else "stale", asof_epoch=latest.timestamp(), threshold_secs=vm.P6_FRESH_SECS)
            if latest else AsOf(prefix="checked", label="not yet", title_iso="", dot_state="stale"))
    return ServingPanel(panel_id="p6", title="Services", state=PanelState.LIVE if fresh else PanelState.LAGGING,
                        as_of=chip, services=tuple(services))


def _split_resident(detail: str) -> tuple[tuple[str, ...], str]:
    if detail.startswith("resident:"):
        return tuple(m.strip() for m in detail[len("resident:"):].split(",") if m.strip()), "ok"
    return (), detail or "ok"


def feed_panel(conn: sqlite3.Connection, now: datetime | None = None, *_: object) -> FeedPanel:
    now = now or _now()
    fr = freshness(conn)
    alive = fr.feed_alive(now)
    hb = fr.heartbeat_at
    hb_label = (f"running (last heartbeat {vm.fmt_age((now - hb).total_seconds())} ago)" if alive and hb
                else f"stopped since {vm.fmt_wall(hb, now=now)}" if hb else "not started")
    read = (f"The factory's record was last read at {vm.fmt_wall(fr.read_at, now=now)}" if fr.read_at
            else "The factory's record has not been read yet")
    return FeedPanel(panel_id="proj", title="Dashboard data feed",
                     state=PanelState.LIVE if fr.fresh(now) else PanelState.LAGGING, as_of=_read_chip(fr, now),
                     heartbeat_ok=alive, heartbeat_label=hb_label, read_label=read, error=fr.error)


# --- Delivered --------------------------------------------------------------------------------------


def _window_bounds(window_from: str, window_to: str) -> tuple[datetime, datetime]:
    """The inclusive London calendar window as UTC instants [lo, hi)."""
    lo_d = date.fromisoformat(window_from)
    hi_d = date.fromisoformat(window_to) + timedelta(days=1)
    lo = datetime(lo_d.year, lo_d.month, lo_d.day, tzinfo=vm.LONDON).astimezone(UTC)
    hi = datetime(hi_d.year, hi_d.month, hi_d.day, tzinfo=vm.LONDON).astimezone(UTC)
    return lo, hi


def default_window(now: datetime) -> tuple[str, str]:
    """This week, Monday to Sunday, London calendar."""
    today = vm.to_london(now).date()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def parse_window(window: tuple[str, str] | None, now: datetime) -> tuple[str, str]:
    """A valid (from, to) pair, or this week. A malformed or reversed window falls back to this week."""
    if window:
        try:
            lo, hi = date.fromisoformat(window[0]), date.fromisoformat(window[1])
        except ValueError:
            return default_window(now)
        if lo <= hi:
            return lo.isoformat(), hi.isoformat()
    return default_window(now)


def _commit(f: Feature) -> str:
    for b in f.builds:
        for s in b.stages:
            if (s.stage_label == "merge-deploy" and s.status == "PASSED" and s.details.get("merged_sha")
                    and s.at == f.delivered_at):
                return str(s.details["merged_sha"])[:10]
    return ""


def delivered_row(f: Feature, book: CostBook | None = None, now: datetime | None = None) -> DeliveredRow:
    assert f.delivered_at is not None
    return DeliveredRow(
        delivered_date=vm.fmt_day(f.delivered_at), delivered_title=vm.fmt_iso(f.delivered_at), title=f.title,
        code=f.code, link=f.link, project=f.project, project_link=project_link(f.project), commit=_commit(f),
        spend=book.feature_slot(f) if book else SpendSlot(),
        time=time_slot(feature_time(f, now or _now())),
    )


def _in_window(t: datetime | None, lo: datetime, hi: datetime) -> bool:
    return t is not None and lo <= t < hi


#: Projects in scope: None means every project (only the operator's own view asks for that); a tuple
#: means exactly those projects, so an empty tuple matches nothing.
Scope = tuple[str, ...] | None


def in_scope(project: str, projects: Scope) -> bool:
    """True when `project` is in scope: every project for None, else only the ones named."""
    return projects is None or project in projects


def delivered_in(factory: Factory, lo: datetime, hi: datetime, projects: Scope = None) -> list[Feature]:
    out = [f for f in factory.features if f.outcome is Outcome.DELIVERED and _in_window(f.delivered_at, lo, hi)
           and in_scope(f.project, projects)]
    out.sort(key=lambda f: f.delivered_at or lo)
    return out


def _other_endings(factory: Factory, lo: datetime, hi: datetime) -> str:
    words = {
        Outcome.NOT_MERGED: "built, not merged", Outcome.MERGE_FAILED: "merge check failed",
        Outcome.BUILD_FAILED: "build failed", Outcome.CANCELLED: "cancelled",
        Outcome.PLANNING_FAILED: "planning failed",
        Outcome.MISSION_NOT_MERGED: "on a mission branch that was not merged",
    }
    counts: dict[str, int] = {}
    for f in factory.features:
        if f.outcome in words and _in_window(f.ended_at, lo, hi):
            counts[words[f.outcome]] = counts.get(words[f.outcome], 0) + 1
    if not counts:
        return ""
    parts = [f"{n} {w}" for w, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    return "Also finished in this window without being delivered: " + "; ".join(parts) + "."


def delivered_panel(
    conn: sqlite3.Connection, now: datetime | None = None, factory: Factory | None = None,
    norms: Norms | None = None, *, window: tuple[str, str] | None = None,
) -> DeliveredPanel:
    now = now or _now()
    factory = factory or load_factory(conn, now)
    win_from, win_to = parse_window(window, now)
    lo, hi = _window_bounds(win_from, win_to)
    fr = freshness(conn)
    book = load_cost_book(conn, now)
    rows = tuple(delivered_row(f, book, now) for f in delivered_in(factory, lo, hi))
    return DeliveredPanel(
        panel_id="p7", title="Delivered", state=_state(fr, now, len(rows)), as_of=_read_chip(fr, now),
        rows=rows, delivered_count=len(rows), window_from=win_from, window_to=win_to,
        other_endings=_other_endings(factory, lo, hi),
    )


# --- page views ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HomeView:
    chrome: PageChrome
    needs_you: NeedsPanel
    in_flight: FeaturesPanel
    activity: ActivityPanel
    planning: PlanningPanel
    serving: ServingPanel
    feed: FeedPanel
    spend_today: SpendSlot = field(default_factory=SpendSlot)  # every model call today (London), tagged or not
    spend_week: SpendSlot = field(default_factory=SpendSlot)   # the same, this week (Monday to now)


@dataclass(frozen=True)
class FeaturesView:
    chrome: PageChrome
    features: FeaturesPanel
    days: int | None


@dataclass(frozen=True)
class IssuesView:
    chrome: PageChrome
    open_rows: tuple[IssueRow, ...]
    closed_rows: tuple[IssueRow, ...]
    note: str
    hidden_line: str = ""    # how many old open failures the 14-day cut-off leaves out
    hidden_count: int = 0    # that number, so a total of open issues can include them (the chat)


@dataclass(frozen=True)
class ProjectRow:
    project: str
    link: str
    features: int          # features on record
    in_progress: int
    delivered: int         # merged and checked, ever
    open_issues: int       # listed open issues plus older unresolved failures
    spend: SpendSlot = field(default_factory=SpendSlot)  # tagged calls of the project's features (since tagging)
    time: TimeSlot = field(default_factory=TimeSlot)     # summed over the project's features


@dataclass(frozen=True)
class ProjectsView:
    chrome: PageChrome
    rows: tuple[ProjectRow, ...]
    as_of: AsOf


@dataclass(frozen=True)
class DeliveredView:
    chrome: PageChrome
    panel: DeliveredPanel


@dataclass(frozen=True)
class FleetView:
    chrome: PageChrome
    serving: ServingPanel
    feed: FeedPanel


def _open(db_path: Path) -> sqlite3.Connection:
    """A read-only connection holding ONE read snapshot for a whole page or fragment.

    Every view builds its content, its read time and its data version on this connection, and a
    feed pass can commit at any moment (the store is in WAL mode, so it does not wait for readers).
    Without one read transaction those reads could straddle a pass: old features under the new
    version and read time, which later polls would then accept as current. So the transaction is
    opened here and the snapshot is fixed at once, by reading the version's change count first;
    every later read sees the store as it was then. The view's `conn.close()` ends it. The live
    update stream keeps its own connection outside this, so it always sees new changes."""
    conn = db.connect_ro(db_path)
    conn.execute("BEGIN")
    try:
        conn.execute("SELECT seq FROM sqlite_sequence WHERE name='change_log'").fetchone()
    except sqlite3.Error:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()  # any read fixes the snapshot
    return conn


def snapshot(db_path: Path) -> sqlite3.Connection:
    """One read snapshot (see `_open`) for a caller that builds one answer from several views, such
    as the chat: pass it to each view as ``conn=`` and close it when done. A view given ``conn``
    reads on it and leaves it open; without one it opens and closes its own, as the pages do."""
    return _open(db_path)


def home_view(db_path: Path, now: datetime | None = None) -> HomeView:
    now = now or _now()
    conn = _open(db_path)
    try:
        factory, norms = load_factory(conn, now), load_norms()
        book = load_cost_book(conn, now)
        return HomeView(
            chrome=page_chrome(conn, now), needs_you=needs_you(conn, now, factory, norms),
            in_flight=in_flight(conn, now, factory, norms, book), activity=recent_activity(conn, now, factory),
            planning=planning(conn, now, factory), serving=serving(conn, now), feed=feed_panel(conn, now),
            spend_today=factory_spend(conn, book, cost.day_start(now), now),
            spend_week=factory_spend(conn, book, cost.week_start(now), now),
        )
    finally:
        conn.close()


def factory_spend(conn: sqlite3.Connection, book: CostBook, lo: datetime, now: datetime) -> SpendSlot:
    """Every model call from `lo` to now, all projects, tagged or not."""
    return book.slot(cost.window_totals(conn, lo, now + timedelta(seconds=1)), scope="factory")


def _stage_rows(conn: sqlite3.Connection, f: Feature, book: CostBook) -> tuple[StageCostRow, ...]:
    """Cost split into planning, building, and merge and check, for a measured feature."""
    slot = book.feature_slot(f)
    if not slot.measured or not slot.calls:
        return ()
    split = cost.split_by_stage(f, cost.feature_calls(conn, f))
    total = sum(t.cost for t in split.values())
    seg = {"planning": "plan", "build": "build", "review": "review"}
    return tuple(StageCostRow(key=k, label=label, seg=seg[k], spend=book.slot(split[k], scope="stage"),
                              share=(split[k].cost / total) if total else 0.0)
                 for k, label in cost.STAGES)



FEATURE_WINDOWS: tuple[int | None, ...] = (7, 30, None)


def features_view(db_path: Path, days: int | None = 7, now: datetime | None = None) -> FeaturesView:
    """Every feature, newest first: the last `days` days by default, or all of them."""
    now = now or _now()
    conn = _open(db_path)
    try:
        factory, norms = load_factory(conn, now), load_norms()
        fr = freshness(conn)
        cut = now - timedelta(days=days) if days else None
        chosen = [f for f in factory.features if cut is None or f.in_progress or (f.sent_at and f.sent_at >= cut)]
        book = load_cost_book(conn, now)
        rows = tuple(feature_row(f, norms, now, book) for f in chosen)
        span = f"sent in the last {days} days, and anything still in progress" if days else "every feature on record"
        panel = FeaturesPanel(panel_id="features", title="Features", state=_state(fr, now, len(rows)),
                              as_of=_read_chip(fr, now), rows=rows, empty_copy="No features in this period.",
                              note=f"{len(rows)} features: {span}. {norms.source_line}")
        return FeaturesView(chrome=page_chrome(conn, now), features=panel, days=days)
    finally:
        conn.close()


def _norms_line(norms: Norms) -> str:
    return (f"{norms.source_line} Planning: {norms.planning_minutes.describe()}. "
            f"Build: {norms.build_minutes.describe()}.")


def feature_page(db_path: Path, key: str, now: datetime | None = None, *,
                 conn: sqlite3.Connection | None = None) -> tuple[PageChrome, FeaturePage] | None:
    now = now or _now()
    owned = conn is None
    conn = _open(db_path) if conn is None else conn
    try:
        factory, norms = load_factory(conn, now), load_norms()
        f = factory.feature(key)
        if f is None:
            return None
        fr = freshness(conn)
        book = load_cost_book(conn, now)
        ot = on_track(f, norms, now)
        signals = tuple(SignalRow(s.name, s.value, _band(s.band, "not measured" if s.band == GAP else ""),
                                  s.norm, s.note) for s in ot.signals)
        attempts = tuple(AttemptRow(
            build_id=b.build_id,
            queued_label=vm.fmt_wall(b.queued_at, now=now) if b.queued_at else "—",
            queued_title=vm.fmt_iso(b.queued_at) if b.queued_at else "",
            status_text=_build_words(b),
            started_by="the factory (a fix after a failed check)" if b.correlation_id.startswith("fix-")
            else ("the mission" if b.is_mission_feature else "the request"),
        ) for b in f.builds)
        waits = tuple(WaitRow(
            kind=w.kind, started_label=vm.fmt_wall(w.started_at, now=now), started_title=vm.fmt_iso(w.started_at),
            ended_label="waiting now" if w.open else vm.fmt_wall(w.ended_at, now=now) if w.ended_at else "",
            duration=vm.fmt_age(w.seconds(now)), open=w.open,
        ) for w in f.waits)
        own = [i for i in find_issues(factory, norms, now) if i.feature is f]
        mission_line = ""
        if f.mission is not None:
            m = f.mission
            mission_line = (f"Part of mission {m.id}" + (f" ({m.title})" if m.title else "")
                            + f": the mission is {_mission_words(m.status)}.")
        sent = f.sent_at
        page = FeaturePage(
            title=f.title, code=f.code, key=f.key, project=f.project, project_link=project_link(f.project),
            request_text=f.request_text, sent_label=vm.fmt_wall(sent, now=now) if sent else "—",
            sent_title=vm.fmt_iso(sent) if sent else "", status_text=f.status_text, mission_line=mission_line,
            journey=journey(f), as_of=_read_chip(fr, now), verdict=_band(ot.band, ot.words.lower()),
            verdict_heading="How it ended" if f.outcome in OUTCOME_WORDS else "On track",
            verdict_reason=ot.reason, signals=signals, norms_line=_norms_line(norms), elapsed=elapsed(f, now),
            attempts=attempts, waits=waits, issues=tuple(issue_row(i, now) for i in own),
            activity=activity_rows(factory, f.builds, now, limit=20), spend=book.feature_slot(f),
            time=time_slot(feature_time(f, now)), cost_by_stage=_stage_rows(conn, f, book),
        )
        return page_chrome(conn, now), page
    finally:
        if owned:
            conn.close()


def _build_words(b: Build) -> str:
    words = {"QUEUED": "waiting to start", "PREPARING": "preparing", "RUNNING": "building",
             "PAUSED": "waiting for a person", "FINALISING": "finishing", "COMPLETE": "built",
             "FAILED": "failed", "CANCELLED": "cancelled", "INTERRUPTED": "interrupted", "SKIPPED": "skipped"}
    text = words.get(b.status, b.status.lower())
    if b.delivered_at():
        text += "; merged and checked"
    elif b.landed_on_mission_branch_at():
        text += "; joined onto the mission's branch"
    elif b.merge_check_failed_at():
        text += "; merge check failed"
    else:
        d = b.merge_decision()
        if d is not None and d.status == "SKIPPED":
            text += "; not merged"
    return text


def _mission_words(status: str) -> str:
    return {"PROPOSED": "proposed", "REJECTED": "rejected", "NOT_STARTED": "not started", "STARTING": "starting",
            "RUNNING": "running", "READY": "ready for its merge", "MERGING": "merging", "MERGED": "merged",
            "NOT_MERGED": "not merged"}.get(status, status.lower())


def project_view(db_path: Path, project: str, now: datetime | None = None, *,
                 conn: sqlite3.Connection | None = None) -> ProjectView:
    now = now or _now()
    owned = conn is None
    conn = _open(db_path) if conn is None else conn
    try:
        factory, norms = load_factory(conn, now), load_norms()
        fr = freshness(conn)
        book = load_cost_book(conn, now)
        mine = [f for f in factory.features if f.project == project]
        cut = now - timedelta(days=30)
        shown = [f for f in mine if f.in_progress or (f.sent_at and f.sent_at >= cut)]
        rows = tuple(feature_row(f, norms, now, book) for f in shown)
        panel = FeaturesPanel(panel_id="project", title="Features", state=_state(fr, now, len(rows)),
                              as_of=_read_chip(fr, now), rows=rows, empty_copy="No features in the last 30 days.",
                              note=f"Sent in the last 30 days, and anything still in progress ({len(rows)} of "
                                   f"{len(mine)} on record).")
        delivered = [f for f in mine if f.outcome is Outcome.DELIVERED]
        week = now - timedelta(days=7)
        open_issues, _closed, hidden = recent(
            [i for i in find_issues(factory, norms, now) if i.project == project], now)
        return ProjectView(
            chrome=page_chrome(conn, now), project=project, features=panel,
            delivered_7d=sum(1 for f in delivered if f.delivered_at and f.delivered_at >= week),
            delivered_total=len(delivered), in_progress=sum(1 for f in mine if f.in_progress),
            issues=tuple(issue_row(i, now) for i in open_issues), spend=project_spend(book, factory, project),
            open_issues_total=len(open_issues) + hidden, hidden_line=hidden_line(hidden),
            time=sum_time(feature_time(f, now) for f in mine),
            weeks=project_weeks(conn, book, factory, project, now),
            spend_week=project_spend(book, factory, project, lo=cost.week_start(now)),
            untagged_note=_untagged_note(conn, book, now),
        )
    finally:
        if owned:
            conn.close()


def _project_tags(book: CostBook, factory: Factory, project: str) -> set[str]:
    """Tags of the project's features sent since tagging started, and of its missions."""
    tags: set[str] = set()
    for f in factory.features:
        if f.project == project and not book.before_tagging(f):
            tags |= cost.feature_tags(f)
    for m in factory.missions:
        if m.target_repo == project:
            tags |= cost.mission_tags(m)
    return tags


def _has_work_since_tagging(book: CostBook, factory: Factory, project: str) -> bool:
    """Whether the project has any feature sent, or mission begun, since calls were tagged."""
    start = book.tagging_started()
    return (any(f.project == project and not book.before_tagging(f) for f in factory.features)
            or any(m.target_repo == project and m.created_at is not None and m.created_at >= start
                   for m in factory.missions))


def _before_tagging_slot(book: CostBook) -> SpendSlot:
    return SpendSlot(state="not_measured", label="Not measured", price_label=book.prices.label,
                     reason=f"Calls before {cost._day(book.tagging_started())} were not tagged by feature.")


def tagged_spend(book: CostBook, factory: Factory, projects: Iterable[str], *, lo: datetime | None = None,
                 hi: datetime | None = None) -> SpendSlot:
    """Tagged calls of some projects, optionally only those that started in [lo, hi).

    "Not measured" (never "no model calls") when the window ended before tagging began, or when none
    of the projects has had a feature or mission since then: their calls were never tagged."""
    names = tuple(projects)
    if book.state == "never":
        return book.not_available()
    if not names:
        return SpendSlot(state="not_measured", label="Not measured", price_label=book.prices.label,
                         reason="This covers no projects.")
    if not any(f.project in names for f in factory.features) and not any(
            m.target_repo in names for m in factory.missions):
        return SpendSlot(state="not_measured", label="Not measured", price_label=book.prices.label,
                         reason="No work on record for this.")
    if (hi is not None and hi <= book.tagging_started()) or not any(
            _has_work_since_tagging(book, factory, p) for p in names):
        return _before_tagging_slot(book)
    tags: set[str] = set()
    for p in names:
        tags |= _project_tags(book, factory, p)
    hours = book.tag_hours(tags)
    t = cost._sum(v for h, v in hours.items()
                  if (lo is None or cost.hour_to_dt(h) >= lo) and (hi is None or cost.hour_to_dt(h) < hi))
    slot = book.slot(t, scope="project")
    if slot.state == "measured" and (lo is None or lo < book.tagging_started()):
        counts = f"Counts calls from {cost._day(book.tagging_started())}, when calls began to be tagged by feature."
        slot = replace(slot, reason=f"{counts} {slot.reason}".strip())
    return slot


def project_spend(book: CostBook, factory: Factory, project: str, *, lo: datetime | None = None,
                  hi: datetime | None = None) -> SpendSlot:
    """Tagged calls of one project, optionally only those that started in [lo, hi)."""
    return tagged_spend(book, factory, (project,), lo=lo, hi=hi)


#: How many weeks a project page lists, newest first.
PROJECT_WEEKS = 8


def project_weeks(conn: sqlite3.Connection, book: CostBook, factory: Factory, project: str,
                  now: datetime) -> tuple[WeekRow, ...]:
    mine = [f for f in factory.features if f.project == project]
    this_week = cost.week_start(now)
    rows = []
    for n in range(PROJECT_WEEKS):
        lo = cost.week_start(this_week - timedelta(days=7 * n) + timedelta(hours=12))
        hi = cost.week_start(lo + timedelta(days=7, hours=12))
        sent = [f for f in mine if f.sent_at and lo <= f.sent_at < hi]
        delivered = sum(1 for f in mine if f.delivered_at and lo <= f.delivered_at < hi)
        all_calls = cost.window_totals(conn, lo, hi)
        tagged = cost.window_totals(conn, lo, hi, tagged_only=True)
        untagged = cost.Totals(all_calls.calls - tagged.calls, all_calls.failed - tagged.failed,
                               all_calls.tokens_in - tagged.tokens_in, all_calls.tokens_out - tagged.tokens_out,
                               all_calls.cost - tagged.cost, all_calls.estimated - tagged.estimated,
                               all_calls.unpriced - tagged.unpriced)
        local = vm.to_london(lo)
        rows.append(WeekRow(
            week_from=local.date().isoformat(), week_label=f"{local.day} {local.strftime('%b')}", sent=len(sent),
            delivered=delivered, spend=project_spend(book, factory, project, lo=lo, hi=hi),
            time=sum_time(feature_time(f, now) for f in sent),
            untagged=book.slot(untagged, scope="factory") if book.state != "never" else None,
        ))
    return tuple(rows)


def _untagged_note(conn: sqlite3.Connection, book: CostBook, now: datetime) -> str:
    """One sentence on this week's factory-wide calls that carry no feature tag."""
    if book.state == "never":
        return ""
    lo, hi = cost.week_start(now), now + timedelta(seconds=1)
    every = cost.window_totals(conn, lo, hi)
    tagged = cost.window_totals(conn, lo, hi, tagged_only=True)
    n = every.calls - tagged.calls
    if not every.calls:
        return "No model calls were recorded this week."
    noun = "call" if every.calls == 1 else "calls"
    verb, pronoun = ("was", "it counts") if n == 1 else ("were", "they count")
    return (f"This week {n} of the factory's {every.calls} model {noun} {verb} not tagged to a feature, so "
            f"{pronoun} for no project.")


def hidden_line(hidden: int) -> str:
    """One sentence for the open failures the 14-day cut-off leaves out of a list ("" for none)."""
    if not hidden:
        return ""
    noun, verb = ("failure", "is") if hidden == 1 else ("failures", "are")
    return f"{hidden} older {noun} with no later attempt {verb} not listed (opened more than 14 days ago)."


def projects_view(db_path: Path, now: datetime | None = None) -> ProjectsView:
    """One row per project on record, with the same counts its project page shows."""
    now = now or _now()
    conn = _open(db_path)
    try:
        factory, norms = load_factory(conn, now), load_norms()
        book = load_cost_book(conn, now)
        issues = find_issues(factory, norms, now)
        names = sorted({f.project for f in factory.features if f.project}
                       | {m.target_repo for m in factory.missions if m.target_repo})
        rows = []
        for name in names:
            mine = [f for f in factory.features if f.project == name]
            open_, _closed, hidden = recent([i for i in issues if i.project == name], now)
            rows.append(ProjectRow(
                project=name, link=project_link(name), features=len(mine),
                in_progress=sum(1 for f in mine if f.in_progress),
                delivered=sum(1 for f in mine if f.outcome is Outcome.DELIVERED),
                open_issues=len(open_) + hidden, spend=project_spend(book, factory, name),
                time=sum_time(feature_time(f, now) for f in mine),
            ))
        return ProjectsView(chrome=page_chrome(conn, now), rows=tuple(rows), as_of=_read_chip(freshness(conn), now))
    finally:
        conn.close()


def issues_view(db_path: Path, now: datetime | None = None, *, conn: sqlite3.Connection | None = None) -> IssuesView:
    now = now or _now()
    owned = conn is None
    conn = _open(db_path) if conn is None else conn
    try:
        factory, norms = load_factory(conn, now), load_norms()
        open_, closed, hidden = recent(find_issues(factory, norms, now), now)
        note = ("Open: anything waiting for a person or stalled now, and failures from the last 14 days with no "
                "later attempt. Closed: issues that closed in the last 7 days.")
        return IssuesView(chrome=page_chrome(conn, now), open_rows=tuple(issue_row(i, now) for i in open_),
                          closed_rows=tuple(issue_row(i, now) for i in closed), note=note,
                          hidden_line=hidden_line(hidden), hidden_count=hidden)
    finally:
        if owned:
            conn.close()


def delivered_view(db_path: Path, window: tuple[str, str] | None = None, now: datetime | None = None, *,
                   conn: sqlite3.Connection | None = None) -> DeliveredView:
    now = now or _now()
    owned = conn is None
    conn = _open(db_path) if conn is None else conn
    try:
        return DeliveredView(chrome=page_chrome(conn, now), panel=delivered_panel(conn, now, window=window))
    finally:
        if owned:
            conn.close()


def chrome_view(db_path: Path, now: datetime | None = None, *, conn: sqlite3.Connection | None = None) -> PageChrome:
    owned = conn is None
    conn = _open(db_path) if conn is None else conn
    try:
        return page_chrome(conn, now or _now())
    finally:
        if owned:
            conn.close()


def fleet_view(db_path: Path, now: datetime | None = None) -> FleetView:
    now = now or _now()
    conn = _open(db_path)
    try:
        return FleetView(chrome=page_chrome(conn, now), serving=serving(conn, now), feed=feed_panel(conn, now))
    finally:
        conn.close()


# --- weekly report -----------------------------------------------------------------------------------
#
# `weekly_report(tenant, window)` is the one composition the /reports page renders and the chat will
# call later. Plan vs actual comes from plans.yaml (mirrored into plan_milestones); until a plan is
# written there, every piece of work shows "no plan agreed", never green. If the factory's record
# is stale the counts and plan colours are withheld rather than shown from old data.

_TARGET_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


OPERATOR_TENANT = "operator"


def _tenant_projects(conn: sqlite3.Connection, tenant: str) -> Scope:
    """The projects a report covers. Only the operator's own view covers every project (None).
    A client tenant covers exactly the projects configured for it: when they are missing, empty
    or unreadable that is NO project, never every project, so one client never sees another's."""
    if tenant == OPERATOR_TENANT:
        return None
    row = conn.execute("SELECT projects_json FROM tenants WHERE tenant_slug=?", (tenant,)).fetchone()
    if not row or not row[0]:
        return ()
    try:
        data = json.loads(row[0])
    except (ValueError, TypeError):
        return ()
    if not isinstance(data, list):
        return ()
    return tuple(p for p in data if isinstance(p, str) and p)


def _tenant_display(conn: sqlite3.Connection, tenant: str) -> str:
    row = conn.execute("SELECT display_name FROM tenants WHERE tenant_slug=?", (tenant,)).fetchone()
    return str(row[0]) if row and row[0] else tenant


def _target_start(target_window: str) -> date | None:
    m = _TARGET_DATE_RE.search(target_window or "")
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _deviation(*, tracked: set[str], delivered: dict[str, datetime], target_start: date | None,
               ref: date) -> tuple[str, BandChip]:
    """Ahead (all delivered before the target began), on target, or behind (target reached, not all
    delivered). Computed here, never stored."""
    if tracked and tracked <= set(delivered):
        latest = vm.to_london(max(delivered[f] for f in tracked)).date()
        if target_start and latest < target_start:
            return "ahead", BandChip("G", "ahead")
        return "on_target", BandChip("G", "on target")
    if target_start and target_start <= ref:
        return "behind", BandChip("R", "behind")
    return "on_target", BandChip("G", "on target")


def _plan_rows(
    conn: sqlite3.Connection, tenant: str, factory: Factory, projects: Scope,
    active: list[Feature], ref: date,
) -> tuple[list[MilestoneRow], int, int, int]:
    delivered = {f.code: f.delivered_at for f in factory.features
                 if f.delivered_at and f.code and in_scope(f.project, projects)}
    milestones = conn.execute(
        "SELECT milestone_id, title, feature_ids_json, project, target_window FROM plan_milestones "
        "WHERE tenant=? ORDER BY milestone_id", (tenant,),
    ).fetchall()
    rows: list[MilestoneRow] = []
    covered: set[str] = set()
    behind = on_target = ahead = 0
    if projects == ():
        milestones = []          # a client with no projects in scope has no plan to show
    on_record = {f.code: f for f in factory.features if f.code}
    for mid, title, ids_json, project, target_window in milestones:
        if project and not in_scope(str(project), projects):
            continue             # a milestone of a project this view does not cover
        try:
            ids = {str(x) for x in json.loads(ids_json or "[]")}
        except (ValueError, TypeError):
            ids = set()
        # A feature on record outside the scope is dropped. A code with no record yet (planned, not
        # started) carries nothing from any project, so it stays: it is this tenant's own plan.
        mine = {c for c in ids if c in on_record and in_scope(on_record[c].project, projects)}
        if not project and projects is not None and not mine:
            continue             # no project of its own and none of its features in scope
        tracked = ({c for c in ids if c not in on_record or c in mine} if ids
                   else {f.code for f in factory.features
                         if project and f.project == project and f.code and in_scope(f.project, projects)})
        covered |= tracked
        dev, band = _deviation(tracked=tracked, delivered={k: v for k, v in delivered.items() if v},
                               target_start=_target_start(target_window), ref=ref)
        behind += dev == "behind"
        ahead += dev == "ahead"
        on_target += dev == "on_target"
        done = sum(1 for c in tracked if c in delivered)
        rows.append(MilestoneRow(milestone_id=str(mid), title=str(title), target_window=str(target_window),
                                 band=band, deviation=dev,
                                 detail=f"target {target_window}: {done} of {len(tracked)} features "
                                        "merged and checked"))
    for f in active:
        if f.code and f.code in covered:
            continue
        rows.append(MilestoneRow(milestone_id="", title=f.label, target_window="", band=BandChip("—", "no plan agreed"),
                                 deviation="no_baseline", detail="not in an agreed plan",
                                 feature_id=f.code or f.key, feature_link=f.link))
    return rows, behind, on_target, ahead


def _report_issues(factory: Factory, norms: Norms, lo: datetime, hi: datetime, now: datetime,
                   projects: Scope) -> IssuesEncountered:
    opened = closed = 0
    oldest = 0.0
    rows: list[ReportIssueRow] = []
    for i in find_issues(factory, norms, now):
        if not in_scope(i.project, projects):
            continue
        o_in, c_in = _in_window(i.opened_at, lo, hi), _in_window(i.closed_at, lo, hi)
        if not (o_in or c_in):
            continue
        opened += o_in
        closed += c_in
        if i.is_open:
            oldest = max(oldest, (now - i.opened_at).total_seconds())
        rows.append(ReportIssueRow(kind=i.kind, band=_issue_band(i.band), headline=f"{i.kind}: {i.subject}",
                                   feature_id=i.feature.code if i.feature else "", scope_link=i.link,
                                   is_open=i.is_open))
    rows.sort(key=lambda r: (0 if (r.is_open and r.band.band == "R") else 1 if r.is_open else 2))
    return IssuesEncountered(opened=opened, closed=closed, oldest_open_age=vm.fmt_age(oldest) if oldest else "",
                             rows=tuple(rows))


def _report_parts(conn: sqlite3.Connection, tenant: str, now: datetime, window: tuple[str, str] | None):  # type: ignore[no-untyped-def]
    win_from, win_to = parse_window(window, now)
    lo, hi = _window_bounds(win_from, win_to)
    ref = min(date.fromisoformat(win_to), vm.to_london(now).date())
    projects = _tenant_projects(conn, tenant)
    factory, norms = load_factory(conn, now), load_norms()
    delivered = delivered_in(factory, lo, hi, projects)
    in_progress = [f for f in factory.features if f.in_progress and in_scope(f.project, projects)]
    stale = not freshness(conn).fresh(now)
    fr = freshness(conn)
    since = vm.fmt_wall(fr.read_at, now=now) if fr.read_at else "the dashboard started"
    plan_rows, behind, on_target, ahead = _plan_rows(conn, tenant, factory, projects, in_progress + delivered, ref)
    return (win_from, win_to, lo, hi, projects, factory, norms, delivered, in_progress, stale, since,
            plan_rows, behind, on_target, ahead)


def weekly_report(
    conn: sqlite3.Connection, tenant: str, now: datetime | None = None, *,
    window: tuple[str, str] | None = None, client_tenants: tuple[tuple[str, str], ...] = (),
) -> WeeklyReport:
    now = now or _now()
    (win_from, win_to, lo, hi, projects, factory, norms, delivered, in_progress, stale, since,
     plan_rows, behind, on_target, ahead) = _report_parts(conn, tenant, now, window)
    book = load_cost_book(conn, now)
    # The operator's report (projects is None) covers every project; a client's covers exactly its own
    # projects, and an empty set means none (main's client-scope rule), never every project.
    every = ({f.project for f in factory.features if f.project}
             | {m.target_repo for m in factory.missions if m.target_repo})
    names = projects if projects is not None else tuple(sorted(every))
    spend = tagged_spend(book, factory, names, lo=lo, hi=hi)
    headline = ReportHeadline(
        delivered_count=len(delivered), in_flight_count=len(in_progress), behind_count=behind,
        on_target_count=on_target, ahead_count=ahead, spend=spend, withheld=stale, withheld_since=since,
        waiting=sum_time(feature_time(f, now) for f in delivered),
        factory_usage=book.slot(cost.window_totals(conn, lo, min(hi, now + timedelta(seconds=1))), scope="factory"),
    )
    return WeeklyReport(
        chrome=page_chrome(conn, now), tenant_slug=tenant, tenant_display=_tenant_display(conn, tenant),
        window_from=win_from, window_to=win_to, headline=headline,
        plan=PlanVsActual(milestones=tuple(plan_rows), withheld=stale, withheld_since=since),
        delivered_rows=tuple(delivered_row(f, book, now) for f in delivered),
        in_flight_rows=tuple(feature_row(f, norms, now, book) for f in in_progress),
        issues=_report_issues(factory, norms, lo, hi, now, projects), client_tenants=client_tenants,
    )


def export_report(
    conn: sqlite3.Connection, tenant: str, now: datetime | None = None, *, window: tuple[str, str] | None = None,
) -> ExportReport:
    """The client-safe export: delivered features (title, project, date, commit) and the four
    sanctioned plan fields. No issues, no work in progress, no internals, and no costs (Rich's
    approved brief for part B; the design's §8 sentence about adding spend to it is superseded)."""
    now = now or _now()
    (win_from, win_to, _lo, _hi, _projects, _factory, _norms, delivered, _in_progress, stale, since,
     plan_rows, _b, _o, _a) = _report_parts(conn, tenant, now, window)
    return ExportReport(
        tenant_display=_tenant_display(conn, tenant), window_from=win_from, window_to=win_to,
        as_of=_read_chip(freshness(conn), now),
        delivered_rows=tuple(ExportDeliveredRow(feature_id=f.code, title=f.title, project=f.project,
                                                delivered_date=vm.fmt_day(f.delivered_at) if f.delivered_at else "",
                                                commit=_commit(f)) for f in delivered),
        milestones=tuple(ExportMilestone(m.milestone_id, m.title, m.target_window, m.band)
                         for m in plan_rows if m.deviation != "no_baseline"),
        milestones_withheld=stale, withheld_since=since,
    )


def _client_tenants(conn: sqlite3.Connection) -> tuple[tuple[str, str], ...]:
    rows = conn.execute(
        "SELECT tenant_slug, display_name FROM tenants WHERE tenant_slug != 'operator' ORDER BY tenant_slug"
    ).fetchall()
    return tuple((str(s), str(d or s)) for s, d in rows)


def weekly_report_view(db_path: Path, tenant: str, window: tuple[str, str] | None = None,
                       now: datetime | None = None, *, conn: sqlite3.Connection | None = None) -> WeeklyReport:
    now = now or _now()
    owned = conn is None
    conn = _open(db_path) if conn is None else conn
    try:
        return weekly_report(conn, tenant, now, window=window, client_tenants=_client_tenants(conn))
    finally:
        if owned:
            conn.close()


def export_report_view(db_path: Path, tenant: str, window: tuple[str, str] | None = None,
                       now: datetime | None = None) -> ExportReport:
    now = now or _now()
    conn = _open(db_path)
    try:
        return export_report(conn, tenant, now, window=window)
    finally:
        conn.close()


# --- live-update fragments ------------------------------------------------------------------------


def panel_view(  # noqa: PLR0911
    db_path: Path, panel_id: str, now: datetime | None = None, *, window: tuple[str, str] | None = None,
) -> vm.Panel:
    """One page section, rebuilt for a live update (`/fragments/{panel}`)."""
    now = now or _now()
    conn = _open(db_path)
    try:
        if panel_id == "p6":
            return serving(conn, now)
        if panel_id == "proj":
            return feed_panel(conn, now)
        factory, norms = load_factory(conn, now), load_norms()
        if panel_id == "p2":
            return in_flight(conn, now, factory, norms)
        if panel_id == "p3":
            return recent_activity(conn, now, factory)
        if panel_id == "p4":
            return needs_you(conn, now, factory, norms)
        if panel_id == "p5":
            return planning(conn, now, factory)
        if panel_id == "p7":
            return delivered_panel(conn, now, factory, norms, window=window)
        raise KeyError(panel_id)
    finally:
        conn.close()


PANELS: tuple[str, ...] = ("p2", "p3", "p4", "p5", "p6", "p7", "proj")


def health(db_path: Path, now: datetime | None = None) -> dict[str, object]:
    """For the start script and the release runbook: is the dashboard reading the factory's record?"""
    now = now or _now()
    conn = _open(db_path)
    try:
        fr = freshness(conn)
        cf = cost.cost_freshness(conn)
        return {
            "factory_record": "fresh" if fr.fresh(now) else ("never read" if fr.read_at is None else "stale"),
            "feed": "running" if fr.feed_alive(now) else "stopped",
            "last_read_at": fr.read_at.isoformat() if fr.read_at else None,
            # LiteLLM's spend log, apart from Forge's record. No addresses or credentials.
            "cost_feed": {"never": "never read", "fresh": "fresh", "stale": "stale"}[cf.state(now)],
            "cost_last_read_at": cf.read_at.isoformat() if cf.read_at else None,
        }
    finally:
        conn.close()


# --- read functions for the chat (part D): the same figures the pages show, as plain data -----------


def cost_summary(db_path: Path, *, feature: str | None = None, project: str | None = None,
                 lo: datetime | None = None, hi: datetime | None = None,
                 now: datetime | None = None, conn: sqlite3.Connection | None = None) -> cost.CostSummary:
    """Tagged model calls, tokens and nominal cost for one feature (code, request id or title) or one
    project, optionally only calls that started in [lo, hi). For the chat: pass the answer's
    snapshot as ``conn=`` (`dbread.snapshot`) so it agrees with the other views of the answer."""
    now = now or datetime.now(UTC)
    owned = conn is None
    conn = _open(db_path) if conn is None else conn
    try:
        factory, book = load_factory(conn, now), load_cost_book(conn, now)
        scope, name, chosen = cost.scope(factory, feature, project)
        measured = [f for f in chosen if not book.before_tagging(f)]
        tags: set[str] = set().union(*(cost.feature_tags(f) for f in measured)) if measured else set()
        if scope == "project":
            for m in factory.missions:
                if m.target_repo == name:
                    tags |= cost.mission_tags(m)
        hours = book.tag_hours(tags)
        t = cost._sum(v for h, v in hours.items()
                 if (lo is None or cost.hour_to_dt(h) >= lo) and (hi is None or cost.hour_to_dt(h) < hi))
        slot = book.slot(t, scope=scope) if (measured or scope == "project") else (
            book.feature_slot(chosen[0]) if chosen else SpendSlot(state="not_measured", reason="No such record."))
        return cost.CostSummary(
            scope=scope, name=name, period=cost.period(lo, hi), state=slot.state, reason=slot.reason,
            calls=t.calls, tokens_in=t.tokens_in, tokens_out=t.tokens_out,
            cost_usd=slot.amount_usd if slot.state == "measured" else None, estimated=slot.estimated,
            stale=slot.stale, price_label=book.prices.label, refs=tuple(f.code or f.key for f in chosen),
        )
    finally:
        if owned:
            conn.close()


def time_summary(db_path: Path, *, feature: str | None = None, project: str | None = None,
                 lo: datetime | None = None, hi: datetime | None = None,
                 now: datetime | None = None, conn: sqlite3.Connection | None = None) -> cost.TimeSummary:
    """Elapsed, machine and waiting time for one feature or the features of one project sent in [lo, hi).
    For the chat: pass the answer's snapshot as ``conn=`` (`dbread.snapshot`)."""
    now = now or datetime.now(UTC)
    owned = conn is None
    conn = _open(db_path) if conn is None else conn
    try:
        factory = load_factory(conn, now)
        scope, name, chosen = cost.scope(factory, feature, project)
        if scope == "project":
            chosen = [f for f in chosen
                      if f.sent_at and (lo is None or f.sent_at >= lo) and (hi is None or f.sent_at < hi)]
        times = [(f, feature_time(f, now)) for f in chosen]
        known: list[tuple[Feature, FeatureTime]] = [(f, t) for f, t in times if t is not None and not t.unmeasured]
        if not chosen:
            slot = TimeSlot(reason="No such record.")
        elif scope == "feature":
            slot = time_slot(times[0][1])
        else:
            slot = sum_time(t for _f, t in times)
        waits: dict[str, float] = {}
        for _f, t in known:
            for p in t.parts:
                if p.kind == "wait":
                    waits[p.label] = waits.get(p.label, 0.0) + p.seconds
        measured = slot.measured
        return cost.TimeSummary(
            scope=scope, name=name, period=cost.period(lo, hi), state=slot.state, reason=slot.reason,
            features=len(known), not_measured_count=slot.not_measured_count,
            elapsed_secs=sum(t.elapsed for _f, t in known) if measured else None,
            machine_secs=sum(t.machine for _f, t in known) if measured else None,
            waiting_secs=sum(t.waiting for _f, t in known) if measured else None,
            waiting_parts=tuple(sorted(waits.items(), key=lambda kv: -kv[1])),
            so_far=any(t.so_far for _f, t in known), refs=tuple(f.code or f.key for f, _t in known),
        )
    finally:
        if owned:
            conn.close()
