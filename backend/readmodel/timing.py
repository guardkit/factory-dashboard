"""Time per feature from Forge's record: elapsed, machine time and waiting time (requirement 6).

Every period comes from a timestamp Forge already records (design of 17 October 2026, §6):

| Period | From → to | Counts as |
|---|---|---|
| In the queue | queue item queued → admitted; planning queued → started; build queued → started | waiting (queue) |
| Planning | planning started → finished | machine |
| Building | build started → finished | machine |
| Merge and check | the merge decision → the last merge step after it | machine |
| Approvals | spec approval and build go-ahead waits | waiting (approvals) |
| Merge decision | merge offer → decision | waiting (merge decision) |
| The mission's merge | joined onto the mission's branch → the mission's merge or close | waiting (mission) |

**Each moment is counted once.** Periods can overlap: a wait for a person lies inside a build, a
fix build's queue can overlap the build before it, and attempts can overlap. So the feature's
elapsed time is cut at every period's start and end, and each piece is counted under the most
specific period that covers it, in this order: approvals, the merge decision, merge and check,
building, planning, the queue, the mission's merge. Whatever no period covers is "between steps".
So machine + waiting + between steps = elapsed, always, and machine time can never exceed it.

Waits are the ones `factory.Feature.waits` already paired with Forge's own rule; nothing here pairs
them again. `stage_log.duration_secs` is not used: Forge records 0 for every stage, and a stage row's
start and end times are the same moment.

**Builds closed long after their last step are not measured** (rule dated 18 October 2026). The
clean-up of 1 October 2026 (07:33 UTC) closed 24 old builds from May to July, writing a start
and completion time of that day, and closed others that had a real start in July. The record then
shows a build that ran for months. A build's runner writes its rows at the start, so a real build
can run for hours with no row near its end: in Forge's record on 18 October the longest such gap
was 7.9 hours, and every clean-up gap was 66 days or more. So: **when a build's completion is more
than 24 hours after its last stage row, the record does not say when it really ended**, and the
feature's time is "not measured", with the reason. The one exception (18 October 2026): when that
last row is a gate pause or a merge offer, the build was waiting for a person, and a long wait for
a person is real time, so it is measured as usual.

**A Forge record defect, handled here.** On 17 October 2026 two mission builds (FEAT-4C9B, FEAT-7C34)
had `started_at` written when they finished (a 2 ms build), although their runner's first stage row
shows them starting at queue time, and a merge-decision row marks their real end. Where a build's
`started_at` is within a second of its completion, and its completion is backed by a stage row (the
24-hour rule above), its start is its runner's first `autobuild` row. Builds from 18 October are
recorded correctly.

Elapsed time ends where the feature ended (factory.Feature.ended_at), or now while it is in
progress, and the figure is then marked "so far".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise

from backend.readmodel.factory import _BUILD_ACTIVE, Build, Feature

#: A build whose completion is this long after its last stage row was closed by a later clean-up.
UNBACKED_END = timedelta(hours=24)
#: How close started_at must be to the completion to be the 17 October defect.
START_WRITTEN_AT_END = timedelta(seconds=1)


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
    unmeasured: str = ""   # why the record cannot say how the time was spent ("" when it can)

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
#: Which period a moment is counted under when several cover it (first wins).
PRECEDENCE = ("approvals", "merge_decision", "merge_check", "building", "planning", "queue", "mission_merge")


def _last_row(b: Build) -> datetime | None:
    return max((s.at for s in b.stages), default=None)


def unbacked_end(b: Build) -> bool:
    """The build's completion is more than 24 hours after its last stage row (or it has a completion
    long after its start and no rows at all): a later clean-up closed it."""
    if b.completed_at is None or b.status in _BUILD_ACTIVE:
        return False
    last = _last_row(b)
    if last is None:
        start = b.started_at or b.queued_at
        return start is not None and b.completed_at - start > UNBACKED_END
    final = max(b.stages, key=lambda s: (s.at, s.id))
    if "gate_pause" in final.details or "merge_offer" in final.details:
        return False   # the build ended waiting for a person: a long wait is real, not a clean-up
    return b.completed_at - last > UNBACKED_END


def build_start(b: Build) -> datetime | None:
    """When the build started: `started_at`, except for the 17 October defect, where `started_at`
    was written at a completion the record backs, and the runner's first row is the real start."""
    if (b.started_at is not None and b.completed_at is not None
            and abs(b.completed_at - b.started_at) <= START_WRITTEN_AT_END and not unbacked_end(b)):
        firsts = [s.at for s in b.stages if s.stage_label == "autobuild"]
        if firsts and min(firsts) < b.started_at:
            return min(firsts)
    return b.started_at


def _build_end(b: Build) -> datetime | None:
    """None while the build is still going."""
    if b.status in _BUILD_ACTIVE:
        return None
    return b.completed_at or _last_row(b) or b.started_at


def _merge_check(b: Build) -> tuple[datetime, datetime | None] | None:
    """From the approving merge decision to the last merge step after it (None: still merging)."""
    decision = b.merge_decision()
    if decision is None or decision.status != "PASSED":
        return None
    after = [s.at for s in b.stages if s.stage_label == "merge-deploy" and s.at >= decision.at]
    landed = b.delivered_at() or b.landed_on_mission_branch_at() or b.merge_check_failed_at()
    if landed is None and b.status not in ("FAILED", "CANCELLED", "INTERRUPTED"):
        return decision.at, None
    return decision.at, max(after) if after else decision.at


Span = tuple[str, datetime, datetime | None, bool]   # (period, start, end or None for "to now", open)


def _spans(f: Feature) -> list[Span]:
    out: list[Span] = []
    q = f.queue
    if q is not None and q.queued_at is not None:
        left = q.admitted_at or (q.closed_at if q.status != "QUEUED" else None)
        out.append(("queue", q.queued_at, left, left is None and f.in_progress))
    run = f.planning
    if run is not None:
        if run.queued_at is not None:
            started = run.started_at or (None if run.state == "QUEUED" else run.completed_at)
            out.append(("queue", run.queued_at, started, started is None and f.in_progress))
        if run.started_at is not None:
            out.append(("planning", run.started_at, run.completed_at,
                        run.active and run.state != "QUEUED" and f.open_wait() is None))
    for b in f.builds:
        start = build_start(b)
        if b.queued_at is not None:
            left = start or (None if b.status in ("QUEUED", "PREPARING") else b.completed_at)
            out.append(("queue", b.queued_at, left, left is None and b.status in ("QUEUED", "PREPARING")))
        if start is not None:
            out.append(("building", start, _build_end(b),
                        b.status in ("RUNNING", "FINALISING") and f.open_wait() is None))
        mc = _merge_check(b)
        if mc is not None:
            out.append(("merge_check", mc[0], mc[1], mc[1] is None))
    for w in f.waits:
        key = "merge_decision" if w.kind == "merge decision" else "approvals"
        out.append((key, w.started_at, w.ended_at, w.open))
    if f.mission is not None:
        landed = [t for t in (b.landed_on_mission_branch_at() for b in f.builds) if t]
        if landed:
            m = f.mission
            m_end = m.delivered_at() or m.closed_at
            out.append(("mission_merge", min(landed), m_end, m_end is None and f.in_progress))
    return out


def _sweep(spans: Iterable[Span], lo: datetime, hi: datetime) -> dict[str, float]:
    """Seconds per period over [lo, hi], each moment counted once under the first period (in
    PRECEDENCE) that covers it."""
    clipped = [(k, max(a, lo), min(z or hi, hi)) for k, a, z, _o in spans]
    clipped = [(k, a, z) for k, a, z in clipped if z > a]
    cuts = sorted({lo, hi, *(a for _k, a, _z in clipped), *(z for _k, _a, z in clipped)})
    rank = {k: i for i, k in enumerate(PRECEDENCE)}
    secs = dict.fromkeys(ORDER, 0.0)
    for a, z in pairwise(cuts):
        covering = [k for k, s, e in clipped if s <= a and e >= z]
        key = min(covering, key=rank.__getitem__) if covering else "not_placed"
        secs[key] += (z - a).total_seconds()
    return secs


def feature_time(f: Feature, now: datetime) -> FeatureTime | None:
    """The time breakdown for one feature, or None when the record has no time it was sent."""
    if f.sent_at is None:
        return None
    sent = f.sent_at
    end = None if f.in_progress else f.ended_at
    stop = max(end or now, sent)
    elapsed = (stop - sent).total_seconds()
    closed_late = [b for b in f.builds if unbacked_end(b)]
    if closed_late:
        b = closed_late[-1]
        last = _last_row(b) or b.started_at or b.queued_at
        assert b.completed_at is not None
        days = (b.completed_at - last).days if last else 0
        when = f"{b.completed_at.day} {b.completed_at.strftime('%B %Y')}"
        reason = (f"Not measured: a build was closed in a clean-up on {when}, {days} days after its last recorded "
                  "step, so the record does not say when it really stopped.")
        return FeatureTime(elapsed=elapsed, so_far=end is None, parts=(), unmeasured=reason)
    spans = _spans(f)
    secs = _sweep(spans, sent, stop)
    open_ = dict.fromkeys(ORDER, False)
    for k, _a, _z, is_open in spans:
        open_[k] |= is_open
    parts = tuple(Part(k, LABELS[k][0], LABELS[k][1], secs[k], open_[k]) for k in ORDER if secs[k] > 0 or open_[k])
    return FeatureTime(elapsed=elapsed, so_far=end is None, parts=parts)
