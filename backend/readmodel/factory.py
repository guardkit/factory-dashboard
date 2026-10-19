"""The factory as Forge's record describes it: features, how far each got, delivery and waits.

Every page and (later) the chat reads features through `load_factory`, so they cannot disagree.
Nothing here writes, and nothing here guesses: a fact the record does not hold is left out and the
page says so.

**What a feature is.** A request sent to the factory. Once it has a build it is known by its
feature code (FEAT-…), and every build with that code is an attempt at it, including the
factory's own fix builds. A request that never reached a build (planning failed, still planning,
stopped in the queue) is known by its request id. A mission's own merge into main
(`mission_role = 'mission-merge'`) is not a feature: it is the mission's merge, and it is never
counted as a feature, an attempt or a delivery.

**Delivered** means the merge step passed: a `merge-deploy` row from the merge executor with status
PASSED, which names the commit that was checked and published (design requirement 6). A feature
built inside a mission is joined onto the mission's branch first; it is delivered when the
mission's own merge passes, and until then it is "on the mission's branch".

**Waits** use the rule Forge itself uses to count a build's waiting for a person
(`seconds_spent_waiting_for_a_person` in Forge's `gating/sqlite_adapters.py`), applied to each kind
of wait: the first of a run of pause (or offer) rows opens a wait, a decision closes it, a
decision with nothing open closes nothing, and a pause after the build was seen working closes
the earlier wait at the last moment it was seen waiting.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

# --- the journey ----------------------------------------------------------------------------

#: How far through, in James's words (design requirement 7).
STATIONS: tuple[str, ...] = (
    "sent",
    "in queue",
    "planning",
    "spec approved",
    "plan approved",
    "building",
    "built",
    "merge decision",
    "merged and checked",
)
SENT, IN_QUEUE, PLANNING, SPEC_APPROVED, PLAN_APPROVED, BUILDING, BUILT, MERGE_DECISION, MERGED = range(9)


class Outcome(StrEnum):
    """Where a feature stands. IN_PROGRESS and ON_MISSION_BRANCH are not finished."""

    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"
    ON_MISSION_BRANCH = "on_mission_branch"
    MISSION_NOT_MERGED = "mission_not_merged"
    NOT_MERGED = "not_merged"
    MERGE_FAILED = "merge_failed"
    BUILT_NO_MERGE = "built_no_merge"
    BUILD_FAILED = "build_failed"
    CANCELLED = "cancelled"
    PLANNING_FAILED = "planning_failed"
    PLANNING_STOPPED = "planning_stopped"
    NOTHING_TO_BUILD = "nothing_to_build"
    HANDED_OFF = "handed_off"
    STOPPED_IN_QUEUE = "stopped_in_queue"
    RETIRED = "retired"


#: Outcomes that are the factory's own failures (they make a feature red and raise an issue).
FAILURES: frozenset[Outcome] = frozenset({Outcome.BUILD_FAILED, Outcome.MERGE_FAILED, Outcome.PLANNING_FAILED})

# Forge's state words.
_BUILD_ACTIVE = ("QUEUED", "PREPARING", "RUNNING", "PAUSED", "FINALISING")
_PLANNING_ACTIVE = ("QUEUED", "RUNNING", "PAUSED", "FEATURE_SPEC", "FEATURE_PLAN")
_SPEC_GATE_LABELS = ("feature-spec-digest-review", "product_docs")
_MISSION_BRANCH_RESULT = "published-to-the-mission-branch"
#: The publication record's results that mean the build's work is on its target branch (Forge's
#: `pipeline/publication_record.py` RESULT_PUBLISHED_DEPLOYMENT_PENDING and RESULT_MERGED_AND_RUNNING;
#: the predicate of `pipeline/mission_finish.py` and the Linear door's `forge_record.ON_MAIN_RESULTS`).
ON_MAIN_RESULTS = frozenset({"published, deployment pending", "merged into the remote and running"})
#: A mission in one of these has ended (Forge's `lifecycle/missions.py` _CLOSED_MISSION_STATUSES).
CLOSED_MISSION_STATUSES = frozenset({"REJECTED", "NOT_STARTED", "MERGED", "NOT_MERGED"})


# --- records ----------------------------------------------------------------------------------


def parse_dt(value: object) -> datetime | None:
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _details(raw: object) -> dict[str, object]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class StageRow:
    id: int
    build_id: str
    stage_label: str
    target: str
    status: str
    at: datetime
    details: dict[str, object]


def _is_decision(s: StageRow) -> bool:
    """A person's (or a mission's) merge word. The merge executor's own row also carries a
    `merge_decision` block, without a `decision`, which is bookkeeping, not a decision."""
    block = s.details.get("merge_decision")
    return isinstance(block, dict) and "decision" in block


@dataclass(frozen=True)
class PlanningEvent:
    id: int
    stage_label: str
    status: str
    at: datetime
    details: dict[str, object]
    actor: str = ""                    # actor_identity: who answered, on a card's verdict row


@dataclass(frozen=True)
class Publication:
    """A build's publication record (Forge's `publication_records`, one row per build): what the
    merge word did. Main is read from here, never from the merge report's wording."""

    result: str
    target_branch: str
    j_commit: str
    decided_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class Build:
    build_id: str
    code: str
    repo: str
    project: str
    status: str
    triggered_by: str
    correlation_id: str
    queued_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    error: str
    mission_id: str
    mission_role: str
    stages: tuple[StageRow, ...]
    pending_request_id: str = ""       # the build gate's card while the build is PAUSED
    target_branch: str = ""            # the branch this work is aimed at, recorded when it started
    publication: Publication | None = None

    @property
    def on_main(self) -> bool:
        """On its target branch by its publication record: the result is one of `ON_MAIN_RESULTS`
        and the record's branch (or the build's, when the record has none) is the build's target
        (or main). The same predicate as Forge's mission finish and the Linear door."""
        p = self.publication
        if p is None or p.result not in ON_MAIN_RESULTS:
            return False
        target = self.target_branch or "main"
        return (p.target_branch or target) == target

    def reached_main_at(self) -> datetime | None:
        """The latest moment its publication record was written (or the build ended), when it is
        on main; None when it is not, or nothing on record says when."""
        if not self.on_main or self.publication is None:
            return None
        times = [t for t in (self.publication.updated_at, self.publication.decided_at, self.completed_at) if t]
        return max(times, default=None)

    @property
    def is_mission_merge(self) -> bool:
        return self.mission_role == "mission-merge"

    @property
    def is_mission_feature(self) -> bool:
        return self.mission_role == "feature"

    def merge_rows(self) -> list[StageRow]:
        return [s for s in self.stages if s.stage_label in ("the merge word", "merge-deploy")]

    def delivered_at(self) -> datetime | None:
        """When this build's merge step passed and published to main (not a mission branch)."""
        if self.is_mission_feature:
            return None
        for s in self.stages:
            if (s.stage_label == "merge-deploy" and s.target == "merge_deploy_executor"
                    and s.status == "PASSED" and s.details.get("result") != _MISSION_BRANCH_RESULT):
                return s.at
        return None

    def landed_on_mission_branch_at(self) -> datetime | None:
        for s in self.stages:
            if (s.stage_label == "merge-deploy" and s.target == "merge_deploy_executor"
                    and s.status == "PASSED" and s.details.get("result") == _MISSION_BRANCH_RESULT):
                return s.at
        return None

    def merge_decision(self) -> StageRow | None:
        """The latest merge decision row (approve is PASSED, decline is SKIPPED)."""
        rows = [s for s in self.stages if _is_decision(s)]
        return rows[-1] if rows else None

    def merge_offered(self) -> bool:
        return any("merge_offer" in s.details for s in self.stages)

    def merge_offer_retired(self) -> bool:
        """The owner retired the open merge offer (Forge's 9 October clean-up of old offers)."""
        return self.merge_retired_at() is not None

    def merge_retired_at(self) -> datetime | None:
        for s in self.stages:
            if "merge_retirement" in s.details:
                return s.at
        return None

    def merge_check_failed_at(self) -> datetime | None:
        """The last merge-deploy row, if it FAILED and nothing passed after it."""
        rows = [s for s in self.stages if s.stage_label == "merge-deploy"]
        if rows and rows[-1].status == "FAILED":
            return rows[-1].at
        return None


@dataclass(frozen=True)
class PlanningRun:
    correlation_id: str
    state: str
    originating_user: str
    expected_approver: str
    request_text: str
    target_repo: str
    queued_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    error: str
    events: tuple[PlanningEvent, ...]
    pending_request_id: str = ""       # the older assumptions checkpoint's card while PAUSED

    @property
    def active(self) -> bool:
        return self.state in _PLANNING_ACTIVE

    def spec_approved(self) -> bool:
        for e in self.events:
            if e.stage_label == "feature-spec-complete":
                return True
            if e.stage_label == "feature-spec-digest-review" and e.status in ("approved", "skipped"):
                return True
        return self.state in ("BUILD_QUEUED", "PLANNED_HANDOFF")

    def retired_at(self) -> datetime | None:
        """When the owner retired this planning run's handover (Forge's administrative clean-up)."""
        for e in self.events:
            if e.stage_label == "planning-handoff-retirement-administrative":
                return e.at
        return None

    def nothing_to_build(self) -> bool:
        return any(e.stage_label == "nothing-to-build" for e in self.events)

    def last_event_at(self) -> datetime | None:
        return self.events[-1].at if self.events else (self.started_at or self.queued_at)


@dataclass(frozen=True)
class QueueItem:
    id: int
    sentence: str
    target_repo: str
    kind: str
    status: str
    correlation_id: str
    queued_at: datetime | None
    admitted_at: datetime | None
    closed_at: datetime | None
    closed_reason: str


@dataclass(frozen=True)
class MissionFeature:
    """One feature of a mission's plan, in Forge's order, with the keys it needs first."""

    key: str
    sentence: str
    needs: tuple[str, ...]
    no_sign_in: bool


@dataclass(frozen=True)
class Mission:
    id: str
    title: str
    target_repo: str
    status: str
    correlation_id: str
    created_at: datetime | None
    closed_at: datetime | None
    closed_reason: str
    merge_build: Build | None
    plan_request_id: str = ""
    originating_user: str = ""
    approved_by: str = ""
    approved_at: datetime | None = None
    plan: tuple[MissionFeature, ...] = ()
    stop_requested_at: datetime | None = None   # a stop was asked for (NULL in a ledger from before stopping)
    stop_requested_by: str = ""                 # an approver identity: never shown as it is

    @property
    def link(self) -> str:
        return f"/missions/{self.id}"

    @property
    def closed(self) -> bool:
        return self.status in CLOSED_MISSION_STATUSES

    @property
    def stopping(self) -> bool:
        """A stop was asked for and the mission has not closed yet: its work is draining."""
        return self.stop_requested_at is not None and not self.closed

    def delivered_at(self) -> datetime | None:
        if self.merge_build is None:
            return None
        for s in self.merge_build.stages:
            if s.stage_label == "merge-deploy" and s.target == "merge_deploy_executor" and s.status == "PASSED":
                return s.at
        return None

    @property
    def label(self) -> str:
        """How `forge status` names a mission's own merge row."""
        return f"mission {self.id}'s merge"


@dataclass(frozen=True)
class Wait:
    """One span of waiting for a person. `ended_at` is None while it is still being waited."""

    kind: str               # 'spec approval' | 'build approval' | 'merge decision'
    started_at: datetime
    ended_at: datetime | None

    @property
    def open(self) -> bool:
        return self.ended_at is None

    def seconds(self, now: datetime) -> float:
        return max(0.0, ((self.ended_at or now) - self.started_at).total_seconds())


@dataclass(frozen=True)
class Feature:
    key: str                          # the feature code, or the request id before any build
    code: str                         # "" before any build
    title: str
    request_text: str
    project: str
    sent_at: datetime | None
    planning: PlanningRun | None
    queue: QueueItem | None
    builds: tuple[Build, ...]          # oldest first; never a mission's own merge
    mission: Mission | None
    station: int                      # index into STATIONS: the current one, or where it ended
    outcome: Outcome
    status_text: str                  # plain words: "Waiting for the merge decision", "Build failed: …"
    ended_at: datetime | None
    delivered_at: datetime | None
    waits: tuple[Wait, ...] = field(default_factory=tuple)

    @property
    def attempts(self) -> int:
        return len(self.builds)

    @property
    def in_progress(self) -> bool:
        """Not finished: still moving, or built inside a mission that has not merged yet."""
        return self.outcome in (Outcome.IN_PROGRESS, Outcome.ON_MISSION_BRANCH)

    @property
    def latest_build(self) -> Build | None:
        return self.builds[-1] if self.builds else None

    @property
    def label(self) -> str:
        """Title first, code after: "Search box that filters notes by title (FEAT-2555)"."""
        if self.code and self.title and self.title != self.code:
            return f"{self.title} ({self.code})"
        return self.title or self.code or self.key

    @property
    def link(self) -> str:
        return f"/features/{self.key}"

    def open_wait(self) -> Wait | None:
        opens = [w for w in self.waits if w.open]
        return opens[-1] if opens else None

    def last_activity_at(self) -> datetime | None:
        return _last_activity(self.planning, self.queue, self.builds) or self.sent_at


@dataclass(frozen=True)
class Factory:
    """Everything the pages read, assembled from one read of the store."""

    features: tuple[Feature, ...]      # newest first
    missions: tuple[Mission, ...]
    builds: tuple[Build, ...]          # every build, mission merges included, newest first

    def feature(self, key: str) -> Feature | None:
        for f in self.features:
            if key in (f.key, f.code) or (f.planning and f.planning.correlation_id == key):
                return f
        return None

    def mission_merges(self) -> tuple[Build, ...]:
        return tuple(b for b in self.builds if b.is_mission_merge)


# --- titles -------------------------------------------------------------------------------------

_TITLE_MAX = 90
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def title_from(text: str, fallback: str) -> str:
    """The request's first sentence, at most 90 characters, cut at a word."""
    text = " ".join((text or "").split())
    if not text:
        return fallback
    first = _SENTENCE_END.split(text, maxsplit=1)[0]
    if len(first) <= _TITLE_MAX:
        return first
    cut = first[: _TITLE_MAX - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(",;: ") + "…"


def first_sentence(text: str, limit: int = 240) -> str:
    """Forge's error text, trimmed for a list: its first sentence, at most `limit` characters."""
    text = " ".join((text or "").split())
    first = _SENTENCE_END.split(text, maxsplit=1)[0]
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


# --- waits: Forge's own pairing rule ----------------------------------------------------------


def _walk(
    rows: Iterable[tuple[datetime, bool, bool, bool]], *, still_waiting: bool, kind: str
) -> list[Wait]:
    """Pair waits from (time, opens, closes, is_gate_row) rows, oldest first.

    The rule of Forge's `seconds_spent_waiting_for_a_person`: only the first of a run of opening
    rows opens a wait; a closing row closes the open wait (and nothing when none is open); an
    opening row after a non-gate row (the record showing work) closes the earlier wait at the
    last moment it was seen waiting and opens a new one. A wait still open at the end runs to now
    when `still_waiting`, otherwise it is closed at the last moment it was seen waiting."""
    waits: list[Wait] = []
    since: datetime | None = None
    last_seen: datetime | None = None
    worked = False
    for at, opens, closes, gated in rows:
        if opens:
            if since is not None and worked:
                waits.append(Wait(kind, since, last_seen or since))
                since = None
            if since is None:
                since = at
            last_seen = at
            worked = False
            continue
        if not gated:
            worked = True
        if closes and since is not None:
            waits.append(Wait(kind, since, at))
            since = None
    if since is not None:
        waits.append(Wait(kind, since, None if still_waiting else (last_seen or since)))
    return waits


def build_waits(build: Build, *, is_latest: bool) -> list[Wait]:
    """Build-gate waits and merge-decision waits for one build."""
    gate = _walk(
        ((s.at, "gate_pause" in s.details,
          "gate_resume" in s.details or (s.status == "SKIPPED" and s.target == "cli-skip"),
          s.status == "GATED") for s in build.stages),
        still_waiting=build.status == "PAUSED",
        kind="build approval",
    )
    merge_closes: Callable[[StageRow], bool] = lambda s: (  # noqa: E731
        _is_decision(s) or "merge_retirement" in s.details
        or (s.stage_label == "the merge word" and s.status == "SKIPPED")
    )
    merge = _walk(
        ((s.at, "merge_offer" in s.details, merge_closes(s), s.status == "GATED") for s in build.stages),
        still_waiting=is_latest and build.status not in ("FAILED", "CANCELLED", "INTERRUPTED", "SKIPPED"),
        kind="merge decision",
    )
    return gate + merge


def planning_waits(run: PlanningRun) -> list[Wait]:
    """Spec approval waits: a GATED (or PAUSED) event for a gate's stage label, closed by the next
    event for that same label with any other status."""
    out: list[Wait] = []
    for label in _SPEC_GATE_LABELS:
        rows = [e for e in run.events if e.stage_label == label]
        if not rows:
            continue
        out.extend(_walk(
            ((e.at, e.status in ("GATED", "PAUSED"), e.status not in ("GATED", "PAUSED"),
              e.status in ("GATED", "PAUSED")) for e in rows),
            still_waiting=run.active,
            kind="spec approval",
        ))
    return out


# --- loading ----------------------------------------------------------------------------------


def _s(v: object) -> str:
    return "" if v is None else str(v)


def _load_builds(conn: sqlite3.Connection) -> list[Build]:
    stages: dict[str, list[StageRow]] = defaultdict(list)
    for r in conn.execute(
        "SELECT id, build_id, stage_label, target_identifier, status, started_at, completed_at, details_json "
        "FROM forge_stage_log ORDER BY id"
    ):
        at = parse_dt(r[5]) or parse_dt(r[6])
        if at is None:
            continue
        stages[r[1]].append(StageRow(int(r[0]), r[1], _s(r[2]), _s(r[3]), _s(r[4]), at, _details(r[7])))
    publications = {
        _s(r[0]): Publication(result=_s(r[1]), target_branch=_s(r[2]), j_commit=_s(r[3]),
                              decided_at=parse_dt(r[4]), updated_at=parse_dt(r[5]))
        for r in conn.execute(
            "SELECT build_id, result, target_branch, j_commit, decided_at, updated_at FROM forge_publication_records"
        )
    }
    builds = []
    for r in conn.execute(
        "SELECT build_id, feature_id, repo, project, status, triggered_by, correlation_id, queued_at, "
        "started_at, completed_at, error, mission_id, mission_role, pending_approval_request_id, target_branch "
        "FROM forge_builds"
    ):
        builds.append(Build(
            build_id=r[0], code=_s(r[1]), repo=_s(r[2]), project=_s(r[3]) or _s(r[2]), status=_s(r[4]),
            triggered_by=_s(r[5]), correlation_id=_s(r[6]), queued_at=parse_dt(r[7]),
            started_at=parse_dt(r[8]), completed_at=parse_dt(r[9]), error=_s(r[10]),
            mission_id=_s(r[11]), mission_role=_s(r[12]), stages=tuple(stages.get(r[0], ())),
            pending_request_id=_s(r[13]), target_branch=_s(r[14]), publication=publications.get(r[0]),
        ))
    builds.sort(key=lambda b: (b.queued_at or datetime.min.replace(tzinfo=UTC), b.build_id))
    return builds


def _load_planning(conn: sqlite3.Connection) -> dict[str, PlanningRun]:
    events: dict[str, list[PlanningEvent]] = defaultdict(list)
    for r in conn.execute(
        "SELECT id, correlation_id, stage_label, status, recorded_at, details_json, actor_identity "
        "FROM forge_planning_events ORDER BY id"
    ):
        at = parse_dt(r[4])
        if at is None:
            continue
        events[r[1]].append(PlanningEvent(int(r[0]), _s(r[2]), _s(r[3]), at, _details(r[5]), _s(r[6])))
    runs = {}
    for r in conn.execute(
        "SELECT correlation_id, state, originating_user, expected_approver, request_text, target_repo, "
        "queued_at, started_at, completed_at, error, pending_approval_request_id FROM forge_planning_runs"
    ):
        runs[r[0]] = PlanningRun(
            correlation_id=r[0], state=_s(r[1]), originating_user=_s(r[2]), expected_approver=_s(r[3]),
            request_text=_s(r[4]), target_repo=_s(r[5]), queued_at=parse_dt(r[6]),
            started_at=parse_dt(r[7]), completed_at=parse_dt(r[8]), error=_s(r[9]),
            events=tuple(events.get(r[0], ())), pending_request_id=_s(r[10]),
        )
    return runs


def _load_queue(conn: sqlite3.Connection) -> dict[str, QueueItem]:
    out = {}
    for r in conn.execute(
        "SELECT id, sentence, target_repo, kind, status, correlation_id, queued_at, admitted_at, closed_at, "
        "closed_reason FROM forge_queue ORDER BY id"
    ):
        out[_s(r[5])] = QueueItem(
            id=int(r[0]), sentence=_s(r[1]), target_repo=_s(r[2]), kind=_s(r[3]), status=_s(r[4]),
            correlation_id=_s(r[5]), queued_at=parse_dt(r[6]), admitted_at=parse_dt(r[7]),
            closed_at=parse_dt(r[8]), closed_reason=_s(r[9]),
        )
    return out


def _load_plans(conn: sqlite3.Connection) -> dict[str, tuple[MissionFeature, ...]]:
    """Each mission's features in Forge's order, with what each needs (Forge's `missions.features`)."""
    needs: dict[tuple[str, str], list[str]] = defaultdict(list)
    for mid, key, needed in conn.execute(
        "SELECT mission_id, key, needs_key FROM forge_mission_needs ORDER BY forge_order, rowid"
    ):
        needs[(_s(mid), _s(key))].append(_s(needed))
    plans: dict[str, list[MissionFeature]] = defaultdict(list)
    for mid, key, sentence, no_sign_in in conn.execute(
        "SELECT mission_id, key, sentence, no_sign_in FROM forge_mission_features ORDER BY forge_order, rowid"
    ):
        plans[_s(mid)].append(MissionFeature(_s(key), _s(sentence), tuple(needs.get((_s(mid), _s(key)), ())),
                                             bool(no_sign_in)))
    return {mid: tuple(rows) for mid, rows in plans.items()}


def _load_missions(conn: sqlite3.Connection, builds: Sequence[Build]) -> dict[str, Mission]:
    merges = {b.mission_id: b for b in builds if b.is_mission_merge and b.mission_id}
    plans = _load_plans(conn)
    out = {}
    for r in conn.execute(
        "SELECT id, title, target_repo, status, correlation_id, created_at, closed_at, closed_reason, "
        "plan_request_id, originating_user, approved_by, approved_at, stop_requested_at, stop_requested_by "
        "FROM forge_missions"
    ):
        out[r[0]] = Mission(
            id=r[0], title=_s(r[1]), target_repo=_s(r[2]), status=_s(r[3]), correlation_id=_s(r[4]),
            created_at=parse_dt(r[5]), closed_at=parse_dt(r[6]), closed_reason=_s(r[7]),
            merge_build=merges.get(r[0]), plan_request_id=_s(r[8]), originating_user=_s(r[9]),
            approved_by=_s(r[10]), approved_at=parse_dt(r[11]), plan=plans.get(r[0], ()),
            stop_requested_at=parse_dt(r[12]), stop_requested_by=_s(r[13]),
        )
    return out


def load_factory(conn: sqlite3.Connection, now: datetime | None = None) -> Factory:
    """Read the copy of Forge's record and assemble every feature (newest first)."""
    now = now or datetime.now(UTC)
    builds = _load_builds(conn)
    planning = _load_planning(conn)
    queue = _load_queue(conn)
    missions = _load_missions(conn, builds)

    by_code: dict[str, list[Build]] = defaultdict(list)
    for b in builds:
        if not b.is_mission_merge and b.code:
            by_code[b.code].append(b)

    features: list[Feature] = []
    used: set[str] = set()
    for code, attempts in by_code.items():
        corrs = [b.correlation_id for b in attempts]
        used.update(corrs)
        run = next((planning[c] for c in corrs if c in planning), None)
        item = next((queue[c] for c in corrs if c in queue), None)
        mission = next((missions[b.mission_id] for b in reversed(attempts) if b.mission_id in missions), None)
        features.append(_assemble(code, code, run, item, tuple(attempts), mission))
    for corr, run in planning.items():
        if corr in used:
            continue
        used.add(corr)
        features.append(_assemble(corr, "", run, queue.get(corr), (), None))
    for corr, item in queue.items():
        if corr in used or item.kind not in ("feature", "fix"):
            continue
        features.append(_assemble(corr, "", None, item, (), None))

    features.sort(key=lambda f: (f.sent_at or datetime.min.replace(tzinfo=UTC)), reverse=True)
    return Factory(
        features=tuple(features),
        missions=tuple(sorted(missions.values(), key=lambda m: m.created_at or now, reverse=True)),
        builds=tuple(reversed(builds)),
    )


# --- assembling one feature ----------------------------------------------------------------------


def _assemble(
    key: str, code: str, run: PlanningRun | None, item: QueueItem | None,
    builds: tuple[Build, ...], mission: Mission | None,
) -> Feature:
    request_text = (run.request_text if run else "") or (item.sentence if item else "")
    project = ""
    if builds:
        project = builds[-1].project
    project = project or (run.target_repo if run else "") or (item.target_repo if item else "")
    project = project or (mission.target_repo if mission else "")
    sent_candidates = [t for t in (
        item.queued_at if item else None,
        run.queued_at if run else None,
        builds[0].queued_at if builds else None,
    ) if t]
    sent_at = min(sent_candidates) if sent_candidates else None

    waits: list[Wait] = []
    if run:
        waits.extend(planning_waits(run))
    for i, b in enumerate(builds):
        waits.extend(build_waits(b, is_latest=i == len(builds) - 1))
    # A mission's own merge wait belongs to the mission, not to each of its features.
    waits.sort(key=lambda w: w.started_at)

    station, outcome, text, ended = _where(run, item, builds, mission, waits)
    delivered = None
    for b in builds:
        d = b.delivered_at()
        if d and (delivered is None or d < delivered):
            delivered = d
    if (delivered is None and mission and any(b.is_mission_feature for b in builds)
            and any(b.landed_on_mission_branch_at() for b in builds)):
        delivered = mission.delivered_at()
    if delivered is not None:
        station, outcome, text, ended = MERGED, Outcome.DELIVERED, "Merged and checked", delivered
    # However long nothing has been recorded, a feature Forge still records as queued, planning or
    # building stays in progress: only an ending or a retirement in the record finishes it. A long
    # silence is shown as a stall (`norms.quiet_spell`), never as an ending.

    return Feature(
        key=key, code=code, title=title_from(request_text, code or "untitled request"),
        request_text=request_text, project=project, sent_at=sent_at, planning=run, queue=item,
        builds=builds, mission=mission, station=station, outcome=outcome, status_text=text,
        ended_at=ended, delivered_at=delivered, waits=tuple(waits),
    )


def _day(dt: datetime) -> str:
    """'9 October', in London time."""
    local = dt.astimezone(ZoneInfo("Europe/London"))
    return f"{local.day} {local.strftime('%B')}"


def _last_activity(run: PlanningRun | None, item: QueueItem | None, builds: tuple[Build, ...]) -> datetime | None:
    times: list[datetime] = []
    if run is not None:
        t = run.last_event_at()
        if t:
            times.append(t)
    for b in builds:
        times.extend(t for t in (b.queued_at, b.started_at, b.completed_at) if t)
        times.extend(st.at for st in b.stages)
    if item is not None:
        times.extend(t for t in (item.queued_at, item.admitted_at, item.closed_at) if t)
    return max(times) if times else None


_WAITING_TEXT = {
    "spec approval": "Waiting for the spec to be approved",
    "build approval": "Waiting for the go-ahead to build",
    "merge decision": "Waiting for the merge decision",
}


def _where(
    run: PlanningRun | None, item: QueueItem | None, builds: tuple[Build, ...],
    mission: Mission | None, waits: Sequence[Wait],
) -> tuple[int, Outcome, str, datetime | None]:
    """(station, outcome, plain words, ended at) from the latest record of the feature."""
    open_wait = next((w for w in reversed(waits) if w.open), None)
    if builds:
        return _where_build(builds[-1], mission, open_wait)
    if run:
        return _where_planning(run, open_wait)
    assert item is not None
    if item.status == "QUEUED":
        return IN_QUEUE, Outcome.IN_PROGRESS, "In the queue", None
    if item.status == "ADMITTED":
        return PLANNING, Outcome.IN_PROGRESS, "Leaving the queue for planning", None
    reason = first_sentence(item.closed_reason) if item.closed_reason else ""
    words = {"BLOCKED": "Stopped in the queue", "WITHDRAWN": "Withdrawn", "DONE": "Closed in the queue"}
    text = words.get(item.status, item.status.capitalize())
    return IN_QUEUE, Outcome.STOPPED_IN_QUEUE, f"{text}: {reason}" if reason else text, item.closed_at


def _where_planning(  # noqa: PLR0911 (one return per planning state)
    run: PlanningRun, open_wait: Wait | None,
) -> tuple[int, Outcome, str, datetime | None]:
    reached = PLAN_APPROVED if run.state == "BUILD_QUEUED" else (SPEC_APPROVED if run.spec_approved() else PLANNING)
    retired = run.retired_at()
    if retired is not None:
        # The owner's administrative clean-up retired this run's handover: nothing more will happen.
        return (PLAN_APPROVED if reached >= PLAN_APPROVED else reached, Outcome.RETIRED,
                f"Retired in the clean-up of {_day(retired)}", retired)
    if run.state == "QUEUED":
        return IN_QUEUE, Outcome.IN_PROGRESS, "Waiting for planning to start", None
    if run.active:
        if open_wait is not None:
            return SPEC_APPROVED, Outcome.IN_PROGRESS, _WAITING_TEXT[open_wait.kind], None
        if reached >= SPEC_APPROVED:
            return PLAN_APPROVED, Outcome.IN_PROGRESS, "Writing the plan", None
        return PLANNING, Outcome.IN_PROGRESS, "Planning", None
    if run.state == "BUILD_QUEUED":
        return BUILDING, Outcome.IN_PROGRESS, "Plan approved; waiting for the build to start", None
    stop_at = SPEC_APPROVED if reached >= SPEC_APPROVED else PLANNING
    if run.state == "PLANNED_HANDOFF":
        if run.nothing_to_build():
            return PLANNING, Outcome.NOTHING_TO_BUILD, "Already done: nothing to build", run.completed_at
        return PLAN_APPROVED, Outcome.HANDED_OFF, "Planned and handed over", run.completed_at
    if run.state == "FAILED":
        why = first_sentence(run.error)
        text = f"Planning failed: {why}" if why else "Planning failed"
        return stop_at, Outcome.PLANNING_FAILED, text, run.completed_at
    words = {"CANCELLED": "Planning cancelled", "TIMED_OUT": "Planning timed out"}
    return stop_at, Outcome.PLANNING_STOPPED, words.get(run.state, run.state.capitalize()), run.completed_at


def _where_build(  # noqa: PLR0911 (one return per build state)
    b: Build, mission: Mission | None, open_wait: Wait | None,
) -> tuple[int, Outcome, str, datetime | None]:
    decision = b.merge_decision()
    approved = decision is not None and decision.status == "PASSED"
    declined = decision is not None and decision.status == "SKIPPED"
    # The owner's 9 October clean-up retired old merge offers, including approved merges that
    # never finished: either way nothing reached main.
    retired = b.merge_offer_retired() and b.delivered_at() is None and b.landed_on_mission_branch_at() is None
    merge_failed = b.merge_check_failed_at()
    landed = b.landed_on_mission_branch_at()
    if merge_failed is not None:
        return MERGED, Outcome.MERGE_FAILED, "Merge check failed", merge_failed
    if retired and b.status == "COMPLETE":
        when = b.merge_retired_at() or b.completed_at
        text = f"Built, not merged: the merge offer was retired in the clean-up of {_day(when)}" if when else (
            "Built, not merged: the merge offer was retired")
        return MERGE_DECISION, Outcome.NOT_MERGED, text, when
    if approved and landed is None and b.status not in ("FAILED", "CANCELLED", "INTERRUPTED"):
        # The merge word was given; the merge and its checks have not finished yet.
        return MERGED, Outcome.IN_PROGRESS, "Merging and checking", None
    if b.status in _BUILD_ACTIVE or (b.status == "COMPLETE" and decision is None and not retired and b.merge_offered()):
        if open_wait is not None and open_wait.kind in ("build approval", "merge decision"):
            station = BUILDING if open_wait.kind == "build approval" else MERGE_DECISION
            return station, Outcome.IN_PROGRESS, _WAITING_TEXT[open_wait.kind], None
        if b.status in ("QUEUED", "PREPARING"):
            return BUILDING, Outcome.IN_PROGRESS, "Waiting for the build to start", None
        if b.status == "COMPLETE":
            return MERGE_DECISION, Outcome.IN_PROGRESS, "Built; the merge decision is next", None
        return BUILDING, Outcome.IN_PROGRESS, "Building", None
    if b.status == "COMPLETE":
        if declined:
            assert decision is not None
            return MERGE_DECISION, Outcome.NOT_MERGED, "Built, not merged", decision.at
        if landed is not None and mission is not None:
            if mission.status in ("NOT_MERGED", "REJECTED"):
                return (MERGED, Outcome.MISSION_NOT_MERGED,
                        f"On mission {mission.id}'s branch; the mission was not merged", mission.closed_at or landed)
            text = (f"On mission {mission.id}'s branch; the mission is stopping, so it will not be merged"
                    if mission.stopping else f"On mission {mission.id}'s branch, waiting for the mission's merge")
            return MERGED, Outcome.ON_MISSION_BRANCH, text, None
        return BUILT, Outcome.BUILT_NO_MERGE, "Built (the record shows no merge)", b.completed_at
    if b.status == "FAILED":
        why = first_sentence(b.error)
        return BUILDING, Outcome.BUILD_FAILED, f"Build failed: {why}" if why else "Build failed", b.completed_at
    words = {"CANCELLED": "Build cancelled", "INTERRUPTED": "Build interrupted", "SKIPPED": "Build skipped"}
    return BUILDING, Outcome.CANCELLED, words.get(b.status, b.status.capitalize()), b.completed_at


# --- small time helpers the pages share -----------------------------------------------------------


def machine_seconds(start: datetime | None, end: datetime | None, waits: Iterable[Wait], now: datetime) -> float | None:
    """Seconds from start to end (or now), minus the waits that fall inside. None without a start."""
    if start is None:
        return None
    stop = end or now
    total = (stop - start).total_seconds()
    for w in waits:
        w_end = w.ended_at or now
        lo, hi = max(w.started_at, start), min(w_end, stop)
        if hi > lo:
            total -= (hi - lo).total_seconds()
    return max(0.0, total)


def planning_seconds(f: Feature, now: datetime) -> float | None:
    if f.planning is None:
        return None
    waits = [w for w in f.waits if w.kind == "spec approval"]
    return machine_seconds(f.planning.started_at, f.planning.completed_at, waits, now)


def build_seconds(f: Feature, now: datetime) -> float | None:
    b = f.latest_build
    if b is None:
        return None
    waits = [w for w in f.waits if w.kind == "build approval"]
    end = b.completed_at if b.status not in _BUILD_ACTIVE else None
    return machine_seconds(b.started_at, end, waits, now)
