"""What are the issues: worked out from Forge's record on every read, never stored.

An issue closes when the record moves on: a failure closes when a later attempt at the same
feature starts (a new build of it, or the same request sent again) or the feature is delivered;
a wait closes when the person decides; a stall closes as soon as the run does something again.
Nothing here is written anywhere.

Kinds (design requirement 7):
- planning failed (with Forge's own sentence);
- build failed, build cancelled;
- merge check failed, for a feature or for a mission's own merge into main (a mission's merge is
  never a feature, an attempt or a delivery, but its failure is an issue of the mission);
- waiting for a person (open waits, and closed waits that went past the green threshold);
- stalled: a feature still in progress with nothing recorded for longer than its threshold (a
  working planning run against the planning stall threshold; anything else against the longer
  "nothing recorded" one). A stall never ends a feature; it stays in progress.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backend.readmodel.factory import Build, Factory, Feature, Mission, Outcome, Wait, build_waits, first_sentence
from backend.readmodel.norms import AMBER, GREEN, RED, Norms, quiet_spell

#: Waiting kinds in plain words.
WAIT_WORDS = {
    "spec approval": "Waiting for the spec to be approved",
    "build approval": "Waiting for the go-ahead to build",
    "merge decision": "Waiting for the merge decision",
}


@dataclass(frozen=True)
class Issue:
    kind: str
    band: str                       # G | A | R
    opened_at: datetime
    closed_at: datetime | None
    detail: str
    feature: Feature | None = None
    mission: Mission | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def subject(self) -> str:
        if self.feature is not None:
            return self.feature.label
        if self.mission is not None:
            return f"{self.mission.label} ({self.mission.title})" if self.mission.title else self.mission.label
        return ""

    @property
    def link(self) -> str:
        if self.feature is not None:
            return self.feature.link
        return "/issues"

    @property
    def project(self) -> str:
        if self.feature is not None:
            return self.feature.project
        return self.mission.target_repo if self.mission else ""


def _wait_band(w: Wait, norms: Norms, now: datetime) -> str:
    return norms.waiting_hours.band(w.seconds(now) / 3600)


def _wait_issues(waits: tuple[Wait, ...] | list[Wait], norms: Norms, now: datetime, **subject: object) -> list[Issue]:
    out = []
    for w in waits:
        band = _wait_band(w, norms, now)
        if w.open or band != GREEN:
            detail = f"waited {_hours(w.seconds(now))}" if not w.open else f"waiting {_hours(w.seconds(now))} so far"
            out.append(Issue(WAIT_WORDS[w.kind], band, w.started_at, w.ended_at, detail, **subject))  # type: ignore[arg-type]
    return out


def _hours(seconds: float) -> str:
    m = int(seconds // 60)
    if m >= 48 * 60:
        return f"{m // 1440} days {m % 1440 // 60} h"
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _failure_detail(b: Build) -> str:
    for s in reversed(b.stages):
        if s.stage_label == "merge-deploy" and s.status == "FAILED":
            text = s.details.get("detail") or s.details.get("failed_step") or ""
            return first_sentence(str(text)) if text else "the merge step did not pass"
    return first_sentence(b.error) if b.error else "no reason recorded"


def _build_issues(f: Feature) -> list[Issue]:
    out = []
    for i, b in enumerate(f.builds):
        nxt = f.builds[i + 1].queued_at if i + 1 < len(f.builds) else None
        closed = nxt or f.delivered_at
        merge_failed = b.merge_check_failed_at()
        if merge_failed is not None:
            out.append(Issue("Merge check failed", RED, merge_failed, closed, _failure_detail(b), feature=f))
        elif b.status == "FAILED":
            out.append(Issue("Build failed", RED, b.completed_at or b.queued_at or f.sent_at or _EPOCH,
                             closed, _failure_detail(b), feature=f))
        elif b.status in ("CANCELLED", "INTERRUPTED"):
            when = b.completed_at or b.queued_at or f.sent_at or _EPOCH
            word = "Build cancelled" if b.status == "CANCELLED" else "Build interrupted"
            out.append(Issue(word, AMBER, when, closed, first_sentence(b.error) if b.error else "", feature=f))
    return out


# Forge always records a queue time; this only keeps the types honest.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _resend_times(factory: Factory) -> dict[tuple[str, str], list[datetime]]:
    """When each request sentence was sent, per project: sending the same sentence again is a later
    attempt at the same feature, even though Forge gives it a new request id."""
    out: dict[tuple[str, str], list[datetime]] = {}
    for f in factory.features:
        if f.sent_at and f.request_text:
            out.setdefault((f.project, " ".join(f.request_text.split()).lower()), []).append(f.sent_at)
    for times in out.values():
        times.sort()
    return out


def _resent_after(f: Feature, after: datetime, resends: dict[tuple[str, str], list[datetime]]) -> datetime | None:
    key = (f.project, " ".join(f.request_text.split()).lower())
    return next((t for t in resends.get(key, []) if t > after and t != f.sent_at), None)


def find_issues(factory: Factory, norms: Norms, now: datetime) -> list[Issue]:
    """Every issue the record shows, open or closed, newest first."""
    issues: list[Issue] = []
    resends = _resend_times(factory)
    for f in factory.features:
        for found in _build_issues(f):
            if found.closed_at is None and found.kind != "Build interrupted":
                found = Issue(found.kind, found.band, found.opened_at,  # noqa: PLW2901
                              _resent_after(f, found.opened_at, resends), found.detail, feature=f)
            issues.append(found)
        if f.outcome is Outcome.PLANNING_FAILED and f.planning is not None:
            when = f.planning.completed_at or f.planning.queued_at or now
            issues.append(Issue("Planning failed", RED, when, _resent_after(f, when, resends),
                                first_sentence(f.planning.error) or "no reason recorded", feature=f))
        issues.extend(_wait_issues(f.waits, norms, now, feature=f))
        stall = _stall_issue(f, norms, now)
        if stall is not None:
            issues.append(stall)
    merges: dict[str, list[Build]] = {}
    for b in reversed(factory.builds):  # oldest first
        if b.is_mission_merge and b.mission_id:
            merges.setdefault(b.mission_id, []).append(b)
    for m in factory.missions:
        if m.merge_build is not None:
            issues.extend(_wait_issues(build_waits(m.merge_build, is_latest=True), norms, now, mission=m))
        issues.extend(_mission_merge_failures(m, merges.get(m.id, [])))
    issues.sort(key=lambda i: i.opened_at, reverse=True)
    return issues


def _mission_merge_failures(m: Mission, merges: list[Build]) -> list[Issue]:
    """A failed check of a mission's own merge into main, with Forge's recorded reason. It closes
    when the merge is tried again (a later merge step, or a later merge build for the mission) or
    the mission's merge passes. These rows stay out of every feature, attempt and delivery count."""
    out: list[Issue] = []
    for i, b in enumerate(merges):
        nxt = merges[i + 1].queued_at if i + 1 < len(merges) else None
        steps = [s for s in b.stages if s.stage_label == "merge-deploy"]
        failed = [(j, s) for j, s in enumerate(steps) if s.status == "FAILED"]
        for j, s in failed:
            closed = steps[j + 1].at if j + 1 < len(steps) else nxt
            text = s.details.get("detail") or s.details.get("failed_step") or ""
            detail = first_sentence(str(text)) if text else "the merge step did not pass"
            out.append(Issue("Merge check failed", RED, s.at, closed, detail, mission=m))
        if not failed and b.status == "FAILED":
            when = b.completed_at or b.queued_at or m.created_at or _EPOCH
            detail = first_sentence(b.error) if b.error else "no reason recorded"
            out.append(Issue("Merge check failed", RED, when, nxt, detail, mission=m))
    return out


def _stall_issue(f: Feature, norms: Norms, now: datetime) -> Issue | None:
    q = quiet_spell(f, norms, now)
    if q is None or q.band == GREEN:
        return None
    what = "no planning activity" if q.planning else "nothing recorded"
    return Issue("Stalled", q.band, q.since, None, f"{what} for {_hours(q.seconds)}", feature=f)


def recent(
    issues: list[Issue], now: datetime, *, open_days: int = 14, closed_days: int = 7,
) -> tuple[list[Issue], list[Issue], int]:
    """(open, recently closed, how many old open failures were left out).

    Open failures older than `open_days` with no later attempt are left out, and counted so the page
    can say how many; waits and stalls that are open now are always listed, however old."""
    open_cut, closed_cut = now - timedelta(days=open_days), now - timedelta(days=closed_days)
    live_kinds = set(WAIT_WORDS.values()) | {"Stalled"}
    still_open = [i for i in issues if i.is_open and (i.kind in live_kinds or i.opened_at >= open_cut)]
    hidden = sum(1 for i in issues if i.is_open and i.kind not in live_kinds and i.opened_at < open_cut)
    closed = [i for i in issues if i.closed_at is not None and i.closed_at >= closed_cut]
    still_open.sort(key=lambda i: ({RED: 0, AMBER: 1}.get(i.band, 2), -i.opened_at.timestamp()))
    closed.sort(key=lambda i: i.closed_at or now, reverse=True)
    return still_open, closed, hidden
