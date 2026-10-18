"""The chat's cost and time answers (part B's figures, wired into part D's chat).

The answers come from `dbread.cost_summary` and `dbread.time_summary` on the answer's one
snapshot, written by fixed templates: every figure is a field from the record, a gap is said with
its reason and never as zero, and the price source and date go with every cost figure. The world
is tests/test_cost_views.py's: a delivered feature (c-1, FEAT-0001), a mission part (FEAT-000A),
a feature from before calls were tagged (FEAT-0OLD) and a project with only such work.
"""

# The world's fixtures (`factory`, `store`) are imported from tests/test_cost_views.py, so the test
# arguments of the same names "redefine" them as far as ruff can tell.
# ruff: noqa: F811

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from backend.chat import answers, grounding, tools
from backend.chat.service import OPERATORS_ONLY, Chat
from backend.projector.cost_import import import_once
from backend.projector.forge_mirror import mirror_once
from backend.projector.store import open_rw
from backend.readmodel import dbread
from backend.readmodel import viewmodels as vm

from tests.forge_fake import REPO, ForgeFake, store_from
from tests.spend_fake import FakeSpend
from tests.test_chat import _COUNT_KEYS, CLIENT, OPERATOR, FakeModel
from tests.test_cost_views import C1, MA, NOW, OLD_PROJECT, T, _calls, factory, store  # noqa: F401 (the fixtures)

PRICE = "Nominal: OpenRouter price for qwen/qwen3.8-flash on 17 Oct 2026."
FEATURE = "Search box that filters notes by title (FEAT-0001)"


def _ctx(path: Path) -> tools.Context:
    return tools.Context(path, NOW)


def _grounded(r: tools.Result) -> str:
    text = answers.render(r)
    assert grounding.figures_grounded(text, r.fields), (text, grounding.ungrounded_figures(text, r.fields))
    return text


# --- real figures -------------------------------------------------------------------------------------


def test_a_features_cost_is_the_feature_pages_figure(store: Path) -> None:
    r = tools.cost_summary(_ctx(store), feature="FEAT-0001")
    _chrome, page = dbread.feature_page(store, "FEAT-0001", NOW)  # type: ignore[misc]
    assert r.fields["cost"] == page.spend.value == vm.fmt_usd(C1)
    assert (r.fields["calls"], r.fields["tokens_in"], r.fields["tokens_out"]) == ("5", "162 k", "8 k")
    text = _grounded(r)
    assert text == (f"{FEATURE}: 5 model calls, 162 k tokens in and 8 k out, nominal cost {vm.fmt_usd(C1)} "
                    "(part estimated from the tokens). " + PRICE)
    assert [ref.href for ref in r.refs] == ["/features/FEAT-0001"]
    # By its title words too, as the chat finds features.
    assert tools.cost_summary(_ctx(store), feature="the search box").fields["cost"] == vm.fmt_usd(C1)


def test_a_projects_cost_is_the_project_pages_figure_and_says_what_it_leaves_out(store: Path) -> None:
    r = tools.cost_summary(_ctx(store), project=REPO)
    view = dbread.project_view(store, REPO, NOW)
    assert r.fields["cost"] == view.spend.value == vm.fmt_usd(C1 + MA)
    assert (r.fields["calls"], r.fields["left_out"]) == ("7", "1")        # FEAT-0OLD was before tagging
    text = _grounded(r)
    assert text.startswith(f"{REPO}: 7 model calls, ")
    assert "Counts calls from 17 October, when calls began to be tagged by feature." in text
    assert "It leaves out 1 feature sent before calls were tagged by feature." in text
    assert text.endswith(PRICE)
    assert [ref.href for ref in r.refs] == [f"/projects/{REPO}"]


def test_cost_for_a_period(store: Path) -> None:
    r = tools.cost_summary(_ctx(store), project=REPO, period="last week")
    text = _grounded(r)
    assert r.fields["state"] == "not_measured"           # last week ended before calls were tagged
    assert text == (f"Cost for {REPO}, last week (5 Oct 2026 to 11 Oct 2026) isn't measured: "
                    "Calls before 17 October were not tagged by feature.")
    assert r.refs[-1].href == "/reports?from=2026-10-05&to=2026-10-11"


def test_a_features_time_is_the_feature_pages_figure(store: Path) -> None:
    r = tools.time_summary(_ctx(store), feature="FEAT-0001")
    _chrome, page = dbread.feature_page(store, "FEAT-0001", NOW)  # type: ignore[misc]
    assert (r.fields["elapsed"], r.fields["machine"], r.fields["waiting"]) == \
        (vm.fmt_age(5400), vm.fmt_age(5160), vm.fmt_age(120)) == ("1 h 30 min", "1 h 26 min", "2 min")
    assert page.time.machine_secs == 5160 and page.time.waiting_secs == 120
    text = _grounded(r)
    assert text == (f"{FEATURE}: 1 h 30 min elapsed, of which 1 h 26 min machine time and 2 min waiting. "
                    "Longest waits: Waiting for approvals 1 min; and Waiting for the merge decision 1 min.")


def test_a_projects_time(store: Path) -> None:
    r = tools.time_summary(_ctx(store), project=REPO)
    t = dbread.time_summary(store, project=REPO, now=NOW)
    assert r.fields["features"] == str(t.features) and r.fields["elapsed"] == vm.fmt_age(t.elapsed_secs or 0)
    assert _grounded(r).startswith(f"{REPO} across {t.features} features: ")


# --- gaps: said with their reason, never as zero ----------------------------------------------------------


def test_a_feature_from_before_tagging(store: Path) -> None:
    r = tools.cost_summary(_ctx(store), feature="FEAT-0OLD")
    text = _grounded(r)
    assert text == "Cost for An old feature (FEAT-0OLD) isn't measured: Calls before 17 October were not tagged " \
                   "by feature."
    assert "$" not in text and r.fields["cost"] == ""
    old = _grounded(tools.cost_summary(_ctx(store), project=OLD_PROJECT))
    assert old == f"Cost for {OLD_PROJECT} isn't measured: Calls before 17 October were not tagged by feature."
    assert dbread.project_view(store, OLD_PROJECT, NOW).spend.state == "not_measured"   # the page agrees


def _unpriced_store(tmp_path: Path, factory: ForgeFake, *, all_unpriced: bool) -> Path:
    spend = FakeSpend(T)
    if not all_unpriced:
        spend.add("p1", 5, "c-1", prompt=10_000, completion=1_000)
    spend.add("x1", 30, "c-1", prompt=2_000, completion=200, provider_model="openai/no-price-model")
    path = tmp_path / "readmodel.db"
    conn = store_from(factory, path, NOW)
    import_once(conn, spend, NOW)
    conn.close()
    return path


def test_unpriced_calls_are_disclosed(tmp_path: Path, factory: ForgeFake) -> None:
    some = tools.cost_summary(_ctx(_unpriced_store(tmp_path, factory, all_unpriced=False)), feature="FEAT-0001")
    text = _grounded(some)
    assert text.startswith(f"{FEATURE}: 2 model calls, ")
    assert "1 of these calls was on a model with no price, so it is not counted." in text


def test_calls_with_no_price_at_all_are_not_measured(tmp_path: Path, factory: ForgeFake) -> None:
    r = tools.cost_summary(_ctx(_unpriced_store(tmp_path, factory, all_unpriced=True)), feature="FEAT-0001")
    text = _grounded(r)
    assert text == (f"Cost for {FEATURE} isn't measured: None of these calls has a price. 1 of these calls was "
                    "on a model with no price, so it is not counted.")
    assert "$" not in text


def test_a_stale_cost_feed_is_said(tmp_path: Path, factory: ForgeFake) -> None:
    path = tmp_path / "readmodel.db"
    conn = store_from(factory, path, NOW)
    import_once(conn, _calls(T), NOW - timedelta(minutes=11))
    conn.close()
    text = _grounded(tools.cost_summary(_ctx(path), feature="FEAT-0001"))
    assert text.endswith(PRICE + " Note: cost data last read at 13:09:00 — may be incomplete.")


def test_before_any_import_cost_is_not_available(tmp_path: Path, factory: ForgeFake) -> None:
    path = tmp_path / "readmodel.db"
    store_from(factory, path, NOW).close()
    text = _grounded(tools.cost_summary(_ctx(path), feature="FEAT-0001"))
    assert text == f"Cost for {FEATURE} isn't available: LiteLLM's spend log has not been read yet."


DAY = 24 * 60.0


def test_time_lost_to_a_clean_up_is_not_measured_and_a_project_says_how_many_it_leaves_out(tmp_path: Path) -> None:
    """The 1 October shape (tests/test_feature_time.py): rows in May, closed by a clean-up."""
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "An old thing.", start=-140 * DAY, end=-140 * DAY + 20)
    for n in range(3):
        fake.build(f"b{n}", "FEAT-0001", f"c1-{n}", status="FAILED", queued=-140 * DAY + 21 + n,
                   started=-15 * DAY, completed=-15 * DAY, error="sandbox-required")
        fake.work(f"b{n}", -140 * DAY + 21 + n)
    fake.planning("c2", "A recent thing.")
    fake.build("b9", "FEAT-0002", "c2")
    path = tmp_path / "readmodel.db"
    store_from(fake, path, NOW).close()
    one = tools.time_summary(_ctx(path), feature="FEAT-0001")
    text = _grounded(one)
    # The builds' own correlation ids are not the planning run's, so the feature is known by its code.
    assert text.startswith("Time for FEAT-0001 isn't measured: a build was closed in a clean-up on "
                           "1 October 2026, 124 days")
    assert one.fields["elapsed"] == "" and "0 min" not in text
    both = tools.time_summary(_ctx(path), project=REPO)
    text = _grounded(both)
    assert both.fields["not_measured_count"] == "1"
    assert text.startswith(f"{REPO} across {both.fields['features']} feature")
    assert "1 feature is left out: a build was closed in a later clean-up" in text


# --- the chat around them ------------------------------------------------------------------------------------


def test_a_cost_question_needs_a_feature_or_a_project(store: Path) -> None:
    for run, what in ((tools.cost_summary, "cost"), (tools.time_summary, "time")):
        r = run(_ctx(store))
        assert r.status == "no_scope"
        assert answers.render(r) == f"Which feature or project do you mean? I can give the {what} of one at a time."
    assert tools.cost_summary(_ctx(store), feature="a rocket launcher").status == "not_found"
    assert tools.time_summary(_ctx(store), project="nothing here").status == "not_found"


def test_through_the_chat_and_clients_are_refused(store: Path) -> None:
    model = FakeModel('{"query": "cost_summary", "args": {"feature": "FEAT-0001"}}',
                      '{"query": "time_summary", "args": {"project": "acme/notes"}}')
    chat = Chat(store, model, now=lambda: NOW)
    a = chat.ask("What did the search box cost?", OPERATOR)
    assert a.kind == "answer" and a.text.startswith(f"{FEATURE}: 5 model calls")
    assert a.refs[0].href == "/features/FEAT-0001"
    b = chat.ask("How long did acme/notes take?", OPERATOR)
    assert b.kind == "answer" and b.text.startswith(f"{REPO} across ")
    refused = Chat(store, FakeModel('{"query": "cost_summary", "args": {"feature": "FEAT-0001"}}'),
                   now=lambda: NOW).ask("What did it cost?", CLIENT)
    assert (refused.kind, refused.text) == ("refused", OPERATORS_ONLY)


def test_every_digit_comes_from_the_record(store: Path, tmp_path: Path, factory: ForgeFake) -> None:
    results = [
        tools.cost_summary(_ctx(store), feature="FEAT-0001"), tools.cost_summary(_ctx(store), project=REPO),
        tools.cost_summary(_ctx(store), feature="FEAT-0OLD"), tools.cost_summary(_ctx(store), project=OLD_PROJECT),
        tools.cost_summary(_ctx(store), feature="FEAT-000A", period="this week"),
        tools.time_summary(_ctx(store), feature="FEAT-0001"), tools.time_summary(_ctx(store), project=REPO),
        tools.time_summary(_ctx(store), feature="FEAT-000A"), tools.time_summary(_ctx(store), feature="FEAT-0OLD"),
    ]

    def scrub(value: object) -> object:
        if isinstance(value, str):
            return re.sub(r"\d", "n", value)
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    for r in results:
        _grounded(r)
        for count in ("0", "1", "2"):
            fields = {k: (count if k in _COUNT_KEYS else scrub(v)) for k, v in r.fields.items()}
            text = answers.render(tools.Result(r.query, r.status, fields))
            assert not re.search(r"\d", text.replace(count, "")), (r.query, text)


def test_a_cost_import_mid_answer_does_not_mix_two_moments(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The answer reads one snapshot: more calls imported after the feature is found are not counted."""
    more = _calls(T)
    more.add("late1", 150, "c-1", prompt=9_000, completion=900)
    passes: list[int] = []
    real = tools._cost_or_time_scope

    def scope_then_an_import_commits(*args: object, **kwargs: object) -> object:
        found = real(*args, **kwargs)  # type: ignore[arg-type]
        if not passes:
            writer = sqlite3.connect(store, isolation_level=None)
            try:
                passes.append(import_once(writer, more, NOW).inserted)
            finally:
                writer.close()
        return found

    monkeypatch.setattr(tools, "_cost_or_time_scope", scope_then_an_import_commits)
    r = tools.cost_summary(_ctx(store), feature="FEAT-0001")
    assert passes == [1] and r.fields["calls"] == "5"
    monkeypatch.setattr(tools, "_cost_or_time_scope", real)
    assert tools.cost_summary(_ctx(store), feature="FEAT-0001").fields["calls"] == "6"   # the import did land



def test_a_feed_pass_mid_answer_does_not_change_the_time_given(store: Path, factory: ForgeFake,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """A fix attempt of FEAT-000A (still in progress) recorded after the feature is found is not part
    of this answer's time."""
    before = answers.render(tools.time_summary(_ctx(store), feature="FEAT-000A"))
    factory.build("bA-fix", "FEAT-000A", "mc-A-a1-fix", status="RUNNING", queued=160, started=160, completed=None)
    passes: list[bool] = []
    real = tools._cost_or_time_scope

    def scope_then_a_pass_commits(*args: object, **kwargs: object) -> object:
        found = real(*args, **kwargs)  # type: ignore[arg-type]
        if not passes:
            writer = open_rw(store)
            try:
                passes.append(mirror_once(writer, factory.path, NOW))
            finally:
                writer.close()
        return found

    monkeypatch.setattr(tools, "_cost_or_time_scope", scope_then_a_pass_commits)
    assert answers.render(tools.time_summary(_ctx(store), feature="FEAT-000A")) == before
    assert passes == [True]
    monkeypatch.setattr(tools, "_cost_or_time_scope", real)
    assert answers.render(tools.time_summary(_ctx(store), feature="FEAT-000A")) != before   # the pass did land
