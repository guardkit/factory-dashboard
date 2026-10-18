"""Grounding by construction (design 2026-10-17, requirement 9).

The model does one of two things: names a query and its arguments, or asks one short clarifying
question. `parse_reply` holds it to exactly that:

- the reply must be one JSON object, either ``{"query": <name>, "args": {...}}`` or
  ``{"clarify": "<question>"}``, with no other keys (so a reply carrying its own answer, figures or
  status in free text is refused);
- the query must be one of the registered ones, and its arguments the ones that query takes, as
  short strings; ``{"query": "none"}`` (the model saying the records cannot answer) is refused;
- a clarifying question must be one short question with no numerals (digits, Roman numerals,
  fractions: any character Python calls numeric), no number words, no money words or currency
  symbols, no feature codes and no status words, so the model cannot state a fact while asking.
  This is a plain word check, so it errs towards refusing: a refused question gets the fixed
  sentence, which is safe.

Anything else becomes a `Rejection`, which the chat answers with the one fixed sentence.

`figures_grounded` is the last check on a written answer: every run of digits in it must be a run
of digits in the query's fields. The templates hold no digits, so this only fails if a template or
a field goes wrong; the chat then refuses rather than show an unchecked figure.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from backend.chat.registry import QuerySpec

MAX_ARG_CHARS = 120
MAX_CLARIFY_CHARS = 200
MAX_REPLY_CHARS = 2000


@dataclass(frozen=True)
class Choice:
    query: str
    args: dict[str, str]


@dataclass(frozen=True)
class Clarify:
    question: str


@dataclass(frozen=True)
class Rejection:
    reason: str      # for the log and the tests; never shown to the user


Reply = Choice | Clarify | Rejection

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_THINK_RE = re.compile(r"^<think>.*?</think>\s*", re.DOTALL)
_CODE_RE = re.compile(r"(?i:\bfeat-)|\b[A-Z]{2,}-[A-Z0-9]+\b")
_STATUS_WORDS = (
    "delivered", "deliver", "delivering", "merged", "merge", "merging", "fail", "fails", "failed", "failing",
    "failure", "error", "errors", "errored", "crash", "crashed", "crashes", "abandoned", "abandon", "aborted",
    "stopped", "stop", "stops", "killed", "timed out", "hung", "dead", "died", "building", "built",
    "waiting", "stalled", "stuck", "cancelled", "canceled", "interrupted", "queued", "pending", "approved",
    "accepted", "signed off", "sign off", "declined", "rejected", "complete", "completed", "finished",
    "passed", "blocked", "running", "paused", "started", "planning", "planned", "late", "overdue", "delayed",
    "slipped", "behind", "ahead", "early", "on track", "at risk", "off track", "in progress", "on target",
    "done", "shipped", "deployed", "deploy", "deploying", "released", "release", "live", "rolled back",
    "reverted", "retried", "broken", "succeeded", "success", "successful",
)
_STATUS_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in _STATUS_WORDS) + r")\b", re.IGNORECASE)
#: Number words. "one" is left out on purpose: it is nearly always the pronoun ("the follow-up one",
#: "which one"), and every other count it could state is a number word refused here.
_NUMBER_WORDS = (
    "zero", "nil", "none", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
    "twenty", "thirty", "forty", "fourty", "fifty", "sixty", "seventy", "eighty", "ninety",
    "hundred", "hundreds", "thousand", "thousands", "million", "millions", "billion", "billions",
    "dozen", "dozens", "half", "halves", "quarter", "double", "twice", "triple", "percent", "per cent",
)
_NUMBER_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in _NUMBER_WORDS) + r")\b", re.IGNORECASE)
_MONEY_WORDS = (
    "pound", "pounds", "quid", "dollar", "dollars", "euro", "euros", "pence", "penny", "cent", "cents",
    "cost", "costs", "costed", "costing", "costly", "spent", "spend", "spends", "spending", "price", "prices",
    "priced", "paid", "fee", "fees", "budget", "expensive", "cheap", "cheaper",
)
_MONEY_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in _MONEY_WORDS) + r")\b", re.IGNORECASE)


def _numeral(text: str) -> str:
    """The first numeral in `text` (a digit, a Roman numeral, a fraction…), or ""."""
    return next((c for c in text if c.isnumeric()), "")


def _currency(text: str) -> str:
    """The first currency symbol in `text` (£, $, €, ¥…), or ""."""
    return next((c for c in text if unicodedata.category(c) == "Sc"), "")


def _strip(raw: str) -> str:
    text = _THINK_RE.sub("", raw.strip())
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text


def clarify_problem(question: object) -> str:  # noqa: PLR0911 (one return per rule)
    """Why a clarifying question is refused, or "" when it is allowed."""
    if not isinstance(question, str):
        return "clarifying question is not text"
    q = " ".join(question.split())
    if not q:
        return "clarifying question is empty"
    if len(q) > MAX_CLARIFY_CHARS:
        return "clarifying question is too long"
    if not q.endswith("?") or q.count("?") != 1:
        return "clarifying question is not one question"
    if _CODE_RE.search(q):
        return "clarifying question contains a feature code"
    if c := _numeral(q):
        return f"clarifying question contains the numeral {c!r}"
    if c := _currency(q):
        return f"clarifying question contains the currency symbol {c!r}"
    words = q.replace("-", " ")   # "ninety-nine", "signed-off" read as words
    if m := _NUMBER_RE.search(words):
        return f"clarifying question contains the number word {m.group(1).lower()!r}"
    if m := _MONEY_RE.search(words):
        return f"clarifying question contains the money word {m.group(1).lower()!r}"
    if m := _STATUS_RE.search(words):
        return f"clarifying question contains the status word {m.group(1).lower()!r}"
    if "<" in q or ">" in q:
        return "clarifying question contains markup"
    return ""


def _args_problem(spec: QuerySpec, args: object) -> tuple[str, dict[str, str]]:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return "args is not an object", {}
    unknown = set(args) - spec.arg_names()
    if unknown:
        return f"unknown argument(s) {sorted(unknown)} for {spec.name}", {}
    clean: dict[str, str] = {}
    for name, value in args.items():
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            return f"argument {name} is not text", {}
        text = " ".join(value.split())
        if len(text) > MAX_ARG_CHARS:
            return f"argument {name} is too long", {}
        if text:
            clean[name] = text
    missing = [a.name for a in spec.args if a.required and a.name not in clean]
    if missing:
        return f"missing argument(s) {missing} for {spec.name}", {}
    return "", clean


def parse_reply(raw: str, queries: Mapping[str, QuerySpec]) -> Reply:  # noqa: PLR0911 (one return per rule)
    """Hold the model's reply to the schema; anything else is a Rejection."""
    if not isinstance(raw, str) or not raw.strip():
        return Rejection("empty reply")
    if len(raw) > MAX_REPLY_CHARS:
        return Rejection("reply too long")
    try:
        data = json.loads(_strip(raw))
    except ValueError:
        return Rejection("reply is not JSON")
    if not isinstance(data, dict):
        return Rejection("reply is not a JSON object")
    keys = set(data)
    if keys == {"clarify"}:
        problem = clarify_problem(data["clarify"])
        return Rejection(problem) if problem else Clarify(" ".join(str(data["clarify"]).split()))
    if "query" not in keys or not keys <= {"query", "args"}:
        return Rejection(f"reply has the wrong keys {sorted(keys)}")
    name = data["query"]
    if not isinstance(name, str):
        return Rejection("query name is not text")
    if name == "none":
        return Rejection("the model says the records cannot answer this")
    spec = queries.get(name)
    if spec is None:
        return Rejection(f"unknown query {name!r}")
    problem, args = _args_problem(spec, data.get("args"))
    if problem:
        return Rejection(problem)
    return Choice(name, args)


# --- the last check on a written answer ---------------------------------------------------------------

_RUN_RE = re.compile(r"\d+")


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)
    elif value is not None:
        yield str(value)


def ungrounded_figures(text: str, fields: Mapping[str, object]) -> list[str]:
    """Digit runs in the answer that are not digit runs of any field."""
    known = {run for s in _strings(fields) for run in _RUN_RE.findall(s)}
    return [run for run in _RUN_RE.findall(text) if run not in known]


def figures_grounded(text: str, fields: Mapping[str, object]) -> bool:
    return not ungrounded_figures(text, fields)


def mentioned(name: str, *texts: str) -> bool:
    """True when `name` (as words) appears in one of the user's own texts, so it may be echoed."""
    want = " ".join(re.findall(r"[a-z0-9]+", name.lower()))
    return bool(want) and any(want in " ".join(re.findall(r"[a-z0-9]+", t.lower())) for t in texts)
