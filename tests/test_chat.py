"""The chat (design 2026-10-17, requirement 9): queries over a fixture ledger, grounding, templates,
limits, the LiteLLM call (stubbed) and the route."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from backend.app import create_app
from backend.auth import ResolvedUser
from backend.chat import answers, grounding, llm, registry, tools
from backend.chat.service import (
    BUSY,
    LIMITED,
    NOT_SET_UP,
    OPERATORS_ONLY,
    QUESTION_SECONDS,
    QUESTIONS_PER_HOUR,
    REFUSAL_REFS,
    STILL_UNCLEAR,
    Answer,
    Chat,
    FollowUp,
    HourlyLimit,
)
from backend.readmodel import dbread
from starlette.testclient import TestClient

from tests import forge_fake
from tests.conftest import LOOPBACK_URL, login
from tests.forge_fake import ForgeFake, delivered_feature, store_from

TENANTS_TEST = Path(__file__).resolve().parent / "fixtures" / "tenants_test.yaml"
NOW = forge_fake.T0 + timedelta(hours=4)          # Friday 16 October 2026, 14:00 London
OPERATOR = ResolvedUser("tester", "Test Person", "operator")
CLIENT = ResolvedUser("client-tester", "Client Person", "clientco")


# --- the fixture ledger ------------------------------------------------------------------------------


def _ledger(tmp_path: Path) -> Path:
    """Two delivered features, one declined, one building, one waiting for the merge decision, a
    failed planning run, a sentence sent twice, and three projects (two of them named "notes")."""
    fake = ForgeFake(tmp_path / "forge.db")
    delivered_feature(fake, 1, text="Search box that filters notes by title. It updates as you type.")
    fake.planning("c2", "Show a Clear button.")
    fake.build("b2", "FEAT-0002", "c2")
    fake.merge_offer("b2", 81)
    fake.merge_decision("b2", 82, "reject")
    fake.planning("c3", "Count the notes <script>alert(1)</script>.")
    fake.build("b3", "FEAT-0003", "c3", status="RUNNING", queued=100, started=100, completed=None)
    fake.planning("c4", "Delete a note.")
    fake.build("b4", "FEAT-0004", "c4")
    fake.merge_offer("b4", 90)
    fake.planning("c5", "Sort the notes.", state="FAILED", error="The planner gave up. Long story.")
    fake.planning("c6", "Export notes as a file.")
    fake.build("b6", "FEAT-0006", "c6", status="FAILED", error="Tests failed on export.")
    delivered_feature(fake, 7, text="Export notes as a file.", merged_at=120)
    fake.planning("c8", "Add an API for notes.", state="RUNNING", end=None, repo="acme/notes-api", start=200)
    fake.planning("c9", "Tidy the readme.", state="FAILED", error="No readme found.", repo="other/notes")
    store = tmp_path / "readmodel.db"
    store_from(fake, store, now=NOW).close()
    return store


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return _ledger(tmp_path)


@pytest.fixture
def ctx(store: Path) -> tools.Context:
    return tools.Context(store, NOW)


# --- the queries, against the fixture ledger -----------------------------------------------------------


def _rows(r: tools.Result) -> list[dict[str, object]]:
    rows = r.fields["rows"]
    assert isinstance(rows, list)
    return rows


def test_feature_status_by_code_matches_the_feature_page(ctx: tools.Context) -> None:
    r = tools.feature_status(ctx, "feat-0001")
    assert r.status == "found"
    found = dbread.feature_page(ctx.db_path, "FEAT-0001", NOW)
    assert found is not None
    _chrome, page = found
    assert r.fields["label"] == "Search box that filters notes by title. (FEAT-0001)"
    assert r.fields["status_text"] == page.status_text
    assert r.fields["elapsed"] == page.elapsed
    assert r.fields["verdict"] == page.verdict.label
    assert r.fields["attempts"] == "1"
    assert r.fields["delivered_on"] == "16 Oct 2026"
    assert r.fields["stale_note"] == ""
    assert r.fields["cost"] == "Cost: not measured yet"
    assert [ref.href for ref in r.refs] == ["/features/FEAT-0001", "/projects/acme/notes"]


def test_feature_status_by_title_words(ctx: tools.Context) -> None:
    r = tools.feature_status(ctx, "the search box")
    assert r.status == "found"
    assert str(r.fields["label"]).startswith("Search box")
    waiting = tools.feature_status(ctx, "Delete a note")
    assert waiting.fields["status_text"] == "Waiting for the merge decision"
    assert waiting.fields["sent"] == "today at 10:00:00"


def test_a_feature_that_stopped_says_where_it_stopped(ctx: tools.Context) -> None:
    failed = answers.render(tools.feature_status(ctx, "FEAT-0006"))
    assert "It stopped at the building step." in failed and "How far" not in failed
    running = answers.render(tools.feature_status(ctx, "FEAT-0003"))
    assert "How far: building." in running


def test_feature_status_lists_every_feature_with_the_same_name(ctx: tools.Context) -> None:
    r = tools.feature_status(ctx, "export notes as a file")
    assert r.status == "ambiguous"
    assert r.fields["count"] == "2"
    assert {c.href for c in r.candidates} == {"/features/FEAT-0006", "/features/FEAT-0007"}
    text = answers.render(r)
    assert "FEAT-0006" in text and "FEAT-0007" in text and text.endswith("Which one do you mean?")


def test_feature_status_not_found_says_so(ctx: tools.Context) -> None:
    r = tools.feature_status(ctx, "a rocket launcher")
    assert r.status == "not_found"
    assert answers.render(r) == "I have no record of a feature called “a rocket launcher”."


def test_project_summary_matches_the_project_page(ctx: tools.Context) -> None:
    r = tools.project_summary(ctx, "acme/notes")
    view = dbread.project_view(ctx.db_path, "acme/notes", NOW)
    assert r.status == "found"
    assert r.fields["in_progress"] == str(view.in_progress)
    assert r.fields["delivered_7d"] == str(view.delivered_7d) == "2"
    assert r.fields["delivered_total"] == str(view.delivered_total)
    assert r.fields["open_issue_count"] == str(len(view.issues))
    with_period = tools.project_summary(ctx, "acme/notes", "this week")
    assert with_period.fields["period_delivered"] == "2"
    assert with_period.fields["period_label"] == "this week (12 Oct 2026 to 18 Oct 2026)"


def test_project_names_resolve_or_ask(ctx: tools.Context) -> None:
    both = tools.project_summary(ctx, "notes")
    assert both.status == "ambiguous"
    assert {c.label for c in both.candidates} == {"acme/notes", "other/notes"}
    assert tools.project_summary(ctx, "notes-api").fields["project"] == "acme/notes-api"
    assert tools.project_summary(ctx, "nothing here").status == "not_found"


def test_open_issues_match_the_issues_page(ctx: tools.Context) -> None:
    r = tools.open_issues(ctx)
    page = dbread.issues_view(ctx.db_path, NOW)
    assert r.fields["count"] == str(len(page.open_rows))
    assert [row["kind"] for row in _rows(r)] == [x.kind for x in page.open_rows][:tools.MAX_LISTED]
    one = tools.open_issues(ctx, "other/notes")
    assert one.fields["count"] == "1"
    assert one.fields["rows"][0]["subject"].startswith("Tidy the readme")  # type: ignore[index]


def test_delivered_matches_the_delivered_page(ctx: tools.Context) -> None:
    r = tools.delivered(ctx, "this week")
    panel = dbread.delivered_view(ctx.db_path, ("2026-10-12", "2026-10-18"), NOW).panel
    assert r.fields["count"] == str(panel.delivered_count) == "2"
    assert [row["label"] for row in _rows(r)] == [
        "Search box that filters notes by title. (FEAT-0001)", "Export notes as a file. (FEAT-0007)"]
    assert r.refs[-1].href == "/delivered?from=2026-10-12&to=2026-10-18"
    assert tools.delivered(ctx, "last week").fields["count"] == "0"
    assert tools.delivered(ctx, "this week", "other/notes").fields["count"] == "0"
    assert tools.delivered(ctx, "next fortnight").status == "bad_period"


def test_weekly_report_matches_the_reports_page(ctx: tools.Context) -> None:
    r = tools.weekly_report(ctx)
    report = dbread.weekly_report_view(ctx.db_path, "operator", ("2026-10-12", "2026-10-18"), NOW)
    assert r.fields["delivered_count"] == str(report.headline.delivered_count)
    assert r.fields["in_flight_count"] == str(report.headline.in_flight_count)
    assert r.fields["issues_opened"] == str(report.issues.opened)
    assert r.fields["plan_agreed"] == ""
    assert "No plan has been agreed yet" in answers.render(r)


def test_a_stale_record_is_said_and_the_report_withheld(store: Path) -> None:
    late = tools.Context(store, NOW + timedelta(days=1))
    assert "last read" in str(tools.feature_status(late, "FEAT-0001").fields["stale_note"])
    assert answers.render(tools.feature_status(late, "FEAT-0001")).startswith("Note: ")
    report = tools.weekly_report(late)
    assert report.fields["withheld"] == "yes"
    assert "withheld" in answers.render(report)


def test_cost_and_time_are_not_measured_yet(ctx: tools.Context) -> None:
    for run in (tools.cost_summary, tools.time_summary):
        r = run(ctx, project="acme/notes")
        assert r.status == "not_measured"
        text = answers.render(r)
        assert "isn't measured yet" in text
        assert not re.search(r"\d", text)


def test_periods() -> None:
    p = tools.resolve_period("last week", NOW)
    assert p is not None and p.window == ("2026-10-05", "2026-10-11")
    p = tools.resolve_period("week of 2026-10-14", NOW)
    assert p is not None and p.window == ("2026-10-12", "2026-10-18")
    p = tools.resolve_period("2026-10-01 to 2026-10-03", NOW)
    assert p is not None and p.window == ("2026-10-01", "2026-10-03")
    assert tools.resolve_period("2026-10-03 to 2026-10-01", NOW) is None
    assert tools.resolve_period("2026-13-40", NOW) is None
    assert tools.resolve_period("whenever", NOW) is None


# --- every template answer: no figure that is not a field ----------------------------------------------


def _every_result(ctx: tools.Context) -> list[tools.Result]:
    return [
        tools.feature_status(ctx, "FEAT-0001"), tools.feature_status(ctx, "FEAT-0003"),
        tools.feature_status(ctx, "FEAT-0004"), tools.feature_status(ctx, "FEAT-0006"),
        tools.feature_status(ctx, "sort the notes"), tools.feature_status(ctx, "export notes as a file"),
        tools.feature_status(ctx, "zzz"), tools.project_summary(ctx, "acme/notes", "last 30 days"),
        tools.project_summary(ctx, "notes"), tools.open_issues(ctx), tools.open_issues(ctx, "acme/notes-api"),
        tools.delivered(ctx), tools.delivered(ctx, "last month"), tools.delivered(ctx, "bad"),
        tools.weekly_report(ctx), tools.weekly_report(ctx, "this week", "acme/notes"),
        tools.cost_summary(ctx), tools.time_summary(ctx),
        tools.weekly_report(tools.Context(ctx.db_path, NOW + timedelta(days=2))),
    ]


def test_every_figure_in_every_answer_is_a_field(ctx: tools.Context) -> None:
    for r in _every_result(ctx):
        text = answers.render(r)
        assert text, r
        assert grounding.figures_grounded(text, r.fields), (r.query, r.status, text,
                                                            grounding.ungrounded_figures(text, r.fields))


def test_the_templates_add_no_digits_of_their_own(ctx: tools.Context) -> None:
    """Render every result again with every digit in its fields replaced by a letter: the
    answer then has no digit at all, so every digit in a real answer came from a field."""

    def scrub(value: object) -> object:
        if isinstance(value, str):
            return re.sub(r"\d", "n", value)
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    for r in _every_result(ctx):
        for count in ("0", "1", "2"):   # the templates branch on these three, so try each
            fields = {k: (count if k in _COUNT_KEYS else scrub(v)) for k, v in r.fields.items()}
            text = answers.render(tools.Result(r.query, r.status, fields))
            assert not re.search(r"\d", text.replace(count, "")), (r.query, r.status, text)


_COUNT_KEYS = {"count", "more", "more_issues", "more_delivered", "open_issue_count", "attempts",
               "delivered_count", "in_flight_count"}


def test_figure_check_catches_an_invented_number() -> None:
    assert not grounding.figures_grounded("3 features were merged", {"count": "2"})
    assert grounding.ungrounded_figures("12 Oct and 7 more", {"on": "12 Oct 2026"}) == ["7"]
    assert grounding.figures_grounded("2 features", {"rows": [{"n": "2"}]})


# --- grounding: the model's reply is held to the schema ------------------------------------------------

Q = registry.QUERIES


def test_a_well_formed_choice_and_clarify_pass() -> None:
    assert grounding.parse_reply('{"query": "delivered", "args": {"period": "last week"}}', Q) == \
        grounding.Choice("delivered", {"period": "last week"})
    assert grounding.parse_reply('```json\n{"query": "open_issues"}\n```', Q) == grounding.Choice("open_issues", {})
    assert grounding.parse_reply('{"query": "open_issues", "args": {"project": null}}', Q) == \
        grounding.Choice("open_issues", {})
    assert grounding.parse_reply('{"clarify": "Which project do you mean?"}', Q) == \
        grounding.Clarify("Which project do you mean?")
    assert isinstance(grounding.parse_reply('{"clarify": "Do you mean the follow-up one?"}', Q), grounding.Clarify)


@pytest.mark.parametrize("raw", [
    "", "Three features were delivered this week.", "[1, 2]", '"text"', "{not json",
    '{"query": "delivered", "answer": "3 features were delivered"}',
    '{"answer": "FEAT-0001 is delivered"}',
    '{"query": "delivered", "clarify": "Which week?"}',
    '{"query": "deploy_now"}', '{"query": "none"}', '{"query": 7}',
    '{"query": "delivered", "args": {"period": "this week", "colour": "green"}}',
    '{"query": "feature_status", "args": {}}',
    '{"query": "feature_status", "args": {"feature": 12}}',
    '{"query": "feature_status", "args": ["x"]}',
    '{"query": "feature_status", "args": {"feature": "' + "x" * 200 + '"}}',
])
def test_replies_outside_the_schema_are_refused(raw: str) -> None:
    assert isinstance(grounding.parse_reply(raw, Q), grounding.Rejection), raw


@pytest.mark.parametrize("question", [
    "Do you mean the one from 16 October?",          # a digit
    "Do you mean FEAT-0001?",                       # a feature code
    "Do you mean feat-2555?",
    "Do you mean the one that was delivered?",      # status words
    "Do you mean the feature that failed?",
    "Do you mean the one that is on track?",
    "Is it the one still in progress?",
    "Which project? Which week?",                   # two questions
    "Tell me which project",                        # not a question
    "Which <b>project</b>?",
    "",
])
def test_clarifying_questions_cannot_state_facts(question: str) -> None:
    reply = grounding.parse_reply(json.dumps({"clarify": question}), Q)
    assert isinstance(reply, grounding.Rejection), question


def test_queries_for_a_client_tenant_are_none() -> None:
    assert registry.queries_for(OPERATOR) == registry.QUERIES
    assert len(registry.QUERIES) == 7
    assert registry.queries_for(CLIENT) == {}


# --- the chat: a stubbed model ---------------------------------------------------------------------------


class FakeModel:
    """Stands in for LiteLLM: replies in order (a string, or a ModelError to raise)."""

    def __init__(self, *replies: str | llm.ModelError, configured: bool = True,
                 gate: threading.Event | None = None) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[dict[str, str]], float]] = []
        self._configured = configured
        self.gate = gate
        self.entered = threading.Event()

    @property
    def configured(self) -> bool:
        return self._configured

    def complete(self, messages: Sequence[dict[str, str]], timeout: float) -> str:
        self.calls.append((list(messages), timeout))
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        reply = self.replies.pop(0) if self.replies else '{"query": "none"}'
        if isinstance(reply, llm.ModelError):
            raise reply
        return reply


def _chat(store: Path, model: FakeModel, **kw: object) -> Chat:
    return Chat(store, model, now=lambda: NOW, **kw)  # type: ignore[arg-type]


def test_ask_runs_the_chosen_query_and_writes_the_template(store: Path) -> None:
    model = FakeModel('{"query": "feature_status", "args": {"feature": "search box"}}')
    a = _chat(store, model).ask("Where is the search box feature?", OPERATOR)
    assert a.kind == "answer" and a.query == "feature_status"
    assert a.text.startswith("Search box that filters notes by title. (FEAT-0001), in acme/notes: "
                             "merged and checked on 16 Oct 2026. On track.")
    assert a.refs[0].href == "/features/FEAT-0001"
    assert len(model.calls) == 1
    messages, timeout = model.calls[0]
    assert messages[0]["role"] == "system" and "feature_status" in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "Where is the search box feature?"}
    assert 0 < timeout <= QUESTION_SECONDS


def test_an_unknown_query_gets_the_plain_refusal_with_links(store: Path) -> None:
    a = _chat(store, FakeModel('{"query": "restart_the_factory"}')).ask("Restart it", OPERATOR)
    assert a.kind == "refused"
    assert a.text == answers.CANT_ANSWER
    assert a.refs == REFUSAL_REFS
    assert {r.href for r in a.refs} == {"/features", "/delivered", "/issues", "/reports"}


def test_bad_json_and_free_text_answers_are_refused(store: Path) -> None:
    for raw in ("{oops", "Two features were delivered this week.",
                '{"query": "delivered", "answer": "9 delivered"}'):
        a = _chat(store, FakeModel(raw)).ask("What was delivered?", OPERATOR)
        assert (a.kind, a.text) == ("refused", answers.CANT_ANSWER), raw
        assert "9" not in a.text


def test_a_question_outside_the_records_is_refused(store: Path) -> None:
    a = _chat(store, FakeModel('{"query": "none"}')).ask("Why did the planner do that?", OPERATOR)
    assert (a.kind, a.text) == ("refused", answers.CANT_ANSWER)


def test_model_failures_say_why_in_one_sentence(store: Path) -> None:
    for err in (llm.TIMED_OUT, llm.UNREACHABLE, llm.REFUSED):
        a = _chat(store, FakeModel(llm.ModelError(err))).ask("What was delivered?", OPERATOR)
        assert (a.kind, a.text) == ("unavailable", err)


def test_not_switched_on_without_a_key(store: Path) -> None:
    model = FakeModel(configured=False)
    a = _chat(store, model).ask("What was delivered?", OPERATOR)
    assert (a.kind, a.text) == ("unavailable", NOT_SET_UP)
    assert model.calls == []


def test_a_client_tenant_gets_no_chat(store: Path) -> None:
    model = FakeModel('{"query": "delivered"}')
    a = _chat(store, model).ask("What was delivered?", CLIENT)
    assert (a.kind, a.text) == ("refused", OPERATORS_ONLY)
    assert model.calls == []


def test_clarify_then_answer_in_at_most_two_calls(store: Path) -> None:
    model = FakeModel('{"clarify": "Which project do you mean?"}',
                      '{"query": "project_summary", "args": {"project": "acme/notes"}}')
    chat = _chat(store, model)
    first = chat.ask("How is the project doing?", OPERATOR)
    assert first.kind == "clarify" and first.text == "Which project do you mean?"
    assert first.follow_up == FollowUp("How is the project doing?", "Which project do you mean?")
    second = chat.ask("acme/notes", OPERATOR, follow_up=first.follow_up)
    assert second.kind == "answer" and second.text.startswith("acme/notes: ")
    assert len(model.calls) == 2
    turns = model.calls[1][0]
    assert [m["role"] for m in turns] == ["system", "user", "assistant", "user"]
    assert json.loads(turns[2]["content"]) == {"clarify": "Which project do you mean?"}


def test_a_second_clarifying_question_ends_the_question(store: Path) -> None:
    model = FakeModel('{"clarify": "Which project do you mean?"}', '{"clarify": "Which week do you mean?"}')
    chat = _chat(store, model)
    first = chat.ask("How is it going?", OPERATOR)
    second = chat.ask("that one", OPERATOR, follow_up=first.follow_up)
    assert (second.kind, second.text) == ("refused", STILL_UNCLEAR)
    assert second.follow_up is None
    assert len(model.calls) == 2


def test_an_ambiguous_name_lists_both_and_asks(store: Path) -> None:
    model = FakeModel('{"query": "feature_status", "args": {"feature": "export notes as a file"}}',
                      '{"query": "feature_status", "args": {"feature": "FEAT-0007"}}')
    chat = _chat(store, model)
    first = chat.ask("How is export notes as a file going?", OPERATOR)
    assert first.kind == "clarify" and first.follow_up is not None
    assert {r.href for r in first.refs} == {"/features/FEAT-0006", "/features/FEAT-0007"}
    second = chat.ask("the second one", OPERATOR, follow_up=first.follow_up)
    assert second.kind == "answer" and "(FEAT-0007)" in second.text


def test_a_not_found_name_is_echoed_only_when_the_user_said_it(store: Path) -> None:
    said = _chat(store, FakeModel('{"query": "feature_status", "args": {"feature": "rocket"}}'))
    assert "“rocket”" in said.ask("Where is the rocket?", OPERATOR).text
    invented = _chat(store, FakeModel('{"query": "feature_status", "args": {"feature": "delivered 3 times"}}'))
    a = invented.ask("Where is the rocket?", OPERATOR)
    assert a.text == "I have no record of that feature."
    assert "3" not in a.text


def test_an_answer_with_an_unchecked_figure_is_refused(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(answers.TEMPLATES, "delivered", lambda fields: "42 features were merged and checked.")
    a = _chat(store, FakeModel('{"query": "delivered"}')).ask("What was delivered?", OPERATOR)
    assert (a.kind, a.text) == ("refused", answers.CANT_ANSWER)


def test_a_read_failure_is_one_sentence(tmp_path: Path) -> None:
    a = _chat(tmp_path / "missing.db", FakeModel('{"query": "delivered"}')).ask("What was delivered?", OPERATOR)
    assert a.kind == "refused" and a.text.startswith("Something went wrong reading")


# --- limits ---------------------------------------------------------------------------------------------


def test_thirty_questions_an_hour_then_a_plain_refusal() -> None:
    clock = [1000.0]
    limit = HourlyLimit(clock=lambda: clock[0])
    assert all(limit.try_take() for _ in range(QUESTIONS_PER_HOUR))
    assert not limit.try_take()
    clock[0] += 3599
    assert not limit.try_take()
    clock[0] += 1
    assert limit.try_take()


def test_the_hourly_limit_holds_across_accounts(store: Path) -> None:
    model = FakeModel(*(['{"query": "open_issues"}'] * 40))
    chat = _chat(store, model)
    other = ResolvedUser("james", "James", "operator")
    answers_ = [chat.ask("Any issues?", OPERATOR if i % 2 else other) for i in range(QUESTIONS_PER_HOUR + 1)]
    assert all(a.kind == "answer" for a in answers_[:-1])
    assert (answers_[-1].kind, answers_[-1].text) == ("limited", LIMITED)
    assert len(model.calls) == QUESTIONS_PER_HOUR


def test_one_question_at_a_time(store: Path) -> None:
    gate = threading.Event()
    model = FakeModel('{"query": "open_issues"}', gate=gate)
    chat = _chat(store, model)
    result: list[Answer] = []
    worker = threading.Thread(target=lambda: result.append(chat.ask("Any issues?", OPERATOR)))
    worker.start()
    assert model.entered.wait(5)
    busy = chat.ask("And now?", OPERATOR)
    assert (busy.kind, busy.text) == ("busy", BUSY)
    gate.set()
    worker.join(5)
    assert result and result[0].kind == "answer"
    assert len(model.calls) == 1


# --- the LiteLLM call itself (httpx stubbed) ---------------------------------------------------------------


def _litellm(handler: Callable[[httpx.Request], httpx.Response]) -> llm.LiteLLMChat:
    return llm.LiteLLMChat("http://litellm.test", "test-key-not-real", transport=httpx.MockTransport(handler))


def test_the_call_names_the_model_tag_json_and_token_cap() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"query": "open_issues"}'}}]})

    out = _litellm(handler).complete([{"role": "user", "content": "hi"}], timeout=60)
    assert out == '{"query": "open_issues"}'
    req = seen[0]
    assert req.url == "http://litellm.test/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer test-key-not-real"
    assert req.headers["x-feature-id"] == "dashboard-chat"
    body = json.loads(req.content)
    assert body["model"] == "dashboard-chat"
    assert body["max_tokens"] == 300
    assert body["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(("handler", "sentence"), [
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)), llm.TIMED_OUT),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r)), llm.UNREACHABLE),
    (lambda r: httpx.Response(401, json={"error": "no"}), llm.REFUSED),
    (lambda r: httpx.Response(500, text="boom"), llm.UNREACHABLE),
])
def test_call_failures_become_one_sentence(handler: Callable[[httpx.Request], httpx.Response],
                                           sentence: str) -> None:
    with pytest.raises(llm.ModelError) as err:
        _litellm(handler).complete([], timeout=5)
    assert err.value.sentence == sentence


def test_a_malformed_body_is_an_empty_reply_and_then_refused(store: Path) -> None:
    model = _litellm(lambda r: httpx.Response(200, text="not json at all"))
    assert model.complete([], timeout=5) == ""
    a = Chat(store, model, now=lambda: NOW).ask("What was delivered?", OPERATOR)
    assert (a.kind, a.text) == ("refused", answers.CANT_ANSWER)


def test_a_timeout_through_the_chat(store: Path) -> None:
    model = _litellm(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)))
    a = Chat(store, model, now=lambda: NOW).ask("What was delivered?", OPERATOR)
    assert (a.kind, a.text) == ("unavailable", llm.TIMED_OUT)


def test_the_key_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_DASH_CHAT_KEY", raising=False)
    assert not llm.LiteLLMChat().configured
    monkeypatch.setenv("FACTORY_DASH_CHAT_KEY", "from-env-not-real")
    assert llm.LiteLLMChat().configured


# --- the route ------------------------------------------------------------------------------------------


@pytest.fixture
def chat_client(store: Path) -> Iterator[tuple[TestClient, FakeModel]]:
    app = create_app(db_path=store, tenants_path=TENANTS_TEST)
    model = FakeModel()
    app.state.chat = _chat(store, model)
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        login(client)
        yield client, model


HX = {"HX-Request": "true", "Origin": LOOPBACK_URL}


def test_post_chat_returns_the_answer_fragment_with_links(chat_client: tuple[TestClient, FakeModel]) -> None:
    client, model = chat_client
    model.replies.append('{"query": "delivered", "args": {"period": "this week"}}')
    r = client.post("/chat", data={"question": "What was delivered this week? <script>x</script>"}, headers=HX)
    assert r.status_code == 200
    assert "<html" not in r.text                       # a fragment, not a page
    assert "2 features were merged and checked this week" in r.text
    assert 'href="/features/FEAT-0001"' in r.text
    assert 'href="/delivered?from=2026-10-12&amp;to=2026-10-18"' in r.text
    assert "<script>x</script>" not in r.text and "&lt;script&gt;x&lt;/script&gt;" in r.text


def test_post_chat_without_scripts_returns_the_page(chat_client: tuple[TestClient, FakeModel]) -> None:
    client, model = chat_client
    model.replies.append('{"query": "open_issues"}')
    r = client.post("/chat", data={"question": "Any issues?"}, headers={"Origin": LOOPBACK_URL})
    assert r.status_code == 200 and "<html" in r.text and "open issue" in r.text


def test_a_clarifying_answer_carries_a_reply_form(chat_client: tuple[TestClient, FakeModel]) -> None:
    client, model = chat_client
    model.replies += ['{"clarify": "Which project do you mean?"}',
                      '{"query": "project_summary", "args": {"project": "acme/notes"}}']
    r = client.post("/chat", data={"question": "How is the project?"}, headers=HX)
    assert 'name="prior_question" value="How is the project?"' in r.text
    assert 'name="prior_clarify" value="Which project do you mean?"' in r.text
    r2 = client.post("/chat", data={"question": "acme/notes", "prior_question": "How is the project?",
                                    "prior_clarify": "Which project do you mean?"}, headers=HX)
    assert "acme/notes: " in r2.text
    assert len(model.calls) == 2


def test_post_chat_from_another_site_is_refused(chat_client: tuple[TestClient, FakeModel]) -> None:
    client, model = chat_client
    r = client.post("/chat", data={"question": "Any issues?"},
                    headers={"HX-Request": "true", "Origin": "https://elsewhere.example"})
    assert r.status_code == 403
    assert model.calls == []


def test_chat_page_and_drawer_for_operators(chat_client: tuple[TestClient, FakeModel]) -> None:
    client, _ = chat_client
    page = client.get("/chat")
    assert page.status_code == 200 and 'hx-post="/chat"' in page.text
    assert page.text.count('id="chat-log"') == 1     # the page's panel, no second drawer
    home = client.get("/").text
    assert 'class="chat-drawer"' in home and 'href="/chat"' in home


def test_chat_requires_an_operator_session(client: TestClient, clientco_client: TestClient) -> None:
    assert clientco_client.post("/chat", data={"question": "x"}, headers=HX).status_code == 403
    assert clientco_client.get("/chat", follow_redirects=False).status_code == 303
    assert 'chat-drawer' not in clientco_client.get("/").text


def test_chat_requires_sign_in(tmp_path: Path) -> None:
    app = create_app(db_path=tmp_path / "rm.db", tenants_path=TENANTS_TEST)
    with TestClient(app, base_url=LOOPBACK_URL) as c:
        assert c.post("/chat", data={"question": "x"}, headers=HX).status_code == 401
        assert c.get("/chat", follow_redirects=False).headers["location"] == "/login"


def test_ask_signature_for_part_e(store: Path) -> None:
    """Part E (voice) calls `app.state.chat.ask(question, user)` and reads `.text` and `.refs`."""
    a = _chat(store, FakeModel('{"query": "cost_summary"}')).ask("What did it cost?", OPERATOR)
    assert isinstance(a.text, str) and all(r.href.startswith("/") for r in a.refs)


def test_now_is_a_friday_afternoon() -> None:
    # The fixture's week (Monday 12 to Sunday 18 October) depends on this.
    assert datetime(2026, 10, 16, 13, 0, tzinfo=NOW.tzinfo) == NOW
