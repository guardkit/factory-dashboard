"""Answering the factory's cards from the dashboard (design of 19 October 2026, ADR-DASH-007).

This module is the ONLY place in the board with a bus client and a publish call (the fence tests in
tests/test_honesty_and_fences.py hold that). The feed never imports it and never holds its
credential. It publishes one thing: the same ``ApprovalResponsePayload`` that Jarvis and the Linear
door send, wrapped in a ``MessageEnvelope`` (``source_id`` "dashboard", ``event_type``
approval_response, the card's own correlation id), on the card's own response subject
``agents.approval.forge.<token>.response``, by a plain core-NATS publish and flush. No JetStream.

Forge still decides: it checks the identity, keeps "first answer wins" and refuses stale answers.
The page shows what Forge recorded, never what was sent.

How one answer goes (``submit``):

1. In ONE snapshot of the board's copy of the record: the copy must be fresh (its freshness known,
   the last successful read at most 60 seconds old, the feed running, the last read not failed),
   and the card must still be open under the request id the page showed. If it was answered, the
   person is told who answered and when, and nothing is sent; if the record has moved on (a newer
   attempt), nothing is sent and the page shows the current card.
2. The card's request id is claimed in the board's own store (backend/claims.py). A second press,
   or a second person, finds the claim and is told an answer is waiting. Nothing is sent twice.
3. The answer is published. The board holds no lasting bus connection: it connects (as the
   ``dashboard_approve`` user, its password read from a file named in a setting and never logged),
   publishes, flushes and closes, so a bus restart never leaves it holding a dead connection. If
   the bus cannot be reached, the person is told so in one sentence and the claim is cleared.

"Rebuild the preview" is not an answer: it claims nothing, so Merge and Decline stay available, and
it keeps its own per-offer mark instead (claims.ask_rebuild).

Settings: ``FACTORY_DASH_NATS_URL`` (the bus, for example ``nats://127.0.0.1:4222``; empty switches
answering off) and ``FACTORY_DASH_APPROVE_PASSWORD_FILE`` (default: ``dashboard_approve_nats_password``
in the configuration folder).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import nats

from backend import claims
from backend.auth import ResolvedUser
from backend.readmodel import cards as cardmodel
from backend.readmodel import dbread
from backend.readmodel import viewmodels as vm
from backend.readmodel.factory import load_factory
from backend.suggestions import clean_text

log = logging.getLogger("factory_dashboard.answers")

# --- settings ----------------------------------------------------------------------------------------

BUS_USER = "dashboard_approve"
NATS_URL_ENV = "FACTORY_DASH_NATS_URL"
PASSWORD_FILE_ENV = "FACTORY_DASH_APPROVE_PASSWORD_FILE"
PASSWORD_FILE_NAME = "dashboard_approve_nats_password"
SOURCE_ID = "dashboard"
EVENT_TYPE = "approval_response"
CONNECT_TIMEOUT_SECS = 5.0
FLUSH_TIMEOUT_SECS = 5.0
#: An answer is sent only from a copy of the record read at most this long ago.
FRESH_FOR_SECS = 60
MAX_NOTE_CHARS = 2000

#: The only subjects this module ever publishes on: one card's response subject.
RESPONSE_SUBJECT = re.compile(r"agents\.approval\.forge\.[^.*>\s]{1,200}\.response")

# Every sentence a person reads after pressing a button.
SENT = "Sent. Waiting for the factory to record it."
WAITING = "An answer from the dashboard is waiting for the factory to record it (sent by {who} at {when})."
NOT_RECORDED = "The factory hasn't recorded this answer. It may have refused it."
STALE = "The factory's record is out of date, so answers are paused. Try again in a minute."
BUS_DOWN = "The answer could not be sent because the factory's message bus can't be reached, so please try again."
NOTE_NEEDED = "Please write what should change, then press Ask for changes again."
NOTE_TOO_LONG = f"Please keep the note under {MAX_NOTE_CHARS} characters."
MOVED = "Nothing was sent: this card has changed since the page was shown. The current card is shown here."
GONE = "Nothing was sent: that card is not in the factory's record."
UNKNOWN = "That answer was not recognised, so nothing was sent."
SLACK_ONLY = "Nothing was sent: answer this one in Slack."
CANT_SAVE = "The answer could not be sent just now, so please try again in a minute."
OFF = "Answering from the dashboard is switched off here."
NO_IDENTITY = "This account can't answer the factory's cards: it has no approver identity."
CROSS_SITE = "This came from another website, so it was refused."
REBUILD_WAITING = "A rebuild of this preview is already waiting."


# --- the bus ---------------------------------------------------------------------------------------------


class Bus(Protocol):
    """Where an answer goes. Raises when it could not be delivered to the broker."""

    async def publish(self, subject: str, data: bytes) -> None: ...


def check_subject(subject: str) -> str:
    if not RESPONSE_SUBJECT.fullmatch(subject):
        raise ValueError("the dashboard publishes only on agents.approval.forge.<card>.response")
    return subject


class NatsBus:
    """One connection per answer: connect, publish, flush, close (design addition of 19 October 2026).

    The password is read from its file at each answer, so a rotated password needs no restart; it is
    never logged or shown."""

    def __init__(self, url: str, password_file: Path) -> None:
        self.url = url
        self.password_file = password_file

    def ready(self) -> bool:
        return bool(self.url) and self.password_file.is_file()

    def _read_credential(self) -> str:
        """The bus user's password, from its file (never logged, never shown)."""
        return self.password_file.read_text(encoding="utf-8").strip()

    async def publish(self, subject: str, data: bytes) -> None:
        check_subject(subject)
        credential = self._read_credential()

        async def quiet(*_: object) -> None:  # the client's own error logging could name the server
            return None

        # The client keeps retrying a first connection that fails, whatever its settings say, so the
        # whole attempt is bounded here: about 5 seconds, then "the bus can't be reached".
        nc = await asyncio.wait_for(nats.connect(
            servers=[self.url], user=BUS_USER, password=credential, name="factory-dashboard",
            connect_timeout=CONNECT_TIMEOUT_SECS, allow_reconnect=False, max_reconnect_attempts=0,
            error_cb=quiet, disconnected_cb=quiet, closed_cb=quiet,
        ), timeout=CONNECT_TIMEOUT_SECS)
        try:
            await nc.publish(subject, data)  # core publish: AGENTS acknowledges nothing
            await asyncio.wait_for(nc.flush(), timeout=FLUSH_TIMEOUT_SECS)
        finally:
            await nc.close()


def bus_from_env(config_dir: Path) -> NatsBus | None:
    url = os.environ.get(NATS_URL_ENV, "").strip()
    if not url:
        return None
    raw = os.environ.get(PASSWORD_FILE_ENV, "").strip()
    return NatsBus(url, Path(raw) if raw else config_dir / PASSWORD_FILE_NAME)


def bus_ready(bus: Bus | None) -> bool:
    if bus is None:
        return False
    ready = getattr(bus, "ready", None)
    return bool(ready()) if callable(ready) else True


async def _send(bus: Bus, subject: str, data: bytes) -> None:
    await bus.publish(check_subject(subject), data)


# --- what each button sends ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    decision: str
    notes: str | None
    dispositions: list[dict[str, object]] | None = None


class Refused(Exception):
    """The answer is refused before anything is sent; the message is the person's sentence."""


def _note(raw: str) -> str:
    text = clean_text(raw or "")
    if len(text) > MAX_NOTE_CHARS:
        raise Refused(NOTE_TOO_LONG)
    return text


def _approve_spec(card: cardmodel.Card, _text: str) -> Answer:
    # Every assumption the card lists is accepted, as the Linear door does.
    dispositions: list[dict[str, object]] = [
        {"assumption_id": a, "disposition": "accepted", "edit_delta": None, "notes": None}
        for a in card.assumption_ids]
    return Answer("approve", None, dispositions or None)


def _ask_for_changes(_card: cardmodel.Card, text: str) -> Answer:
    if not text:
        raise Refused(NOTE_NEEDED)
    # Forge reads a note whose first word is "reject" as "cancel this run" (driver.py
    # `_reject_word_split`); the fixed prefix makes the person's words always mean "revise from this".
    return Answer("reject", f"Please change: {text}")


def _fixed(decision: str, notes: str | None = None) -> Callable[[cardmodel.Card, str], Answer]:
    return lambda _card, _text: Answer(decision, notes)


#: What each button sends: (card kind, button) -> the decision Forge accepts, with its exact notes.
DECISIONS: dict[tuple[str, str], Callable[[cardmodel.Card, str], Answer]] = {
    (cardmodel.SPEC_REVIEW, "approve"): _approve_spec,
    (cardmodel.SPEC_REVIEW, "changes"): _ask_for_changes,
    (cardmodel.SPEC_REVIEW, "stop"): lambda _c, text: Answer("reject", f"reject: {text}" if text else "reject"),
    # On the sign-in card approve means "no sign-in is needed" and reject means "it needs sign-in".
    (cardmodel.SIGN_IN, "no_sign_in"): _fixed("approve"),
    (cardmodel.SIGN_IN, "needs_sign_in"): _fixed("reject"),
    (cardmodel.BUILD_GATE, "start"): _fixed("approve"),
    (cardmodel.BUILD_GATE, "cancel"): lambda _c, text: Answer("reject", text or None),
    (cardmodel.MISSION_PLAN, "start"): _fixed("approve"),
    (cardmodel.MISSION_PLAN, "dont_start"): _fixed("reject"),
    (cardmodel.MERGE, "merge"): _fixed("approve"),
    (cardmodel.MERGE, "decline"): _fixed("reject"),
    # Not an answer: Forge rebuilds the preview and the card stays open (merge_executor.py ~7418-7438).
    (cardmodel.MERGE, "rebuild"): _fixed("defer", cardmodel.REBUILD_NOTE),
}


def decide(card: cardmodel.Card, answer: str, note: str) -> Answer:
    """The decision Forge accepts for one button, with its exact notes (design, "Which cards")."""
    chosen = DECISIONS.get((card.kind, answer))
    if chosen is None or (answer == "rebuild" and (card.rebuild is None or not card.rebuild.offered)):
        raise Refused(UNKNOWN)
    return chosen(card, _note(note))


def envelope(card: cardmodel.Card, answer: Answer, decided_by: str, now: datetime | None = None) -> dict[str, object]:
    """The MessageEnvelope (nats-core) around one ApprovalResponsePayload, as plain JSON."""
    now = now or datetime.now(UTC)
    return {
        "message_id": str(uuid.uuid4()),
        "timestamp": now.astimezone(UTC).isoformat(),
        "version": "1.0",
        "source_id": SOURCE_ID,
        "event_type": EVENT_TYPE,
        "project": None,
        "correlation_id": card.correlation_id,
        "payload": {
            "request_id": card.request_id,
            "decision": answer.decision,
            "decided_by": decided_by,
            "notes": answer.notes,
            "dispositions": answer.dispositions,
        },
    }


# --- freshness and the same-snapshot check ---------------------------------------------------------------


def copy_is_fresh(fr: dbread.Freshness, now: datetime) -> bool:
    """Answers are sent only from a copy that is known to be current: freshness known, the last
    successful read at most 60 seconds old, the feed running and its last read not failed."""
    if fr.read_at is None or fr.heartbeat_at is None or not fr.feed_alive(now) or fr.error:
        return False
    return (now - fr.read_at).total_seconds() <= FRESH_FOR_SECS


@dataclass(frozen=True)
class Outcome:
    code: str                 # sent | rebuild_sent | taken | rebuild_waiting | already | moved | gone | stale |
                              # bus | refused | slack | cant_save
    request_id: str           # the card the page should show (the current one when the record moved on)
    sentence: str = ""        # for "refused": the plain sentence


def _check(db_path: Path, request_id: str, kind: str, token: str, now: datetime
           ) -> tuple[cardmodel.Card | None, Outcome | None, int]:
    """In ONE snapshot: freshness, then the card's current request id. (card, None, read count) when
    it may be answered, otherwise (None, the outcome, 0)."""
    conn = dbread.snapshot(db_path)
    try:
        fr = dbread.freshness(conn)
        if not copy_is_fresh(fr, now):
            return None, Outcome("stale", request_id), 0
        factory = load_factory(conn, now)
        card = cardmodel.find(factory, request_id)
        if card is not None and card.open:
            if not card.answerable:
                return None, Outcome("slack", request_id), 0
            return card, None, fr.read_count
        newer = cardmodel.current(factory, kind, token) if kind and token else None
        if newer is not None:
            return None, Outcome("moved", newer.request_id), 0
        if card is not None:
            return None, Outcome("already", request_id), 0
        return None, Outcome("gone", request_id), 0
    finally:
        conn.close()


def _read_count(db_path: Path) -> int:
    conn = dbread.snapshot(db_path)
    try:
        return dbread.freshness(conn).read_count
    finally:
        conn.close()


async def submit(  # noqa: PLR0911, PLR0912 (one return per outcome)
        *, db_path: Path, store: Path, bus: Bus, user: ResolvedUser, decided_by: str,
                 request_id: str, kind: str, token: str, answer: str, note: str,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> Outcome:
    """Check, claim, publish (see the module docstring). Never raises for an expected refusal."""
    try:
        card, refused, read_count = await asyncio.to_thread(_check, db_path, request_id, kind, token, now())
    except sqlite3.Error as exc:
        log.warning("answers: the record could not be read (%s)", exc)
        return Outcome("stale", request_id)
    if refused is not None:
        return refused
    assert card is not None
    try:
        chosen = decide(card, answer, note)
    except Refused as exc:
        return Outcome("refused", request_id, str(exc))
    rebuild = answer == "rebuild"
    try:
        if rebuild:
            assert card.rebuild is not None
            ok, _mark = await asyncio.to_thread(claims.ask_rebuild, store, request_id=card.request_id,
                                                rebuilds=card.rebuild.count, user=user, read_count=read_count)
            if not ok:
                return Outcome("rebuild_waiting", card.request_id)
        else:
            ok, _claim = await asyncio.to_thread(claims.claim, store, request_id=card.request_id,
                                                 card_kind=card.kind, answer=answer, decision=chosen.decision,
                                                 user=user, read_count=read_count)
            if not ok:
                return Outcome("taken", card.request_id)
    except claims.Unavailable:
        return Outcome("cant_save", card.request_id)
    data = json.dumps(envelope(card, chosen, decided_by, now()), separators=(",", ":")).encode("utf-8")
    try:
        await _send(bus, card.response_subject, data)
    except Exception as exc:  # any failure to reach the bus is the same one sentence
        log.warning("answers: could not send an answer for %s (%s)", card.request_id, type(exc).__name__)
        try:
            if rebuild:
                await asyncio.to_thread(claims.drop_rebuild, store, card.request_id)
            else:
                await asyncio.to_thread(claims.release, store, card.request_id)
        except claims.Unavailable:
            pass
        return Outcome("bus", card.request_id)
    log.info("answers: sent %s for %s as %s", chosen.decision, card.request_id, user.username)
    # Passes are counted from the feed's read count just after the send (claims.NOT_RECORDED_AFTER).
    try:
        after = await asyncio.to_thread(_read_count, db_path)
        await asyncio.to_thread(claims.set_read_count, store, card.request_id, after,
                                table="rebuild_marks" if rebuild else "answer_claims")
    except (sqlite3.Error, claims.Unavailable):
        pass
    return Outcome("rebuild_sent" if rebuild else "sent", card.request_id)


# --- what the page says ---------------------------------------------------------------------------------

_CODES = frozenset({"sent", "rebuild_sent", "taken", "rebuild_waiting", "already", "moved", "gone", "stale",
                    "bus", "slack", "cant_save", "note_needed", "note_too_long", "unknown"})
#: Refusal sentences that travel as a code in the page's address (never as free text).
REFUSAL_CODES = {NOTE_NEEDED: "note_needed", NOTE_TOO_LONG: "note_too_long", UNKNOWN: "unknown"}


def known_code(code: str) -> bool:
    return code in _CODES


def when_words(at: datetime | None) -> str:
    if at is None:
        return "an unknown time"
    return vm.fmt_wall(at, now=datetime.now(UTC))


def notice(code: str, card: cardmodel.Card | None, claim: claims.Claim | None,
           name: Callable[[str], str]) -> str:
    """The sentence for an outcome, worded from the record and the claim (never from the address)."""
    if code in ("sent", "rebuild_sent"):
        return ""   # the card itself says so, from the claim or the mark, until the record answers
    if code == "already" and card is not None and card.recorded is not None:
        r = card.recorded
        if r.answered:
            return f"Nothing was sent: {name(r.who)} already {r.text} at {when_words(r.at)}."
        return f"Nothing was sent: this card has already closed. {r.text}"
    if code == "taken" and claim is not None:
        return WAITING.format(who=claim.display_name, when=when_words(claim.claimed_at))
    return {
        "rebuild_waiting": REBUILD_WAITING, "moved": MOVED,
        "gone": GONE, "stale": STALE, "bus": BUS_DOWN, "slack": SLACK_ONLY, "cant_save": CANT_SAVE,
        "note_needed": NOTE_NEEDED, "note_too_long": NOTE_TOO_LONG, "unknown": UNKNOWN,
        "taken": WAITING.format(who="someone", when="a moment ago"),
        "already": "Nothing was sent: this card has already closed.",
    }.get(code, "")
