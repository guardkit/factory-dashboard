"""The copy of LiteLLM's spend log: incremental, idempotent, overlapping, re-priced, and honest about
outages (design of 17 October 2026, requirement 3, "Checks for the import")."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from backend import db
from backend.prices import load_prices
from backend.projector import cost_import
from backend.projector.cost_import import feature_tag, import_once, run_pass
from backend.projector.store import open_rw

from tests.spend_fake import IN_PRICE, OUT_PRICE, FakeSpend

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "readmodel.db"
    db.apply_schema(path)
    return open_rw(path)


def _ids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT request_id FROM spend_calls ORDER BY started_at, request_id")]


def _status(conn: sqlite3.Connection) -> tuple[object, ...]:
    return conn.execute(  # type: ignore[no-any-return]
        "SELECT last_success_at, last_attempt_at, last_error FROM cost_feed_status WHERE source='litellm'").fetchone()


def test_each_call_is_stored_once_and_the_same_pass_twice_changes_nothing(store: sqlite3.Connection) -> None:
    fake = FakeSpend(T0)
    for i in range(3):
        fake.add(f"r{i}", i, "corr-1")
    first = import_once(store, fake, T0 + timedelta(minutes=5))
    second = import_once(store, fake, T0 + timedelta(minutes=6))
    assert (first.ok, first.inserted) == (True, 3)
    assert (second.ok, second.inserted) == (True, 0)
    assert _ids(store) == ["r0", "r1", "r2"]


def test_only_what_is_needed_is_kept(store: sqlite3.Connection) -> None:
    fake = FakeSpend(T0)
    fake.add("r1", 1, "corr-1", prompt=1200, completion=300, model_name="architect-agent")
    import_once(store, fake, T0 + timedelta(minutes=5))
    cols = [r[1] for r in store.execute("PRAGMA table_info(spend_calls)")]
    assert "messages" not in cols and "response" not in cols and "api_key" not in cols
    row = store.execute("SELECT feature_tag, model_name, prompt_tokens, completion_tokens, started_at, ended_at "
                        "FROM spend_calls").fetchone()
    assert row == ("corr-1", "architect-agent", 1200, 300, "2026-10-18T09:01:00.000000Z", "2026-10-18T09:01:30.000000Z")


def test_a_late_row_and_a_long_call_are_picked_up_by_the_overlap(store: sqlite3.Connection) -> None:
    """A call that ends after a later-starting call is already stored, and a row written 2 hours late,
    both fall inside the 3-hour re-read."""
    fake = FakeSpend(T0)
    fake.add("long", 0, "corr-1", duration_min=50, hidden=True)  # still running: LiteLLM writes it at the end
    fake.add("short", 10, "corr-1")
    import_once(store, fake, T0 + timedelta(minutes=12))
    assert _ids(store) == ["short"]
    fake.reveal("long")
    fake.add("later", 125, "corr-1")
    fake.add("two-hours-late", 10.5, "corr-1", hidden=True)
    import_once(store, fake, T0 + timedelta(minutes=126))
    fake.reveal("two-hours-late")
    result = import_once(store, fake, T0 + timedelta(minutes=127))
    assert result.inserted == 1
    assert _ids(store) == ["long", "short", "two-hours-late", "later"]


def test_two_calls_with_the_same_start_time_across_a_page_boundary(store: sqlite3.Connection,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cost_import, "BATCH", 2)
    fake = FakeSpend(T0)
    for rid in ("a", "b", "c", "d", "e"):
        fake.add(rid, 1, "corr-1")  # all start at the same moment
    result = import_once(store, fake, T0 + timedelta(minutes=5))
    assert result.inserted == 5
    assert _ids(store) == ["a", "b", "c", "d", "e"]
    assert fake.reads == 3  # pages of 2, 2 and 1


def test_an_outage_then_catch_up(store: sqlite3.Connection) -> None:
    fake = FakeSpend(T0)
    fake.add("r1", 0, "corr-1")
    import_once(store, fake, T0 + timedelta(minutes=1))
    good = _status(store)[0]
    fake.down = True
    for minute in range(2, 32):  # a 30-minute outage, with calls still being made
        fake.add(f"during-{minute}", minute, "corr-1")
        result = import_once(store, fake, T0 + timedelta(minutes=minute))
        assert not result.ok
    last_success, _attempt, error = _status(store)
    assert last_success == good                     # the last good read is kept
    assert error == "could not read LiteLLM's spend log (SourceDown)"
    fake.down = False
    result = import_once(store, fake, T0 + timedelta(minutes=33))
    assert result.ok and result.inserted == 30
    assert _status(store)[2] is None


def test_the_error_never_carries_the_database_address(store: sqlite3.Connection) -> None:
    fake = FakeSpend(T0)
    fake.down = True
    import_once(store, fake, T0)
    error = str(_status(store)[2])
    assert "postgresql" not in error and "not-a-real-password" not in error and "192.0.2.9" not in error


def test_prices_litellm_spend_estimated_no_tokens_and_not_priced(store: sqlite3.Connection) -> None:
    fake = FakeSpend(T0)
    fake.add("priced", 1, "corr-1", prompt=1000, completion=100)                       # LiteLLM's own spend
    fake.add("old", 2, "corr-1", prompt=2000, completion=500, spend=0.0)               # before prices: estimated
    fake.add("failed", 3, "corr-1", prompt=0, completion=0, spend=0.0, status="failure")
    fake.add("other-model", 4, None, prompt=900, completion=90, provider_model="openai/coach-ft-v4")
    import_once(store, fake, T0 + timedelta(minutes=5))
    rows = {r[0]: r[1:] for r in store.execute("SELECT request_id, cost_usd, cost_basis, price_ref FROM spend_calls")}
    ref = "qwen/qwen3.8-flash, OpenRouter 2026-10-17"
    assert rows["priced"] == (pytest.approx(1000 * IN_PRICE + 100 * OUT_PRICE), "litellm", ref)
    assert rows["old"] == (pytest.approx(2000 * IN_PRICE + 500 * OUT_PRICE), "estimated", ref)
    assert rows["failed"] == (0.0, "no tokens", None)
    assert rows["other-model"] == (None, "not priced", None)   # never counted as 0


def test_the_price_list_is_dated_and_named() -> None:
    prices = load_prices()
    assert prices.fetched_on.isoformat() == "2026-10-17"
    assert prices.label == "Nominal: OpenRouter price for qwen/qwen3.8-flash on 17 Oct 2026"
    assert prices.find("qwen3.8-flash-next-autoround") is prices.find("openai/qwen3.8-flash-next-autoround")
    assert prices.tagging_started_at == datetime(2026, 10, 17, 6, 57, 36, tzinfo=UTC)


def test_an_untagged_call_is_kept_without_a_feature() -> None:
    assert feature_tag(["User-Agent: AsyncOpenAI", "x-feature-id: 2860-B-a2"]) == "2860-B-a2"
    assert feature_tag(["User-Agent: AsyncOpenAI"]) is None
    assert feature_tag([]) is None and feature_tag(None) is None


def test_untagged_rows_are_stored_with_no_tag(store: sqlite3.Connection) -> None:
    fake = FakeSpend(T0)
    fake.add("tutor", 1, None, model_name="tutor-coach")
    import_once(store, fake, T0 + timedelta(minutes=5))
    assert store.execute("SELECT feature_tag FROM spend_calls").fetchone() == (None,)


def test_the_daily_recount_adds_rows_written_too_late_for_the_overlap(store: sqlite3.Connection) -> None:
    fake = FakeSpend(T0)
    fake.add("yesterday", -24 * 60, "corr-1", hidden=True)  # yesterday's call, written today
    fake.add("now", 0, "corr-1")
    import_once(store, fake, T0 + timedelta(minutes=1))
    fake.reveal("yesterday")
    import_once(store, fake, T0 + timedelta(hours=2))       # the overlap reaches back 3 hours only
    assert "yesterday" not in _ids(store)
    result = import_once(store, fake, T0 + timedelta(hours=25))   # a day after the last recount
    assert result.reconciled == 1
    assert "yesterday" in _ids(store)
    note = store.execute("SELECT reconcile_note FROM cost_feed_status").fetchone()[0]
    assert "1 missing calls added" in note


def test_without_an_address_the_feed_says_so_and_imports_nothing(store: sqlite3.Connection,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_DASH_SPEND_DB_URL", raising=False)
    result = run_pass(store, T0)
    assert not result.ok
    assert _status(store)[0] is None
    assert _status(store)[2] == "the spend log is not configured"


def test_new_calls_tell_open_pages_to_refresh(store: sqlite3.Connection) -> None:
    fake = FakeSpend(T0)
    fake.add("r1", 1, "corr-1")
    import_once(store, fake, T0 + timedelta(minutes=2))
    seq_after_first = store.execute("SELECT MAX(seq) FROM change_log").fetchone()[0]
    import_once(store, fake, T0 + timedelta(minutes=3))   # nothing new
    assert store.execute("SELECT MAX(seq) FROM change_log").fetchone()[0] == seq_after_first
    assert seq_after_first is not None
