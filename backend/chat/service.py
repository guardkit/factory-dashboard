"""The chat's core: `Chat.ask(question, user) -> Answer(text, refs)` (design 2026-10-17, requirement 9).

The route (`POST /chat`) and the voice endpoint (part E) both call this. One question is answered
at a time across the process, and at most 30 an hour across every account; more are refused with
a plain sentence. Each question gets at most two model calls (the choice, and one more after a
clarifying answer), each at most 300 output tokens, inside a 60-second limit for the question.

How an answer is made: the model chooses a query and its arguments, or asks one clarifying
question (`grounding.parse_reply` holds it to that). The server runs the query
(`backend/chat/tools.py`, the pages' own read layer) and writes the answer from that query's fixed
template (`backend/chat/answers.py`), filled only with the returned fields. The answer's figures
are checked against the fields once more before it is shown.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from backend.auth import ResolvedUser
from backend.chat import answers, grounding, llm, registry, tools
from backend.chat.tools import PAGES, Ref, Result
from backend.readmodel import viewmodels as vm

log = logging.getLogger("factory_dashboard.chat")

QUESTIONS_PER_HOUR = 30
QUESTION_SECONDS = 60.0
MAX_QUESTION_CHARS = 500

BUSY = "Another question is being answered just now. Please try again in a moment."
LIMITED = ("The chat has reached its limit of questions for this hour, which keeps the factory's model free "
           "for building. Please try again later.")
NOT_SET_UP = "The chat isn't switched on here: the dashboard has no key for the factory's model."
OPERATORS_ONLY = "The chat is for operator accounts only."
EMPTY = "Please type a question."
TOO_LONG = "Please ask a shorter question."
STILL_UNCLEAR = ("I still can't tell what you mean. Try naming the feature or the project as the Features page "
                 "shows it.")
READ_FAILED = "Something went wrong reading the dashboard's records, so I can't answer that just now."
NO_TIME_LEFT = "That took too long, so I stopped. Please try again in a minute."

#: Links offered with a refusal: the pages that hold what the chat can answer.
REFUSAL_REFS: tuple[Ref, ...] = (PAGES["features"], PAGES["delivered"], PAGES["issues"], PAGES["reports"])


@dataclass(frozen=True)
class FollowUp:
    """A clarifying question waiting for the user's reply: the question it was about, and what was asked."""

    question: str
    clarify: str


@dataclass(frozen=True)
class Answer:
    text: str
    refs: tuple[Ref, ...] = ()
    #: answer | clarify | refused | busy | limited | unavailable
    kind: str = "answer"
    query: str = ""
    fields: dict[str, object] | None = None
    #: set when `kind == "clarify"` and the user may reply (the second model call is still unspent)
    follow_up: FollowUp | None = None


class HourlyLimit:
    """At most `limit` questions in any rolling hour, across every account."""

    def __init__(self, limit: int = QUESTIONS_PER_HOUR, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._clock = clock
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def try_take(self) -> bool:
        with self._lock:
            now = self._clock()
            while self._times and now - self._times[0] >= 3600:
                self._times.popleft()
            if len(self._times) >= self._limit:
                return False
            self._times.append(now)
            return True


def _today(now: datetime) -> str:
    d = vm.to_london(now)
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B %Y')}"


def system_prompt(queries: dict[str, registry.QuerySpec], now: datetime) -> str:
    lines = [
        "You route questions about a software factory's dashboard to one query. You never answer "
        "the question yourself: the server runs the query and writes the answer from the records.",
        "Reply with ONE JSON object and nothing else, in one of these forms:",
        '  {"query": "<name>", "args": {"<arg>": "<text>"}}',
        '  {"clarify": "<one short question>"}',
        '  {"query": "none"}   (when no query below can answer it, e.g. "why did the planner do that?")',
        "Queries:",
    ]
    for q in queries.values():
        args = "; ".join(f"{a.name} ({'required' if a.required else 'optional'}): {a.meaning}" for a in q.args)
        lines.append(f"- {q.name}: {q.purpose}. Args: {args or 'none'}.")
    lines += [
        "Copy names from the user's words; do not invent names, dates or numbers.",
        "Ask a clarifying question only when you cannot choose a query or a required argument. It must be "
        "one short question with no digits, no feature codes and no words about status or progress.",
        f"Today is {_today(now)} (London).",
    ]
    return "\n".join(lines)


class Chat:
    def __init__(self, db_path: Path, model: llm.ChatModel | None = None, *,
                 limit: HourlyLimit | None = None, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] | None = None) -> None:
        self.db_path = db_path
        self.model: llm.ChatModel = model or llm.LiteLLMChat()
        self.limit = limit or HourlyLimit(clock=clock)
        self._clock = clock
        self._now = now or (lambda: datetime.now(UTC))
        self._one_at_a_time = threading.Lock()

    def ask(  # noqa: PLR0911 (one return per refusal)
        self, question: str, user: ResolvedUser, *, follow_up: FollowUp | None = None,
    ) -> Answer:
        """Answer one question from the dashboard's records. Never raises for a model or read failure:
        every failure is an Answer whose text says why in one sentence."""
        queries = registry.queries_for(user)
        if not queries:
            return Answer(OPERATORS_ONLY, kind="refused")
        question = " ".join((question or "").split())
        if not question:
            return Answer(EMPTY, kind="refused")
        if len(question) > MAX_QUESTION_CHARS or (follow_up and len(follow_up.question) > MAX_QUESTION_CHARS):
            return Answer(TOO_LONG, kind="refused")
        if not self.model.configured:
            return Answer(NOT_SET_UP, kind="unavailable")
        if not self._one_at_a_time.acquire(blocking=False):
            return Answer(BUSY, kind="busy")
        try:
            if not self.limit.try_take():
                return Answer(LIMITED, kind="limited")
            answer = self._answer(question, user, queries, follow_up)
            log.info("chat: %s%s", answer.kind, f" ({answer.query})" if answer.query else "")
            return answer
        finally:
            self._one_at_a_time.release()

    # --- one question ---------------------------------------------------------------------------

    def _answer(self, question: str, user: ResolvedUser, queries: dict[str, registry.QuerySpec],
                follow_up: FollowUp | None) -> Answer:
        deadline = self._clock() + QUESTION_SECONDS
        now = self._now()
        messages = [{"role": "system", "content": system_prompt(queries, now)}]
        if follow_up is not None:
            messages += [{"role": "user", "content": follow_up.question},
                         {"role": "assistant", "content": json.dumps({"clarify": follow_up.clarify})}]
        messages.append({"role": "user", "content": question})
        try:
            raw = self.model.complete(messages, timeout=deadline - self._clock())
        except llm.ModelError as exc:
            return Answer(exc.sentence, kind="unavailable")
        if self._clock() > deadline:
            return Answer(NO_TIME_LEFT, kind="unavailable")
        reply = grounding.parse_reply(raw, queries)
        user_texts = (question, follow_up.question) if follow_up else (question,)
        if isinstance(reply, grounding.Rejection):
            log.info("chat: model reply refused: %s", reply.reason)
            return Answer(answers.CANT_ANSWER, REFUSAL_REFS, kind="refused")
        if isinstance(reply, grounding.Clarify):
            if follow_up is not None:  # the second and last model call asked again
                return Answer(STILL_UNCLEAR, REFUSAL_REFS, kind="refused")
            return Answer(reply.question, kind="clarify", follow_up=FollowUp(question, reply.question))
        return self._run(reply, queries[reply.query], now, user_texts, first_round=follow_up is None,
                         original=follow_up.question if follow_up else question)

    def _run(self, choice: grounding.Choice, spec: registry.QuerySpec, now: datetime,
             user_texts: tuple[str, ...], *, first_round: bool, original: str) -> Answer:
        ctx = tools.Context(self.db_path, now)
        try:
            result = spec.run(ctx, **choice.args)
        except Exception:
            log.exception("chat: query %s failed", choice.query)
            return Answer(READ_FAILED, REFUSAL_REFS, kind="refused", query=choice.query)
        result = _echo_only_users_words(result, user_texts)
        text = answers.render(result)
        if not grounding.figures_grounded(text, result.fields):
            log.error("chat: answer for %s had figures not in its fields: %s", choice.query,
                      grounding.ungrounded_figures(text, result.fields))
            return Answer(answers.CANT_ANSWER, REFUSAL_REFS, kind="refused", query=choice.query)
        if result.status == "ambiguous":
            return Answer(text, result.candidates, kind="clarify", query=choice.query, fields=result.fields,
                          follow_up=FollowUp(original, text) if first_round else None)
        if result.status == "not_found":
            refs = _not_found_refs(result)
        elif result.status == "bad_period":
            refs = (PAGES["delivered"], PAGES["reports"])
        else:
            refs = result.refs
        return Answer(text, refs, kind="answer", query=choice.query, fields=result.fields)


def _echo_only_users_words(result: Result, user_texts: tuple[str, ...]) -> Result:
    """A not-found or ambiguous answer repeats the name searched for only when the user said it:
    the model's argument is never shown as if it were the user's words."""
    name = result.fields.get("name")
    if (result.status in ("not_found", "ambiguous") and isinstance(name, str) and name
            and not grounding.mentioned(name, *user_texts)):
        return replace(result, fields={**result.fields, "name": ""})
    return result


def _not_found_refs(result: Result) -> tuple[Ref, ...]:
    kind = result.fields.get("kind")
    return (PAGES["features"],) if kind == "feature" else (PAGES["features"], PAGES["issues"])
