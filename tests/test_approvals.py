"""Answering the factory's cards from the dashboard (design of 19 October 2026).

Every card kind is answered end to end through the real routes, the real feed code and a fake bus
client that records what was published: the exact subject and payload, the envelope's correlation
id and `decided_by`. Then: an already-answered card sends nothing; two presses at once send one
answer; the guards; the spec card's fixed prefix; the sign-in card's meanings; the rebuild's states;
the stale-copy refusals; an unreachable bus; and confirmation against the record.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from backend import accounts, answers, claims
from backend.app import create_app
from backend.auth import ResolvedUser
from backend.projector.forge_mirror import mirror_once
from backend.projector.store import iso, open_rw, write_heartbeat
from starlette.testclient import TestClient

from tests.conftest import LOOPBACK_URL, TENANTS_TEST, account_row, client_account_row, login, write_accounts
from tests.forge_fake import ForgeFake, store_from

# Placeholders only: no real member id appears in this public repository.
APPROVER = "U0PLACEHOLDER1"
ORIGIN = {"Origin": LOOPBACK_URL}

OFFER_SUBJECT = "agents.approval.forge.merge-FEAT-0001"
PREVIEW = "https://preview.example.test/notes/"
REPORT = "https://reports.example.test/b1/index.html"
SPEC_CARD: dict[str, object] = {
    "checkpoint": "spec_digest_review",
    "title": "The spec is ready — here's what will be built",
    "what_happened": "The machine wrote the spec from your sentence.",
    "what_it_will_do": [{"sentence": "Typing in the search box shows only notes whose title matches.", "tags": []}],
    "what_the_machine_assumed": [{"assumption": "The search ignores capital letters.", "why": "the usual choice"}],
}


class FakeBus:
    """Records what would have been published; can be made slow or unreachable."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail = False
        self.delay = 0.0

    async def publish(self, subject: str, data: bytes) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise OSError("nothing is listening there")
        self.sent.append((subject, json.loads(data)))


@dataclass
class World:
    client: TestClient
    fake: ForgeFake
    feed: sqlite3.Connection
    bus: FakeBus
    store: Path
    answers_db: Path

    def refresh(self, passes: int = 1, now: datetime | None = None) -> None:
        """Feed passes over the fake record (a heartbeat and a successful read each)."""
        for _ in range(passes):
            at = now or datetime.now(UTC)
            write_heartbeat(self.feed, at)
            mirror_once(self.feed, self.fake.path, at)

    def answer(self, request_id: str, kind: str, token: str, answer: str, note: str = "", back: str = "/",
               headers: dict[str, str] | None = None) -> Any:
        return self.client.post("/cards/answer", data={
            "request_id": request_id, "kind": kind, "token": token, "answer": answer, "note": note, "back": back,
        }, headers=ORIGIN if headers is None else headers, follow_redirects=False)


def said(response: Any) -> str:
    assert response.status_code == 303, (response.status_code, response.text)
    return str(parse_qs(urlsplit(response.headers["location"]).query)["said"][0])


def approver_row() -> dict[str, object]:
    return {**account_row(), "approver_identity": APPROVER}


@pytest.fixture
def world(tmp_path: Path, config_dir: Path) -> Iterator[World]:
    write_accounts(config_dir, [approver_row(), client_account_row()])
    fake = ForgeFake(tmp_path / "forge.db")
    store = tmp_path / "readmodel.db"
    feed = store_from(fake, store)
    bus = FakeBus()
    app = create_app(db_path=store, tenants_path=TENANTS_TEST, answer_bus=bus)
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        assert login(client).status_code == 200
        yield World(client, fake, feed, bus, store, tmp_path / claims.DB_NAME)
    feed.close()


# --- building each card in the fake record ----------------------------------------------------------------


def spec_card(w: World, corr: str = "c1", card: dict[str, object] | None = None) -> str:
    rid = f"plan-{corr}:feature-spec-digest-review:0"
    w.fake.planning(corr, "Add a search box that filters notes by title.", state="FEATURE_SPEC", end=None)
    w.fake.event(corr, "feature-spec-digest-review", "GATED", 5, {"digest_review": {
        "outcome": "opened", "request_id": rid, "attempt_count": 0, "expected_approver": APPROVER,
        "card": card or SPEC_CARD, "wait_seconds": 3600}}, actor="planning-driver")
    w.refresh()
    return rid


def sign_in_card(w: World, corr: str = "c2") -> str:
    rid = f"plan-{corr}:qa-pass-bars-auth-confirm:0"
    w.fake.planning(corr, "Show each user their own notes.", state="FEATURE_PLAN", end=None)
    w.fake.event(corr, "qa-pass-bars-auth-confirm", "GATED", 5, {"auth_confirmation": {
        "outcome": "opened", "request_id": rid, "attempt_count": 0, "expected_approver": APPROVER,
        "basis_lines": ["each user signs in to see their notes"], "wait_seconds": 3600}}, actor="planning-driver")
    w.refresh()
    return rid


def gate_card(w: World, corr: str = "c3", build: str = "b3", attempt: int = 0) -> str:
    rid = f"{build}:autobuild:{attempt}"
    if attempt == 0:
        w.fake.planning(corr, "Count the notes.", state="BUILD_QUEUED")
        w.fake.build(build, "FEAT-0003", corr, status="PAUSED", completed=None)
    w.fake.conn.execute("UPDATE builds SET pending_approval_request_id=? WHERE build_id=?", (rid, build))
    w.fake.stage(build, "autobuild", "autobuild_runner", "GATED", 30 + attempt,
                 {"gate_pause": {"request_id": rid, "attempt_count": attempt}})
    w.refresh()
    return rid


def mission_card(w: World, mid: str = "m1", plan: bool = True) -> str:
    rid = f"mission-{mid}:mission-plan:0"
    w.fake.mission(mid, "Notes search", f"lin-m-{mid}-1", status="PROPOSED")
    w.fake.conn.execute("UPDATE missions SET plan_request_id=? WHERE id=?", (rid, mid))
    if plan:
        for key, sentence in (("search", "Add a search box."), ("highlight", "Highlight the matches.")):
            w.fake.conn.execute("INSERT INTO mission_features (mission_id, key, sentence) VALUES (?,?,?)",
                                (mid, key, sentence))
        w.fake.conn.execute("INSERT INTO mission_needs (mission_id, key, needs_key) VALUES (?,?,?)",
                            (mid, "highlight", "search"))
    w.refresh()
    return rid


def offer(rebuilds: int = 0, state: str = "ready", failure: str | None = None,
          wanted: str | None = None) -> dict[str, object]:
    record: dict[str, object] = {
        "kind": "merge_deploy_offer", "build_id": "b1", "feature_id": "FEAT-0001", "branch": "autobuild/FEAT-0001",
        "candidate_sha": "1e581663e8aa1e581663e8aa1e581663e8aa1e58", "request_id": "merge-b1",
        "correlation_id": "c1", "approval_subject": OFFER_SUBJECT, "card_words": {"branch": "autobuild/FEAT-0001"},
        "changed_files": [{"status": "M", "path": "src/notes.ts", "old_path": ""}],
        "review_evidence": {"state": state, "app_url": PREVIEW, "report_url": REPORT, "failure_reason": failure},
        "preview_rebuilds": rebuilds,
    }
    if wanted:
        record["rebuild_wanted_at"] = wanted
    return record


def merge_card(w: World, *, corr: str = "c1") -> str:
    w.fake.planning(corr, "Search box that filters notes by title.")
    w.fake.build("b1", "FEAT-0001", corr)
    w.fake.stage("b1", "the merge word", "merge_deploy_offer", "GATED", 81, {"merge_offer": offer()})
    w.refresh()
    return "merge-b1"


def amend_offer(w: World, minute: float, **kw: Any) -> None:
    w.fake.stage("b1", "the merge word", "merge_deploy_offer", "GATED", minute, {"merge_offer": offer(**kw)})


def decide_merge(w: World, decision: str = "approve", who: str = "U0SOMEONEELSE") -> None:
    status = "PASSED" if decision == "approve" else "SKIPPED"
    w.fake.stage("b1", "the merge word", "merge_deploy_decision", status, 85,
                 {"merge_decision": {"decision": decision, "decided_by": who, "request_id": "merge-b1"}})


def page(w: World, path: str) -> str:
    return str(w.client.get(path).text)


def recorded_lines(body: str) -> list[str]:
    return re.findall(r'<p class="answer-card__recorded">(.*?)</p>', body, re.DOTALL)


def one_sent(w: World) -> tuple[str, dict[str, Any]]:
    assert len(w.bus.sent) == 1, w.bus.sent
    return w.bus.sent[0]


def check_envelope(env: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    assert env["source_id"] == "dashboard" and env["event_type"] == "approval_response"
    assert env["correlation_id"] == correlation_id
    assert env["version"] == "1.0" and env["message_id"] and env["timestamp"]
    assert env["payload"]["decided_by"] == APPROVER
    payload: dict[str, Any] = env["payload"]
    return payload


# --- one test per card kind ---------------------------------------------------------------------------------


def test_spec_review_approve(world: World) -> None:
    rid = spec_card(world)
    body = page(world, "/features/c1")
    assert "Approve the spec" in body and "Ask for changes" in body and "Stop this request" in body
    assert "Typing in the search box shows only notes whose title matches." in body       # the card's evidence
    assert said(world.answer(rid, "spec_review", "plan-c1", "approve", back="/features/c1")) == "sent"
    subject, env = one_sent(world)
    assert subject == "agents.approval.forge.plan-c1.response"
    payload = check_envelope(env, "c1")
    assert payload == {"request_id": rid, "decision": "approve", "decided_by": APPROVER, "notes": None,
                       "dispositions": None}
    assert answers.SENT in page(world, "/features/c1")


def test_spec_review_approve_accepts_every_assumption_the_card_lists(world: World) -> None:
    rid = spec_card(world, card={**SPEC_CARD, "assumptions": [{"id": "ASSUM-1"}, {"id": "ASSUM-2"}]})
    assert said(world.answer(rid, "spec_review", "plan-c1", "approve")) == "sent"
    _, env = one_sent(world)
    assert env["payload"]["dispositions"] == [
        {"assumption_id": "ASSUM-1", "disposition": "accepted", "edit_delta": None, "notes": None},
        {"assumption_id": "ASSUM-2", "disposition": "accepted", "edit_delta": None, "notes": None}]


def test_ask_for_changes_always_means_revise_even_when_the_note_starts_with_reject(world: World) -> None:
    rid = spec_card(world)
    assert said(world.answer(rid, "spec_review", "plan-c1", "changes", note="reject this and that")) == "sent"
    _, env = one_sent(world)
    assert env["payload"]["decision"] == "reject"
    assert env["payload"]["notes"] == "Please change: reject this and that"
    assert env["payload"]["notes"].startswith("Please change: ")


def test_ask_for_changes_needs_a_note(world: World) -> None:
    rid = spec_card(world)
    r = world.answer(rid, "spec_review", "plan-c1", "changes", note="   ", back="/features/c1")
    assert said(r) == "note_needed" and world.bus.sent == []
    assert answers.NOTE_NEEDED in page(world, r.headers["location"])
    assert said(world.answer(rid, "spec_review", "plan-c1", "changes", note="x" * 2001)) == "note_too_long"
    assert world.bus.sent == []


@pytest.mark.parametrize(("note", "notes"), [("", "reject"), ("too big for now", "reject: too big for now")])
def test_stop_this_request_sends_the_reject_word(world: World, note: str, notes: str) -> None:
    rid = spec_card(world)
    assert said(world.answer(rid, "spec_review", "plan-c1", "stop", note=note)) == "sent"
    _, env = one_sent(world)
    assert env["payload"]["decision"] == "reject" and env["payload"]["notes"] == notes


@pytest.mark.parametrize(("answer", "decision"), [("no_sign_in", "approve"), ("needs_sign_in", "reject")])
def test_sign_in_question_buttons_say_what_they_mean(world: World, answer: str, decision: str) -> None:
    rid = sign_in_card(world)
    body = page(world, "/features/c2")
    card = body[body.index('id="card-plan-c2-qa-pass-bars-auth-confirm-0"'):]
    card = card[:card.index("</article>")]
    labels = re.findall(r'<button type="submit" name="answer" value="([^"]+)"[^>]*>([^<]+)</button>', card)
    assert labels == [("no_sign_in", "No sign-in needed"), ("needs_sign_in", "It needs sign-in")]
    assert "each user signs in to see their notes" in card
    assert said(world.answer(rid, "sign_in", "plan-c2", answer)) == "sent"
    subject, env = one_sent(world)
    assert subject == "agents.approval.forge.plan-c2.response"
    payload = check_envelope(env, "c2")
    assert payload["decision"] == decision and payload["notes"] is None and payload["request_id"] == rid


def test_build_gate_start(world: World) -> None:
    rid = gate_card(world)
    assert "Start the build" in page(world, "/features/FEAT-0003")
    assert said(world.answer(rid, "build_gate", "b3", "start")) == "sent"
    subject, env = one_sent(world)
    assert subject == "agents.approval.forge.b3.response"
    payload = check_envelope(env, "c3")
    assert payload["request_id"] == rid and payload["decision"] == "approve" and payload["notes"] is None


def test_build_gate_cancel_with_a_reason(world: World) -> None:
    rid = gate_card(world)
    assert said(world.answer(rid, "build_gate", "b3", "cancel", note="wrong project")) == "sent"
    _, env = one_sent(world)
    assert env["payload"]["decision"] == "reject" and env["payload"]["notes"] == "wrong project"


@pytest.mark.parametrize(("answer", "decision"), [("start", "approve"), ("dont_start", "reject")])
def test_mission_plan(world: World, answer: str, decision: str) -> None:
    rid = mission_card(world)
    body = page(world, "/missions/m1")
    assert "Start the mission" in body and "Don&#39;t start it" in body
    assert "Add a search box." in body and "Highlight the matches." in body and "needs search first" in body
    assert body.index("Add a search box.") < body.index("Highlight the matches.")              # Forge's order
    assert said(world.answer(rid, "mission_plan", "mission-m1", answer, back="/missions/m1")) == "sent"
    subject, env = one_sent(world)
    assert subject == "agents.approval.forge.mission-m1.response"
    payload = check_envelope(env, "lin-m-m1-1")
    assert payload["decision"] == decision and payload["request_id"] == rid


def test_a_mission_plan_not_in_the_copy_is_answered_in_slack(world: World) -> None:
    rid = mission_card(world, plan=False)
    body = page(world, "/missions/m1")
    assert "Start the mission" not in body and "answer this one in Slack" in body
    assert said(world.answer(rid, "mission_plan", "mission-m1", "start")) == "slack"
    assert world.bus.sent == []


@pytest.mark.parametrize(("answer", "decision"), [("merge", "approve"), ("decline", "reject")])
def test_merge_card(world: World, answer: str, decision: str) -> None:
    rid = merge_card(world)
    body = page(world, "/features/FEAT-0001")
    assert "Merge and deploy" in body and "Decline" in body and "Rebuild the preview" in body
    assert f'href="{PREVIEW}"' in body and f'href="{REPORT}"' in body               # beside its evidence
    assert said(world.answer(rid, "merge", "merge-FEAT-0001", answer)) == "sent"
    subject, env = one_sent(world)
    assert subject == OFFER_SUBJECT + ".response"
    payload = check_envelope(env, "c1")
    assert payload["request_id"] == "merge-b1" and payload["decision"] == decision and payload["notes"] is None


def test_a_merge_offer_without_its_correlation_id_is_not_answered_here(world: World) -> None:
    world.fake.planning("c1", "Search box.")
    world.fake.build("b1", "FEAT-0001", "c1")
    world.fake.stage("b1", "the merge word", "merge_deploy_offer", "GATED", 81,
                     {"merge_offer": {**offer(), "correlation_id": None}})
    world.refresh()
    assert "Merge and deploy" not in page(world, "/features/FEAT-0001")
    assert said(world.answer("merge-b1", "merge", "merge-FEAT-0001", "merge")) == "slack"
    assert world.bus.sent == []


def test_the_older_checkpoint_is_answered_in_slack(world: World) -> None:
    world.fake.planning("c9", "An old request.", state="PAUSED", end=None)
    world.fake.conn.execute("UPDATE planning_runs SET pending_approval_request_id='plan-c9:product_docs:0'")
    world.fake.event("c9", "product_docs", "GATED", 5)
    world.refresh()
    body = page(world, "/features/c9")
    assert "Assumptions checkpoint" in body and "answer this one in Slack" in body
    assert '<button type="submit" name="answer"' not in body
    assert said(world.answer("plan-c9:product_docs:0", "checkpoint", "plan-c9", "approve")) == "slack"
    assert world.bus.sent == []


def test_the_envelope_is_a_valid_nats_core_envelope(world: World) -> None:
    nats_core = pytest.importorskip("nats_core", reason="nats-core is not installed beside the board")
    rid = merge_card(world)
    world.answer(rid, "merge", "merge-FEAT-0001", "merge")
    _, env = one_sent(world)
    events = pytest.importorskip("nats_core.events")
    parsed = nats_core.MessageEnvelope.model_validate(env)
    assert parsed.correlation_id == "c1"
    events.ApprovalResponsePayload.model_validate(parsed.payload)


# --- the Now page ------------------------------------------------------------------------------------------


def test_needs_you_shows_each_open_card_with_its_age_and_buttons(world: World) -> None:
    merge_card(world)
    sign_in_card(world)
    mission_card(world)
    body = page(world, "/")
    band = body[body.index('data-panel="p4"'):body.index('data-panel="p2"')]
    for words in ("Merge and deploy", "No sign-in needed", "Start the mission"):
        assert words in band, words
    assert band.count('class="answer-form"') >= 6
    assert "Answer here or in Slack" in band and "Decide in Slack" not in band
    assert 'value="/"' in band                                                     # the form returns to Now
    fragment = world.client.get("/fragments/p4").text
    assert "Merge and deploy" in fragment


# --- already answered, races, guards -------------------------------------------------------------------------


def test_an_already_answered_card_sends_nothing_and_names_the_answer(world: World) -> None:
    rid = merge_card(world)
    decide_merge(world, "reject", who="U0SOMEONEELSE")
    world.refresh()
    r = world.answer(rid, "merge", "merge-FEAT-0001", "merge", back="/features/FEAT-0001")
    assert said(r) == "already" and world.bus.sent == []
    body = page(world, r.headers["location"])
    assert "Nothing was sent: U0SOMEONEELSE already declined the merge at" in body
    assert "<b>U0SOMEONEELSE</b> declined the merge" in body and "Merge and deploy" not in body


def test_a_second_press_finds_the_waiting_answer(world: World) -> None:
    rid = merge_card(world)
    assert said(world.answer(rid, "merge", "merge-FEAT-0001", "merge")) == "sent"
    r = world.answer(rid, "merge", "merge-FEAT-0001", "decline", back="/features/FEAT-0001")
    assert said(r) == "taken" and len(world.bus.sent) == 1
    body = page(world, r.headers["location"])
    assert "An answer from the dashboard is waiting for the factory to record it (sent by Test Person at" in body
    assert "already answered" not in body


def test_two_presses_at_once_send_one_answer(world: World) -> None:
    rid = merge_card(world)
    world.bus.delay = 0.2
    user = ResolvedUser("tester", "Test Person", "operator", APPROVER)
    other = ResolvedUser("james", "James", "operator", "dashboard:james")

    async def both() -> list[answers.Outcome]:
        return list(await asyncio.gather(*(
            answers.submit(db_path=world.store, store=world.answers_db, bus=world.bus, user=u,
                           decided_by=u.approver_identity, request_id=rid, kind="merge", token="merge-FEAT-0001",
                           answer=a, note="") for u, a in ((user, "merge"), (other, "decline")))))

    outcomes = asyncio.run(both())
    assert sorted(o.code for o in outcomes) == ["sent", "taken"]
    assert len(world.bus.sent) == 1


def test_guards(world: World, config_dir: Path) -> None:
    rid = merge_card(world)
    # Another site's page.
    r = world.answer(rid, "merge", "merge-FEAT-0001", "merge", headers={"Origin": "https://elsewhere.example"})
    assert r.status_code == 403
    # Not signed in.
    world.client.post("/logout", headers=ORIGIN)
    assert world.answer(rid, "merge", "merge-FEAT-0001", "merge").status_code == 401
    # A client account: no buttons ever, and the route refuses it.
    write_accounts(config_dir, [approver_row(), client_account_row()])
    login(world.client, "client-tester", "client-password-not-real")
    assert world.answer(rid, "merge", "merge-FEAT-0001", "merge").status_code == 403
    assert "Merge and deploy" not in world.client.get("/").text
    world.client.post("/logout", headers=ORIGIN)
    # An operator with no approver identity sees the card, no buttons, and is refused.
    write_accounts(config_dir, [account_row(), client_account_row()])
    login(world.client)
    body = page(world, "/features/FEAT-0001")
    assert "Merge card" in body and "Merge and deploy" not in body and "Decide in Slack" in body
    assert world.answer(rid, "merge", "merge-FEAT-0001", "merge").status_code == 403
    assert world.bus.sent == []


def test_the_size_check_comes_before_the_answer_route(world: World) -> None:
    rid = merge_card(world)
    r = world.answer(rid, "merge", "merge-FEAT-0001", "merge", note="x" * 40_000)
    assert r.status_code == 413 and world.bus.sent == []


def test_a_card_that_moved_on_sends_nothing_and_shows_the_current_card(world: World) -> None:
    old = gate_card(world)
    new = gate_card(world, attempt=1)               # deferred: a new card on the same build
    r = world.answer(old, "build_gate", "b3", "start", back="/features/FEAT-0003")
    assert said(r) == "moved" and world.bus.sent == []
    assert parse_qs(urlsplit(r.headers["location"]).query)["card"][0] == new
    body = page(world, r.headers["location"])
    assert answers.MOVED in body and f'value="{new}"' in body and f'value="{old}"' not in body


# --- a stale copy, an unreachable bus ------------------------------------------------------------------------


def _stale(w: World, how: str) -> None:
    now = datetime.now(UTC)
    if how == "stopped feed":
        write_heartbeat(w.feed, now - timedelta(minutes=5))
    elif how == "failed read":
        mirror_once(w.feed, w.fake.path.parent / "gone.db", now)
    else:  # a copy older than the limit, with the feed alive
        w.feed.execute("UPDATE mirror_status SET last_success_at=? WHERE source='forge'",
                       (iso(now - timedelta(seconds=90)),))


@pytest.mark.parametrize("how", ["stopped feed", "failed read", "too old"])
def test_answers_pause_on_a_stale_copy(world: World, how: str) -> None:
    rid = merge_card(world)
    _stale(world, how)
    r = world.answer(rid, "merge", "merge-FEAT-0001", "merge", back="/features/FEAT-0001")
    assert said(r) == "stale" and world.bus.sent == []
    assert answers.STALE.replace("'", "&#39;") in page(world, r.headers["location"])
    assert claims.read_all(world.answers_db) == ({}, {})


def test_an_unreachable_bus_says_so_and_clears_the_claim(world: World) -> None:
    rid = merge_card(world)
    world.bus.fail = True
    r = world.answer(rid, "merge", "merge-FEAT-0001", "merge", back="/features/FEAT-0001")
    assert said(r) == "bus"
    assert answers.BUS_DOWN.replace("'", "&#39;") in page(world, r.headers["location"])
    assert claims.read_all(world.answers_db)[0] == {}
    world.bus.fail = False
    assert said(world.answer(rid, "merge", "merge-FEAT-0001", "merge")) == "sent"   # free to try again


# --- confirmation against the record -------------------------------------------------------------------------


def test_sent_then_recorded(world: World) -> None:
    rid = merge_card(world)
    world.answer(rid, "merge", "merge-FEAT-0001", "merge")
    world.refresh(2)
    body = page(world, "/features/FEAT-0001")
    assert answers.SENT in body and "Merge and deploy" not in body       # no second answer from here
    decide_merge(world, "approve", who=APPROVER)
    world.refresh()
    body = page(world, "/features/FEAT-0001")
    assert "<b>Test Person</b> approved the merge" in body and answers.SENT not in body
    assert claims.read_all(world.answers_db)[0] == {}


def test_still_open_two_passes_after_the_send_is_not_recorded(world: World) -> None:
    rid = merge_card(world)
    world.answer(rid, "merge", "merge-FEAT-0001", "merge")
    world.refresh(2)
    assert answers.SENT in page(world, "/features/FEAT-0001")
    world.refresh()
    body = page(world, "/features/FEAT-0001")
    assert answers.NOT_RECORDED.replace("'", "&#39;") in body and "Merge and deploy" in body   # may try again
    assert said(world.answer(rid, "merge", "merge-FEAT-0001", "merge")) == "sent"


def test_a_stopped_feed_never_declares_an_answer_unrecorded(world: World) -> None:
    rid = merge_card(world)
    world.answer(rid, "merge", "merge-FEAT-0001", "merge")
    world.refresh(5)
    write_heartbeat(world.feed, datetime.now(UTC) - timedelta(minutes=5))    # the feed stops
    claims.set_read_count(world.answers_db, rid, 0)                            # however many passes ago
    body = page(world, "/features/FEAT-0001")
    assert answers.SENT in body and "hasn&#39;t recorded" not in body


def test_the_page_version_moves_when_an_answer_changes_state(world: World) -> None:
    rid = merge_card(world)
    before = re.search(r'data-version="([^"]+)"', page(world, "/features/FEAT-0001"))
    world.answer(rid, "merge", "merge-FEAT-0001", "merge")
    after = re.search(r'data-version="([^"]+)"', world.client.get("/fragments/freshness").text)
    assert before and after and before.group(1) != after.group(1)


# --- what a closed card shows ------------------------------------------------------------------------------


def test_closed_cards_show_only_what_the_record_says(world: World) -> None:
    rid = spec_card(world)
    world.fake.event("c1", "feature-spec-digest-review", "timed_out", 9,
                     {"digest_review": {"outcome": "timed_out", "request_id": rid, "decided_by": None}})
    world.fake.conn.execute("UPDATE planning_runs SET state='FAILED' WHERE correlation_id='c1'")
    gate_card(world)
    world.fake.conn.execute("UPDATE builds SET status='RUNNING', pending_approval_request_id=NULL WHERE build_id='b3'")
    world.fake.mission("m2", "Archive", "lin-m-m2-1", status="REJECTED")
    world.fake.conn.execute("UPDATE missions SET plan_request_id='mission-m2:mission-plan:0', "
                            "closed_reason='rejected by you' WHERE id='m2'")
    world.refresh()
    # The recorded line is exactly what Forge recorded, and names no one.
    assert recorded_lines(page(world, "/features/c1")) == ["Closed without an answer: nobody answered in time."]
    assert recorded_lines(page(world, "/features/FEAT-0003")) == ["The card closed, and the build carried on."]
    assert recorded_lines(page(world, "/missions/m2")) == ["Not started: rejected by you."]
    assert said(world.answer(rid, "spec_review", "plan-c1", "approve")) == "already"
    assert world.bus.sent == []


def test_a_spec_answer_and_an_approved_mission_name_who_and_when(world: World) -> None:
    rid = spec_card(world)
    world.fake.event("c1", "feature-spec-digest-review", "revise", 8, {"digest_review": {
        "outcome": "revise", "request_id": rid, "decided_by": "U0SOMEONEELSE", "decision": "reject",
        "notes": "Please change: make it faster"}}, actor="U0SOMEONEELSE")
    mission_card(world)
    world.fake.conn.execute("UPDATE missions SET status='STARTING', approved_by=?, approved_at=? WHERE id='m1'",
                            (APPROVER, iso(datetime(2026, 10, 18, 9, 0, tzinfo=UTC))))
    world.refresh()
    assert "<b>U0SOMEONEELSE</b> asked for changes: “Please change: make it faster”" in page(world, "/features/c1")
    assert "<b>Test Person</b> approved the plan" in page(world, "/missions/m1")


# --- rebuilding the preview ------------------------------------------------------------------------------------


def _rebuild(w: World) -> str:
    return said(w.answer("merge-b1", "merge", "merge-FEAT-0001", "rebuild", back="/features/FEAT-0001"))


def test_rebuild_sends_defer_and_claims_nothing(world: World) -> None:
    merge_card(world)
    assert _rebuild(world) == "rebuild_sent"
    subject, env = one_sent(world)
    assert subject == OFFER_SUBJECT + ".response"
    assert env["payload"]["decision"] == "defer" and env["payload"]["notes"] == "rebuild-preview"
    assert env["correlation_id"] == "c1"
    body = page(world, "/features/FEAT-0001")
    assert "Rebuild requested." in body and "Merge and deploy" in body and "Rebuild the preview" not in body
    assert _rebuild(world) == "rebuild_waiting" and len(world.bus.sent) == 1        # one at a time
    assert said(world.answer("merge-b1", "merge", "merge-FEAT-0001", "merge")) == "sent"   # still answerable


def test_a_slow_rebuild_then_rebuilt(world: World) -> None:
    merge_card(world)
    _rebuild(world)
    world.refresh(3)
    assert "Rebuild requested." in page(world, "/features/FEAT-0001")
    amend_offer(world, 90, rebuilds=1, state="ready")
    world.refresh()
    body = page(world, "/features/FEAT-0001")
    assert "Rebuilt." in body and f'href="{PREVIEW}"' in body and "Rebuild the preview" in body


def test_a_rebuild_waiting_for_room(world: World) -> None:
    merge_card(world)
    _rebuild(world)
    amend_offer(world, 90, wanted="2026-10-19T09:00:00+00:00")
    world.refresh(6)
    body = page(world, "/features/FEAT-0001")
    assert "Waiting for room to rebuild." in body and "Can&#39;t confirm" not in body


def test_a_failed_rebuild_names_its_reason(world: World) -> None:
    merge_card(world)
    _rebuild(world)
    amend_offer(world, 90, rebuilds=1, state="failed", failure="the preview's build step failed.")
    world.refresh()
    body = page(world, "/features/FEAT-0001")
    assert "The rebuild failed: the preview&#39;s build step failed." in body
    assert f'href="{PREVIEW}"' not in body                                    # no preview link is promised


def test_a_rebuild_with_no_change_for_long_enough_cannot_be_confirmed(world: World) -> None:
    merge_card(world)
    _rebuild(world)
    world.refresh(3)
    assert "Rebuild requested." in page(world, "/features/FEAT-0001")
    world.refresh()
    body = page(world, "/features/FEAT-0001")
    assert "Can&#39;t confirm the rebuild yet" in body and "Rebuild the preview" in body
    assert _rebuild(world) == "rebuild_sent"


def test_no_rebuild_button_after_three_rebuilds(world: World) -> None:
    merge_card(world)
    amend_offer(world, 90, rebuilds=3)
    world.refresh()
    assert "Rebuild the preview" not in page(world, "/features/FEAT-0001")
    assert said(world.answer("merge-b1", "merge", "merge-FEAT-0001", "rebuild")) == "unknown"
    assert world.bus.sent == []


# --- accounts ------------------------------------------------------------------------------------------------


def _parse(rows: list[dict[str, object]]) -> dict[str, accounts.Account]:
    import yaml  # noqa: PLC0415

    return accounts.parse_accounts(yaml.safe_dump({"accounts": rows}), known_tenants={"operator", "clientco"})


def test_approver_identity_in_the_accounts_file() -> None:
    got = _parse([approver_row(), {**account_row(), "username": "james", "approver_identity": "dashboard:james"},
                  {**account_row(), "username": "watcher"}])
    assert got["tester"].approver_identity == APPROVER and got["james"].approver_identity == "dashboard:james"
    assert got["watcher"].approver_identity == ""
    with pytest.raises(accounts.ConfigError, match="client account"):
        _parse([{**client_account_row(), "approver_identity": "dashboard:client"}])
    with pytest.raises(accounts.ConfigError, match="spaces"):
        _parse([{**account_row(), "approver_identity": "two words"}])


# --- the mirror ----------------------------------------------------------------------------------------------


def test_an_older_record_without_the_new_columns_reads_as_empty(tmp_path: Path) -> None:
    forge = sqlite3.connect(tmp_path / "old.db")
    forge.executescript("""
        CREATE TABLE builds (build_id TEXT PRIMARY KEY, feature_id TEXT, repo TEXT, status TEXT, correlation_id TEXT,
                             queued_at TEXT);
        CREATE TABLE missions (id TEXT PRIMARY KEY, title TEXT, target_repo TEXT, status TEXT, correlation_id TEXT,
                               created_at TEXT);
        INSERT INTO builds VALUES ('b1', 'FEAT-1', 'acme/notes', 'PAUSED', 'c1', '2026-10-16T09:00:00+00:00');
        INSERT INTO missions VALUES ('m1', 'T', 'acme/notes', 'PROPOSED', 'x', '2026-10-16T09:00:00+00:00');
    """)
    forge.commit()
    forge.close()
    from backend import db  # noqa: PLC0415

    db.apply_schema(tmp_path / "rm.db")
    conn = open_rw(tmp_path / "rm.db")
    mirror_once(conn, tmp_path / "old.db")
    assert conn.execute("SELECT pending_approval_request_id FROM forge_builds").fetchone() == (None,)
    assert conn.execute("SELECT plan_request_id FROM forge_missions").fetchone() == (None,)
    assert conn.execute("SELECT COUNT(*) FROM forge_mission_features").fetchone() == (0,)
    assert conn.execute("SELECT read_count FROM mirror_status").fetchone() == (1,)


def test_a_store_from_before_the_new_columns_is_brought_up_to_date(tmp_path: Path) -> None:
    from backend import db  # noqa: PLC0415

    path = tmp_path / "rm.db"
    old = sqlite3.connect(path)
    # The tables as they shipped before 19 October 2026.
    old.executescript("""
        CREATE TABLE forge_builds (build_id TEXT PRIMARY KEY, feature_id TEXT NOT NULL, repo TEXT, branch TEXT,
            project TEXT, status TEXT, triggered_by TEXT, originating_user TEXT, correlation_id TEXT, queued_at TEXT,
            started_at TEXT, completed_at TEXT, error TEXT, terminal_class TEXT, mission_id TEXT, mission_role TEXT);
        CREATE TABLE forge_planning_runs (correlation_id TEXT PRIMARY KEY, state TEXT, originating_user TEXT,
            expected_approver TEXT, request_text TEXT, target_repo TEXT, triggered_by TEXT, defer_count INTEGER,
            paused_at TEXT, escalated_at TEXT, queued_at TEXT, started_at TEXT, completed_at TEXT, error TEXT);
        CREATE TABLE forge_missions (id TEXT PRIMARY KEY, title TEXT, target_repo TEXT, status TEXT,
            correlation_id TEXT, created_at TEXT, closed_at TEXT, closed_reason TEXT);
        CREATE TABLE mirror_status (source TEXT PRIMARY KEY, last_attempt_at TEXT, last_success_at TEXT,
                                    last_error TEXT, content_hash TEXT);
        INSERT INTO mirror_status (source, last_success_at) VALUES ('forge', '2026-10-18T09:00:00+00:00');
    """)
    old.close()
    db.apply_schema(path)
    db.apply_schema(path)                                   # twice, as both containers do: no error
    conn = sqlite3.connect(path)
    for table, column, _decl in db.ADDED_COLUMNS:
        assert column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}, (table, column)
    assert conn.execute("SELECT read_count FROM mirror_status").fetchone() == (0,)
    conn.close()
    # And the feed's copy then works on it.
    fake = ForgeFake(tmp_path / "forge.db")
    fake.build("b1", "FEAT-0001", "c1", status="PAUSED", completed=None)
    fake.conn.execute("UPDATE builds SET pending_approval_request_id='b1:autobuild:0'")
    feed = open_rw(path)
    assert mirror_once(feed, fake.path)
    assert feed.execute("SELECT pending_approval_request_id FROM forge_builds").fetchone() == ("b1:autobuild:0",)


def test_the_plan_tables_keep_forges_order(world: World) -> None:
    mission_card(world)
    rows = world.feed.execute("SELECT key FROM forge_mission_features ORDER BY forge_order").fetchall()
    assert [r[0] for r in rows] == ["search", "highlight"]
    needs = world.feed.execute("SELECT mission_id, key, needs_key FROM forge_mission_needs").fetchall()
    assert needs == [("m1", "highlight", "search")]


def test_answers_need_bus_settings(tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without FACTORY_DASH_NATS_URL the board has no bus: the cards show, with no buttons."""
    monkeypatch.delenv(answers.NATS_URL_ENV, raising=False)
    write_accounts(config_dir, [approver_row()])
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "Search box.")
    fake.build("b1", "FEAT-0001", "c1")
    fake.stage("b1", "the merge word", "merge_deploy_offer", "GATED", 81, {"merge_offer": offer()})
    store_from(fake, tmp_path / "readmodel.db").close()
    app = create_app(db_path=tmp_path / "readmodel.db", tenants_path=TENANTS_TEST)
    assert app.state.answer_bus is None
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        login(client)
        body = client.get("/features/FEAT-0001").text
        assert "Merge card" in body and "Merge and deploy" not in body and answers.OFF in body
        r = client.post("/cards/answer", data={"request_id": "merge-b1", "answer": "merge"}, headers=ORIGIN)
        assert r.status_code == 503
    monkeypatch.setenv(answers.NATS_URL_ENV, "nats://127.0.0.1:4222")
    bus = answers.bus_from_env(config_dir)
    assert bus is not None and bus.password_file == config_dir / answers.PASSWORD_FILE_NAME and not bus.ready()
    (config_dir / answers.PASSWORD_FILE_NAME).write_text("not-a-real-password\n", encoding="utf-8")
    assert bus.ready()


# --- the real bus client against a throwaway nats-server (skipped where there is none) -----------------------


def _free_port() -> int:
    import socket  # noqa: PLC0415

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def nats_server(tmp_path: Path) -> Iterator[tuple[str, int, Path]]:
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import time  # noqa: PLC0415

    binary = shutil.which("nats-server") or str(Path.home() / ".local" / "bin" / "nats-server")
    if not Path(binary).exists():
        pytest.skip("nats-server is not on this machine")
    port, monitor = _free_port(), _free_port()
    password = tmp_path / "pw"
    password.write_text("test-password-not-real\n", encoding="utf-8")
    conf = tmp_path / "nats.conf"
    conf.write_text(f"""
listen: 127.0.0.1:{port}
http: 127.0.0.1:{monitor}
authorization {{
  users = [
    {{ user: dashboard_approve, password: "test-password-not-real",
       permissions: {{ publish: "agents.approval.forge.*.response", subscribe: "_INBOX.>" }} }}
    {{ user: watcher, password: "watcher-not-real", permissions: {{ subscribe: ">" }} }}
  ]
}}
""", encoding="utf-8")
    proc = subprocess.Popen([binary, "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 10
    import socket  # noqa: PLC0415

    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            assert time.monotonic() < deadline, "nats-server did not start"
            time.sleep(0.05)
    try:
        yield f"nats://127.0.0.1:{port}", monitor, password
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_the_bus_client_connects_publishes_flushes_and_closes(nats_server: tuple[str, int, Path]) -> None:
    import urllib.request  # noqa: PLC0415

    import nats  # noqa: PLC0415

    url, monitor, password = nats_server
    bus = answers.NatsBus(url, password)
    subject = "agents.approval.forge.merge-FEAT-0001.response"

    async def run() -> bytes:
        watcher = await nats.connect(servers=[url], user="watcher", password="watcher-not-real")
        got = await watcher.subscribe(subject)
        await watcher.flush()
        await bus.publish(subject, b'{"hello": 1}')
        msg = await got.next_msg(timeout=5)
        await watcher.close()
        return bytes(msg.data)

    assert asyncio.run(run()) == b'{"hello": 1}'
    with urllib.request.urlopen(f"http://127.0.0.1:{monitor}/connz", timeout=5) as r:
        connz = json.loads(r.read())
    assert connz["num_connections"] == 0                     # no lasting connection is held


def test_the_bus_client_fails_quickly_when_the_bus_is_not_there(tmp_path: Path) -> None:
    import time  # noqa: PLC0415

    (tmp_path / "pw").write_text("x", encoding="utf-8")
    bus = answers.NatsBus(f"nats://127.0.0.1:{_free_port()}", tmp_path / "pw")
    started = time.monotonic()
    with pytest.raises(Exception):  # noqa: B017 (any failure is "the bus can't be reached")
        asyncio.run(bus.publish("agents.approval.forge.x.response", b"{}"))
    assert time.monotonic() - started < answers.CONNECT_TIMEOUT_SECS + 3
