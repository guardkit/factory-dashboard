"""The cards the factory is waiting on a person for, and what each one recorded once it closed.

Design of 19 October 2026 (docs/architecture/approvals-from-the-dashboard-2026-10-19.md), "Which
cards", "What is open" and "What each card can show once it has closed". Everything here is read
from the copy of Forge's record (`load_factory`), the same copy the pages show; nothing here writes,
sends or guesses. The Linear door's `ForgeRecord.cards()` reads the same rows the same way.

A card carries what an answer needs (its current request id, attempt, correlation id and subject
token), the buttons it offers, and the evidence the person reads beside them. A card that has
closed carries only what Forge recorded for it (`Recorded`): who answered and how where Forge writes
that down, otherwise just how it closed. A card that closed without a person's answer (no answer in
time, a cancelled run, a retired offer) is never called answered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from backend.readmodel import missionwords
from backend.readmodel.factory import Build, Factory, Feature, Mission, MissionFeature, PlanningEvent, PlanningRun

# --- the kinds of card ---------------------------------------------------------------------------

SPEC_REVIEW = "spec_review"
SIGN_IN = "sign_in"
BUILD_GATE = "build_gate"
MISSION_PLAN = "mission_plan"
MERGE = "merge"
CHECKPOINT = "checkpoint"
PAUSED = "paused"      # a paused build at another card (a fix journey's merge-ready checkpoint, a budget pause)

KIND_TITLES = {
    SPEC_REVIEW: "Spec review",
    SIGN_IN: "Sign-in question",
    BUILD_GATE: "Go-ahead to build",
    MISSION_PLAN: "Mission plan",
    MERGE: "Merge card",
    CHECKPOINT: "Assumptions checkpoint",
    PAUSED: "Paused build",
}

#: The planning doors that wait inline: (kind, Forge's stage label, the key its record sits under).
#: Forge's planning/driver.py (`_DIGEST_REVIEW_STAGE`, `_AUTH_CONFIRM_STAGE`, `_inline_confirmation_door`).
DOORS: tuple[tuple[str, str, str], ...] = (
    (SPEC_REVIEW, "feature-spec-digest-review", "digest_review"),
    (SIGN_IN, "qa-pass-bars-auth-confirm", "auth_confirmation"),
)
_DOOR_OPEN = frozenset({"GATED", "reopened"})
#: Planning states after which no planning card can still be waiting (the Linear door's list).
_PLANNING_OVER = {
    "FAILED": "the planning run failed",
    "CANCELLED": "the planning run was cancelled",
    "TIMED_OUT": "the planning run ran out of time",
    "PLANNED_HANDOFF": "planning had already moved on",
    "BUILD_QUEUED": "planning had already moved on",
}

# Forge's merge rows (pipeline/merge_offer.py, merge_executor.py, merge_card.py).
MERGE_OFFER_TARGET = "merge_deploy_offer"
MERGE_DECISION_TARGET = "merge_deploy_decision"
#: Forge refuses a fourth rebuild of one card's preview (merge_card.REBUILD_CAP).
REBUILD_CAP = 3
#: The note that makes a merge card's "defer" a rebuild of its preview (merge_card.REBUILD_NOTE).
REBUILD_NOTE = "rebuild-preview"

SUBJECT_PREFIX = "agents.approval.forge."
#: One NATS subject token: no dots, no wildcards, no spaces.
_TOKEN = re.compile(r"[^.*>\s]{1,200}")

SLACK = "Answer this one in Slack."
#: Forge's build gate: the stage its request ids name (`<build>:autobuild:<attempt>`).
BUILD_GATE_STAGE = "autobuild"
_LONDON = ZoneInfo("Europe/London")


def valid_token(token: str) -> bool:
    return bool(_TOKEN.fullmatch(token or ""))


# --- what a card is --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Button:
    """One answer a card offers. `note` is "" (none), "required" or "optional"."""

    answer: str
    label: str
    primary: bool = False
    note: str = ""
    note_label: str = ""


#: The buttons of each card, in the order they are shown. The words say what each answer means;
#: backend/answers.py maps each one to the decision Forge accepts.
BUTTONS: dict[str, tuple[Button, ...]] = {
    SPEC_REVIEW: (
        Button("approve", "Approve the spec", primary=True),
        Button("changes", "Ask for changes", note="required", note_label="What should change"),
        Button("stop", "Stop this request", note="optional", note_label="Why (optional)"),
    ),
    SIGN_IN: (
        Button("no_sign_in", "No sign-in needed", primary=True),
        Button("needs_sign_in", "It needs sign-in"),
    ),
    BUILD_GATE: (
        Button("start", "Start the build", primary=True),
        Button("cancel", "Cancel", note="optional", note_label="Why (optional)"),
    ),
    MISSION_PLAN: (
        Button("start", "Start the mission", primary=True),
        Button("dont_start", "Don't start it"),
    ),
    MERGE: (
        Button("merge", "Merge and deploy", primary=True),
        Button("decline", "Decline"),
    ),
    CHECKPOINT: (),
    PAUSED: (),
}
REBUILD_BUTTON = Button("rebuild", "Rebuild the preview")


@dataclass(frozen=True)
class Recorded:
    """What Forge recorded when a card closed. `answered` only when a person's answer is on record."""

    text: str                         # what happened, without the person's name ("approved the spec")
    answered: bool
    who: str = ""                     # the recorded decided_by / actor identity
    decision: str = ""
    notes: str = ""
    at: datetime | None = None


@dataclass(frozen=True)
class Evidence:
    """What the person reads beside the buttons, all from the record."""

    heading: str = ""
    lines: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    assumptions: tuple[tuple[str, str], ...] = ()   # (what was assumed, why)
    sign_in: str = ""                               # the spec card's sign-in question, when it carries one
    plan: tuple[MissionFeature, ...] = ()
    preview_url: str = ""
    report_url: str = ""


@dataclass(frozen=True)
class Rebuild:
    """A merge card's preview, as its saved offer records it (merge_offer.py ~3271-3313)."""

    offered: bool          # the card has a preview this release describes, and fewer than three rebuilds
    count: int             # preview_rebuilds
    wanted_at: str         # rebuild_wanted_at: Forge is waiting for room to rebuild
    state: str             # review_evidence.state: ready, failed or ended
    failure: str           # review_evidence.failure_reason


@dataclass(frozen=True)
class Card:
    kind: str
    request_id: str
    attempt: int
    correlation_id: str
    token: str                        # the subject token: agents.approval.forge.<token>
    opened_at: datetime | None
    open: bool
    answerable: bool                  # buttons may be shown (an open card with everything an answer needs)
    subject: str                      # the feature's label, or the mission's
    link: str                         # the page the card belongs to
    evidence: Evidence = field(default_factory=Evidence)
    recorded: Recorded | None = None  # set once the card has closed
    slack_reason: str = ""            # why an open card is answered in Slack only
    feature_key: str = ""
    mission_id: str = ""
    build_id: str = ""
    assumption_ids: tuple[str, ...] = ()
    rebuild: Rebuild | None = None

    @property
    def title(self) -> str:
        return KIND_TITLES[self.kind]

    @property
    def buttons(self) -> tuple[Button, ...]:
        if not (self.open and self.answerable):
            return ()
        extra = (REBUILD_BUTTON,) if self.rebuild is not None and self.rebuild.offered else ()
        return BUTTONS[self.kind] + extra

    @property
    def anchor(self) -> str:
        return self.anchor_for(self.request_id)

    @staticmethod
    def anchor_for(request_id: str) -> str:
        """An HTML id for a card: its request id with anything but letters and digits as '-'."""
        return "card-" + re.sub(r"[^A-Za-z0-9]+", "-", request_id).strip("-")[:120]

    @property
    def response_subject(self) -> str:
        return f"{SUBJECT_PREFIX}{self.token}.response"


# --- small readers -----------------------------------------------------------------------------------


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def https_link(url: object) -> str:
    """`url` when it is a plain https link (the Slack and Linear cards' rule), else ""."""
    if not isinstance(url, str) or not url or len(url) > 2000 or re.search(r"[\s<>\"'`\\]", url):
        return ""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or "@" in parts.netloc:
        return ""
    return url


def _day(dt: datetime) -> str:
    local = dt.astimezone(_LONDON)
    return f"{local.day} {local.strftime('%B')}"


def stage_of(request_id: str) -> str:
    """The stage a Forge request id names (Forge's `parse_request_id`), or "" when it is not one."""
    parts = request_id.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        return ""
    return unquote(parts[1])


def _attempt_of(request_id: str) -> int:
    """The attempt at the end of a Forge request id (`build:stage:attempt`), or 0."""
    tail = request_id.rsplit(":", 1)[-1] if ":" in request_id else ""
    return _int(tail) if tail.isdigit() else 0


# --- planning cards ------------------------------------------------------------------------------------

#: What a door's verdict row says, by Forge's recorded outcome (planning/driver.py `_decide`).
_DOOR_ANSWERS: dict[str, dict[str, str]] = {
    SPEC_REVIEW: {
        "approved": "approved the spec",
        "revise": "asked for changes",
        "cancelled": "stopped this request",
        "rejected": "stopped this request",
        "deferred": "set the card aside",
    },
    SIGN_IN: {
        "confirmed": "said no sign-in is needed",
        "approved": "said no sign-in is needed",
        "rejected": "said it needs sign-in",
        "deferred": "set the card aside",
    },
}
_DOOR_CLOSED = {
    "timed_out": "Closed without an answer: nobody answered in time.",
    "undeliverable": "Closed without an answer: the card could not be delivered.",
}


def _door_card(f: Feature, run: PlanningRun, kind: str, stage: str, key: str) -> Card | None:
    """The latest round of one planning door. A door with no opening row carrying a request id never
    showed a card (a mission feature's spec review is skipped, Forge writes only a `skipped` row), so
    there is none. The verdict is the FIRST row after the latest opening: when a Stop cancels the run
    Forge writes the verdict, then a second row for the move to CANCELLED that carries no decision."""
    rows = [e for e in run.events if e.stage_label == stage and isinstance(e.details.get(key), dict)]
    openings = [e for e in rows if e.status in _DOOR_OPEN and _text(_dict(e.details.get(key)).get("request_id"))]
    if not openings:
        return None
    opening = openings[-1]
    opened = _dict(opening.details.get(key))
    request_id = _text(opened.get("request_id"))
    # A door reopened after a restart keeps its request id: the card has been waiting since the first.
    first = next(e for e in openings if _text(_dict(e.details.get(key)).get("request_id")) == request_id)
    after = [e for e in rows if e.id > opening.id]
    token = f"plan-{run.correlation_id}"
    base = dict(kind=kind, request_id=request_id, attempt=_int(opened.get("attempt_count")),
                correlation_id=run.correlation_id, token=token, opened_at=first.at, subject=f.label,
                link=f.link, feature_key=f.key, evidence=_door_evidence(kind, opened))
    if kind == SPEC_REVIEW:
        card_body = _dict(opened.get("card"))
        ids = tuple(_text(a.get("id")) for a in _list(card_body.get("assumptions")) if isinstance(a, dict)
                    and _text(a.get("id")))
        base["assumption_ids"] = ids
    if not after:
        if run.state in _PLANNING_OVER:
            return Card(**base, open=False, answerable=False,  # type: ignore[arg-type]
                        recorded=Recorded(f"Closed without an answer: {_PLANNING_OVER[run.state]}.", False,
                                          at=run.completed_at))
        ok = bool(request_id) and valid_token(token)
        return Card(**base, open=True, answerable=ok, slack_reason="" if ok else SLACK)  # type: ignore[arg-type]
    verdict = after[0]
    return Card(**base, open=False, answerable=False,  # type: ignore[arg-type]
                recorded=_door_verdict(kind, verdict, _dict(verdict.details.get(key))))


def _door_verdict(kind: str, row: PlanningEvent, record: dict[str, object]) -> Recorded:
    outcome = _text(record.get("outcome")) or row.status
    who = _text(record.get("decided_by"))
    decision = _text(record.get("decision"))
    notes = _text(record.get("notes"))
    # A person's answer: who and which. A Stop is recorded as outcome "cancelled" with decided_by,
    # and a row written only for the move to CANCELLED carries no decision, so either is "stopped".
    if who and (decision or outcome == "cancelled"):
        words = _DOOR_ANSWERS[kind].get(outcome, f"answered “{decision}”")
        return Recorded(words, True, who=row.actor if row.actor and row.actor != "planning-driver" else who,
                        decision=decision, notes=notes, at=row.at)
    text = _DOOR_CLOSED.get(outcome, f"Closed without an answer: the factory recorded “{outcome}”.")
    return Recorded(text, False, at=row.at)


def _door_evidence(kind: str, opened: dict[str, object]) -> Evidence:
    if kind == SIGN_IN:
        basis = tuple(_text(x) for x in _list(opened.get("basis_lines")) if _text(x))
        return Evidence(
            heading="Does this feature sit behind a sign-in?",
            lines=("The spec checker flagged this feature as sitting behind a sign-in. These lines of the "
                   "spec tripped it:", *basis,
                   "No sign-in needed: the build carries on as it would for an unflagged feature. "
                   "It needs sign-in: the run stops so the checks are registered by hand."),
        )
    card = _dict(opened.get("card"))
    lines = [_text(card.get(k)) for k in ("what_happened", "possible_contradiction") if _text(card.get(k))]
    if _text(card.get("target_repo")):
        lines.insert(0, f"Project: {_text(card.get('target_repo'))}")
    examples = tuple(_text(e.get("sentence")) for e in _list(card.get("what_it_will_do"))
                     if isinstance(e, dict) and _text(e.get("sentence")))
    assumed = tuple((_text(e.get("assumption")), _text(e.get("why")))
                    for e in _list(card.get("what_the_machine_assumed"))
                    if isinstance(e, dict) and _text(e.get("assumption")))
    check = _dict(card.get("sign_in_check"))
    sign_in = _text(check.get("statement")) if check else ""
    if check and not sign_in:
        sign_in = _text(check.get("title")) or "One thing to confirm."
    return Evidence(heading=_text(card.get("title")) or "The spec is ready", lines=tuple(lines),
                    examples=examples, assumptions=assumed, sign_in=sign_in)


def _checkpoint_card(f: Feature, run: PlanningRun) -> Card | None:
    """The older assumptions checkpoint: a PAUSED run. Its assumption list exists only on the bus,
    so the dashboard cannot show what it would be answering; it is answered in Slack."""
    if run.state != "PAUSED" or not run.pending_request_id:
        return None
    paused = [e for e in run.events if e.status in ("GATED", "PAUSED")]
    return Card(kind=CHECKPOINT, request_id=run.pending_request_id, attempt=_attempt_of(run.pending_request_id),
                correlation_id=run.correlation_id, token=f"plan-{run.correlation_id}",
                opened_at=paused[-1].at if paused else run.started_at, open=True, answerable=False,
                subject=f.label, link=f.link, feature_key=f.key,
                slack_reason="Its list of assumptions is only in Slack, so answer this one in Slack.",
                evidence=Evidence(heading="The planning run is waiting at its assumptions checkpoint"))


# --- build gates -----------------------------------------------------------------------------------------

_GATE_CARRIED_ON = {
    "QUEUED": "the build is waiting to start", "PREPARING": "the build is starting",
    "RUNNING": "the build carried on", "FINALISING": "the build carried on", "COMPLETE": "the build carried on",
    "CANCELLED": "the build was cancelled", "FAILED": "the build failed", "INTERRUPTED": "the build was interrupted",
    "SKIPPED": "the build was skipped",
}


def _pause_base(b: Build, rid: str, subject: str, link: str, feature_key: str, request_text: str
                ) -> dict[str, object]:
    """A paused build's card: the build gate only when Forge's request id names the `autobuild` stage.
    Any other pause (a fix journey parked at the merge-ready checkpoint, a budget pause) is shown with
    the stage Forge names and answered in Slack."""
    stage = stage_of(rid)
    if stage == BUILD_GATE_STAGE:
        evidence = Evidence(heading="The plan is ready; the build waits for the go-ahead",
                            lines=tuple(x for x in (request_text, f"Build {b.build_id}") if x))
        kind = BUILD_GATE
    else:
        where = f"at {stage}" if stage else "at a card this dashboard cannot answer"
        evidence = Evidence(heading=f"The build is waiting {where}",
                            lines=tuple(x for x in (request_text, f"Build {b.build_id}") if x))
        kind = PAUSED
    return dict(kind=kind, correlation_id=b.correlation_id, token=b.build_id, subject=subject, link=link,
                feature_key=feature_key, build_id=b.build_id, mission_id=b.mission_id, evidence=evidence)


def _gate_card(b: Build, subject: str, link: str, feature_key: str, request_text: str) -> Card | None:
    pauses = [s for s in b.stages if isinstance(s.details.get("gate_pause"), dict)]
    if b.status == "PAUSED" and b.pending_request_id:
        rid = b.pending_request_id
        base = _pause_base(b, rid, subject, link, feature_key, request_text)
        mine = [s for s in pauses if _text(_dict(s.details.get("gate_pause")).get("request_id")) == rid]
        attempt = _int(_dict(mine[-1].details["gate_pause"]).get("attempt_count")) if mine else _attempt_of(rid)
        opened = mine[0].at if mine else (pauses[-1].at if pauses else b.started_at)
        ok = base["kind"] == BUILD_GATE and bool(b.correlation_id) and valid_token(b.build_id)
        return Card(**base, request_id=rid, attempt=attempt, opened_at=opened, open=True,  # type: ignore[arg-type]
                    answerable=ok, slack_reason="" if ok else SLACK)
    if not pauses:
        return None
    last = _dict(pauses[-1].details["gate_pause"])
    rid = _text(last.get("request_id"))
    base = _pause_base(b, rid, subject, link, feature_key, request_text)
    if b.status == "PAUSED":   # paused again under a new card that is not recorded yet
        return None
    after = _GATE_CARRIED_ON.get(b.status, f"the build is {b.status.lower()}")
    # Forge does not record who answered a build gate, so the page never names anyone.
    return Card(**base, request_id=rid, attempt=_int(last.get("attempt_count")), opened_at=pauses[-1].at,  # type: ignore[arg-type]
                open=False, answerable=False, recorded=Recorded(f"The card closed, and {after}.", False))


# --- merge cards -------------------------------------------------------------------------------------------


def _merge_card(b: Build, subject: str, link: str, feature_key: str, mission_id: str) -> Card | None:
    offers = [s for s in b.stages if s.target == MERGE_OFFER_TARGET and isinstance(s.details.get("merge_offer"), dict)]
    if not offers:
        return None
    offer = _dict(offers[-1].details["merge_offer"])
    request_id = _text(offer.get("request_id"))
    correlation = _text(offer.get("correlation_id"))
    approval_subject = _text(offer.get("approval_subject"))
    token = approval_subject[len(SUBJECT_PREFIX):] if approval_subject.startswith(SUBJECT_PREFIX) else ""
    first = next((s for s in offers if _text(_dict(s.details["merge_offer"]).get("request_id")) == request_id),
                 offers[-1])
    evidence_block = _dict(offer.get("review_evidence"))
    rebuild = Rebuild(
        offered="card_words" in offer and bool(evidence_block) and _int(offer.get("preview_rebuilds")) < REBUILD_CAP,
        count=_int(offer.get("preview_rebuilds")), wanted_at=_text(offer.get("rebuild_wanted_at")),
        state=_text(evidence_block.get("state")), failure=_text(evidence_block.get("failure_reason")).rstrip("."),
    )
    base = dict(kind=MERGE, request_id=request_id, attempt=0, correlation_id=correlation, token=token,
                opened_at=first.at, subject=subject, link=link, feature_key=feature_key, mission_id=mission_id,
                build_id=b.build_id, evidence=_merge_evidence(offer, evidence_block), rebuild=rebuild)
    decisions = [s for s in b.stages if s.target == MERGE_DECISION_TARGET
                 and isinstance(s.details.get("merge_decision"), dict)
                 and "decision" in _dict(s.details["merge_decision"])]
    if decisions:
        said = _dict(decisions[-1].details["merge_decision"])
        who, decision = _text(said.get("decided_by")), _text(said.get("decision"))
        words = {"approve": "approved the merge", "reject": "declined the merge"}.get(
            decision, f"answered “{decision}”")
        recorded = (Recorded(words, True, who=who, decision=decision, at=decisions[-1].at) if who
                    else Recorded(f"Closed: the factory recorded “{decision}”.", False, at=decisions[-1].at))
        return Card(**base, open=False, answerable=False, recorded=recorded)  # type: ignore[arg-type]
    retired = b.merge_retired_at()
    if retired is not None:
        return Card(**base, open=False, answerable=False,  # type: ignore[arg-type]
                    recorded=Recorded(f"Closed without an answer: the offer was retired in the clean-up of "
                                      f"{_day(retired)}.", False, at=retired))
    ok = bool(request_id) and bool(correlation) and valid_token(token)
    return Card(**base, open=True, answerable=ok, slack_reason="" if ok else SLACK)  # type: ignore[arg-type]


def _merge_evidence(offer: dict[str, object], ev: dict[str, object]) -> Evidence:
    lines: list[str] = []
    branch, sha = _text(offer.get("branch")), _text(offer.get("candidate_sha"))
    if branch:
        lines.append(f"Merges {branch}" + (f" at {sha[:10]}" if sha else "") + " and deploys it.")
    files = offer.get("changed_files")
    if isinstance(files, list) and files:
        lines.append(f"{len(files)} file{'s' if len(files) != 1 else ''} changed.")
    state = _text(ev.get("state"))
    if state == "ready":
        lines.append("The preview is ready to try.")
    elif state == "failed":
        why = _text(ev.get("failure_reason")).rstrip(".") or "no reason was given"
        lines.append(f"The preview could not be prepared: {why}.")
    elif state == "ended":
        lines.append("The preview has ended.")
    return Evidence(heading="The build is ready to merge", lines=tuple(lines),
                    preview_url=https_link(ev.get("app_url")) if state == "ready" else "",
                    report_url=https_link(ev.get("report_url")))


# --- mission plans ----------------------------------------------------------------------------------------


def _mission_plan_card(m: Mission, now: datetime, names: missionwords.Names | None) -> Card | None:
    if not m.plan_request_id:
        return None
    token = f"mission-{m.id}"
    evidence = Evidence(heading=f"Mission: {m.title}" if m.title else "Mission plan",
                        lines=(f"Project: {m.target_repo}",) if m.target_repo else (), plan=m.plan)
    base = dict(kind=MISSION_PLAN, request_id=m.plan_request_id, attempt=_attempt_of(m.plan_request_id),
                correlation_id=m.correlation_id, token=token, opened_at=m.created_at, subject=_mission_label(m),
                link=m.link, mission_id=m.id, evidence=evidence)
    if m.status == "PROPOSED" and not m.approved_at:
        # Buttons only when the whole plan is in the copy: the mission row and at least one feature.
        ok = bool(m.plan) and bool(m.correlation_id) and valid_token(token)
        reason = "" if ok else ("Its plan is not in the dashboard's copy yet, so answer this one in Slack."
                                if not m.plan else SLACK)
        return Card(**base, open=True, answerable=ok, slack_reason=reason)  # type: ignore[arg-type]
    if m.approved_by:
        rec = Recorded("approved the plan", True, who=m.approved_by, decision="approve", at=m.approved_at)
    elif m.status in ("REJECTED", "NOT_STARTED"):
        # Forge does not record who rejected a mission plan: only that it was not started, and why
        # (a stop's reason names an approver identity, which `reason_words` never shows as it is).
        reason = missionwords.reason_words(m.closed_reason, now, names)
        rec = Recorded(f"Not started: {reason}." if reason else "Not started.", False, at=m.closed_at)
    else:
        rec = Recorded(f"The card closed; the mission is {missionwords.status_words(m)}.", False)
    return Card(**base, open=False, answerable=False, recorded=rec)  # type: ignore[arg-type]


def _mission_label(m: Mission) -> str:
    return f"Mission {m.id}" + (f" ({m.title})" if m.title else "")


# --- assembling ------------------------------------------------------------------------------------------


def feature_cards(f: Feature) -> tuple[Card, ...]:
    """Every card of one feature, open or closed: the latest of each planning door, the older
    checkpoint while it waits, and each build's go-ahead card and merge card."""
    out: list[Card] = []
    run = f.planning
    if run is not None:
        for kind, stage, key in DOORS:
            door = _door_card(f, run, kind, stage, key)
            if door is not None:
                out.append(door)
        checkpoint = _checkpoint_card(f, run)
        if checkpoint is not None:
            out.append(checkpoint)
    for b in f.builds:
        gate = _gate_card(b, f.label, f.link, f.key, f.request_text)
        if gate is not None:
            out.append(gate)
        merge = _merge_card(b, f.label, f.link, f.key, b.mission_id)
        if merge is not None:
            out.append(merge)
    return tuple(out)


def mission_cards(m: Mission, *, now: datetime | None = None,
                  names: missionwords.Names | None = None) -> tuple[Card, ...]:
    """The mission's plan card and its own merge card (and that merge build's go-ahead card).
    `names` maps an approver identity to a display name, for a reason that names one."""
    out: list[Card] = []
    plan = _mission_plan_card(m, now or datetime.now(UTC), names)
    if plan is not None:
        out.append(plan)
    b = m.merge_build
    if b is not None:
        label = _mission_label(m)
        gate = _gate_card(b, label, m.link, "", "")
        if gate is not None:
            out.append(gate)
        merge = _merge_card(b, label, m.link, "", m.id)
        if merge is not None:
            out.append(merge)
    return tuple(out)


def all_cards(factory: Factory) -> tuple[Card, ...]:
    out: list[Card] = []
    for f in factory.features:
        out.extend(feature_cards(f))
    for m in factory.missions:
        out.extend(mission_cards(m))
    return tuple(out)


def open_cards(factory: Factory) -> tuple[Card, ...]:
    """Every open card, longest waiting first."""
    found = [c for c in all_cards(factory) if c.open]
    found.sort(key=lambda c: c.opened_at or datetime.max.replace(tzinfo=UTC))
    return tuple(found)


def find(factory: Factory, request_id: str) -> Card | None:
    """The card with this request id, open or closed (an open one first)."""
    found = [c for c in all_cards(factory) if c.request_id == request_id]
    return next((c for c in found if c.open), found[0] if found else None)


def current(factory: Factory, kind: str, token: str) -> Card | None:
    """The open card of this kind on this subject now, whatever its request id."""
    return next((c for c in all_cards(factory) if c.open and c.kind == kind and c.token == token), None)
