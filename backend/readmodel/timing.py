"""Time per feature from Forge's record: elapsed, machine time and waiting time (requirement 6).

Every period comes from a timestamp Forge already records (design of 17 October 2026, §6):

| Period | From → to | Counts as |
|---|---|---|
| In the queue | queue item queued → admitted; planning queued → started; build queued → started | waiting (queue) |
| Planning | planning started → finished, minus the spec-approval waits inside it | machine |
| Build | build started → finished, minus the build-approval waits inside it | machine |
| Merge and check | the merge decision → the last merge step after it | machine |
| Approvals | spec approval and build go-ahead waits | waiting (approvals) |
| Merge decision | merge offer → decision | waiting (merge decision) |
| The mission's merge | joined onto the mission's branch → the mission's merge or close | waiting (mission) |

Waits are the ones `factory.Feature.waits` already paired with Forge's own rule; nothing here pairs
them again. `stage_log.duration_secs` is not used: Forge records 0 for every stage, and a stage row's
start and end times are the same moment.

**A Forge record defect, handled here.** On 17 October 2026 three mission builds had `started_at`
written when they finished (a 2 ms build), although their runner's first stage row shows them
starting at queue time. A build's start is therefore the earlier of `started_at` and its first
`autobuild` stage row. Builds from 18 October are recorded correctly.

Elapsed time ends where the feature ended (factory.Feature.ended_at), or now while it is in
progress, and the figure is then marked "so far". Whatever part of elapsed time the record does not
place in one of the periods above (a few seconds between steps, usually) is kept as "not placed",
so that machine + waiting + not placed = elapsed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.readmodel.factory import _BUILD_ACTIVE, Build, Feature, Wait, machine_seconds, planning_seconds


@dataclass(frozen=True)
class Part:
    key: str          # planning | building | merge_check | queue | approvals | merge_decision | mission_merge |
                      # not_placed
    label: str
    kind: str         # 'machine' | 'wait' | 'none'
    seconds: float
    open: bool = False  # still going on now


@dataclass(frozen=True)
class FeatureTime:
    elapsed: float
    so_far: bool
    parts: tuple[Part, ...]

    def total(self, kind: str) -> float:
        return sum(p.seconds for p in self.parts if p.kind == kind)

    @property
    def machine(self) -> float:
        return self.total("machine")

    @property
    def waiting(self) -> float:
        return self.total("wait")

    def part(self, key: str) -> float:
        return sum(p.seconds for p in self.parts if p.key == key)


LABELS = {
    "planning": ("Planning", "machine"),
    "building": ("Building", "machine"),
    "merge_check": ("Merge and check", "machine"),
    "queue": ("In the queue", "wait"),
    "approvals": ("Waiting for approvals", "wait"),
    "merge_decision": ("Waiting for the merge decision", "wait"),
    "mission_merge": ("Waiting for the mission's merge", "wait"),
    "not_placed": ("Between steps", "none"),
}
ORDER = tuple(LABELS)


def build_start(b: Build) -> datetime | None:
    """When the build started: `started_at`, or its runner's first stage row if that is earlier."""
    firsts = [s.at for s in b.stages if s.stage_label == "autobuild"]
    candidates = [t for t in (b.started_at, min(firsts) if firsts else None) if t]
    return min(candidates) if candidates else None


def _build_end(b: Build, now: datetime) -> datetime | None:
    if b.status in _BUILD_ACTIVE:
        return None
    if b.completed_at:
        return b.completed_at
    return max((s.at for s in b.stages), default=None) or b.started_at or now


def _span(start: datetime | None, end: datetime | None, lo: datetime, hi: datetime) -> float:
    """Seconds of [start, end) inside [lo, hi]; an open end runs to hi."""
    if start is None:
        return 0.0
    a, z = max(start, lo), min(end or hi, hi)
    return max(0.0, (z - a).total_seconds())


def _merge_check(b: Build, stop: datetime) -> tuple[float, bool]:
    """From the approving merge decision to the last merge step after it (machine time)."""
    decision = b.merge_decision()
    if decision is None or decision.status != "PASSED":
        return 0.0, False
    after = [s.at for s in b.stages if s.stage_label == "merge-deploy" and s.at >= decision.at]
    landed = b.delivered_at() or b.landed_on_mission_branch_at() or b.merge_check_failed_at()
    if landed is None and b.status not in ("FAILED", "CANCELLED", "INTERRUPTED"):
        return _span(decision.at, None, decision.at, stop), True  # merging now
    end = max(after) if after else decision.at
    return _span(decision.at, end, decision.at, stop), False


def _waits(waits: tuple[Wait, ...], kinds: tuple[str, ...], lo: datetime, hi: datetime) -> tuple[float, bool]:
    chosen = [w for w in waits if w.kind in kinds]
    return sum(_span(w.started_at, w.ended_at, lo, hi) for w in chosen), any(w.open for w in chosen)


def feature_time(f: Feature, now: datetime) -> FeatureTime | None:
    """The time breakdown for one feature, or None when the record has no time it was sent."""
    if f.sent_at is None:
        return None
    sent = f.sent_at
    end = None if f.in_progress else f.ended_at
    stop = end or now
    stop = max(stop, sent)
    elapsed = (stop - sent).total_seconds()
    secs: dict[str, float] = dict.fromkeys(ORDER, 0.0)
    open_: dict[str, bool] = dict.fromkeys(ORDER, False)

    # Waiting in the queue: the work queue, then planning's own queue, then each build's.
    q = f.queue
    if q is not None:
        left = q.admitted_at or (q.closed_at if q.status != "QUEUED" else None)
        secs["queue"] += _span(q.queued_at, left, sent, stop)
        open_["queue"] |= left is None and f.in_progress
    run = f.planning
    if run is not None and run.queued_at is not None:
        started = run.started_at or (None if run.state == "QUEUED" else run.completed_at)
        secs["queue"] += _span(run.queued_at, started, sent, stop)
        open_["queue"] |= started is None and f.in_progress
    for b in f.builds:
        started = build_start(b) or (None if b.status in ("QUEUED", "PREPARING") else b.completed_at)
        secs["queue"] += _span(b.queued_at, started, sent, stop)
        open_["queue"] |= started is None and b.status in ("QUEUED", "PREPARING")

    # Machine time.
    if run is not None:
        secs["planning"] = min(planning_seconds(f, stop) or 0.0, elapsed)
        open_["planning"] = run.active and run.state != "QUEUED" and f.open_wait() is None
    gate_waits = [w for w in f.waits if w.kind == "build approval"]
    for b in f.builds:
        b_end = _build_end(b, now)
        b_stop = min(b_end, stop) if b_end else stop
        secs["building"] += machine_seconds(build_start(b), b_stop, gate_waits, stop) or 0.0
        open_["building"] |= b.status in ("RUNNING", "FINALISING") and f.open_wait() is None
        mc, mc_open = _merge_check(b, stop)
        secs["merge_check"] += mc
        open_["merge_check"] |= mc_open

    # Waiting for a person, as Part A paired it.
    secs["approvals"], open_["approvals"] = _waits(f.waits, ("spec approval", "build approval"), sent, stop)
    secs["merge_decision"], open_["merge_decision"] = _waits(f.waits, ("merge decision",), sent, stop)

    # A feature built inside a mission waits on its branch for the mission's own merge.
    if f.mission is not None:
        landed = [t for t in (b.landed_on_mission_branch_at() for b in f.builds) if t]
        if landed:
            m = f.mission
            m_end = m.delivered_at() or m.closed_at
            secs["mission_merge"] = _span(min(landed), m_end, sent, stop)
            open_["mission_merge"] = m_end is None and f.in_progress

    placed = sum(v for k, v in secs.items() if k != "not_placed")
    secs["not_placed"] = max(0.0, elapsed - placed)
    parts = tuple(Part(k, LABELS[k][0], LABELS[k][1], secs[k], open_[k]) for k in ORDER if secs[k] > 0 or open_[k])
    return FeatureTime(elapsed=elapsed, so_far=end is None, parts=parts)
