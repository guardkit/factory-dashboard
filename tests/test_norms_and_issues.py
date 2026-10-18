"""On-track thresholds (dated, derived, gaps never green) and issues that close when the record moves on."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from backend import db
from backend.readmodel.factory import Factory, Outcome, load_factory
from backend.readmodel.issues import Issue, find_issues, recent
from backend.readmodel.norms import AMBER, GAP, GREEN, RED, derive, load_norms, on_track

from tests.forge_fake import T0, ForgeFake, delivered_feature, store_from

NOW = T0 + timedelta(hours=3)


@pytest.fixture
def fake(tmp_path: Path) -> ForgeFake:
    return ForgeFake(tmp_path / "forge.db")


def _load(fake: ForgeFake, tmp_path: Path, now=NOW) -> Factory:  # type: ignore[no-untyped-def]
    store = tmp_path / "store.db"
    store_from(fake, store).close()
    conn = db.connect_ro(store)
    try:
        return load_factory(conn, now)
    finally:
        conn.close()


def test_norms_file_is_dated_and_says_how_each_number_was_derived() -> None:
    norms = load_norms()
    assert norms.derived_on >= "2026-10-17"
    assert norms.window_days == 14
    for band in (norms.attempts, norms.planning_minutes, norms.build_minutes, norms.stall_minutes,
                 norms.waiting_hours):
        assert band.how and band.green_max <= band.amber_max
    # Re-derived from builds, not the July "6-9 attempts / 120-minute stall" world.
    assert norms.stall_minutes.amber_max <= 60
    assert norms.attempts.green_max == 1


def test_bands() -> None:
    attempts = load_norms().attempts
    assert [attempts.band(n) for n in (1, 2, 3)] == [GREEN, AMBER, RED]


def test_unmeasured_signals_are_gaps_never_green(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Still planning.", state="RUNNING", end=None)
    f = _load(fake, tmp_path).feature("c1")
    assert f is not None
    signals = {s.name: s for s in on_track(f, load_norms(), NOW).signals}
    assert signals["Build attempts"].band == GAP and signals["Build attempts"].value == "—"
    assert signals["Build time"].band == GAP


def test_a_quiet_build_is_not_called_stalled(fake: ForgeFake, tmp_path: Path) -> None:
    """Forge records nothing between a build's start and finish, so silence there is not a stall."""
    fake.planning("c1", "Build it.")
    fake.build("b1", "FEAT-0001", "c1", status="RUNNING", completed=None)
    f = _load(fake, tmp_path).feature("FEAT-0001")
    assert f is not None
    stall = next(s for s in on_track(f, load_norms(), NOW).signals if s.name == "No activity while running")
    assert stall.band == GAP and "Forge records nothing" in stall.note


def test_a_quiet_planning_run_is_a_stall(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Plan it.", state="RUNNING", end=None)
    fake.event("c1", "feature-spec", "dispatch_submitted", 1)
    factory = _load(fake, tmp_path)
    f = factory.feature("c1")
    assert f is not None
    stall = next(s for s in on_track(f, load_norms(), NOW).signals if s.name == "No activity while running")
    assert stall.band == RED
    kinds = [i.kind for i in find_issues(factory, load_norms(), NOW) if i.is_open]
    assert "Stalled" in kinds


def test_a_factory_failure_is_off_track_whatever_the_signals(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Plan it.", state="FAILED", error="It broke.")
    f = _load(fake, tmp_path).feature("c1")
    assert f is not None
    ot = on_track(f, load_norms(), NOW)
    assert ot.band == RED and ot.words == "Planning failed" and ot.reason == "Planning failed: It broke."


def test_a_failed_build_closes_when_the_next_attempt_starts(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Count notes.")
    fake.build("b1", "FEAT-0001", "c1", status="FAILED", completed=60, error="Tests failed. Long detail.")
    fake.build("b2", "FEAT-0001", "fix-build-FEAT-0001-1", queued=70, started=70, completed=None, status="RUNNING")
    issues = [i for i in find_issues(_load(fake, tmp_path), load_norms(), NOW) if i.kind == "Build failed"]
    assert len(issues) == 1
    assert issues[0].closed_at == T0 + timedelta(minutes=70) and issues[0].detail == "Tests failed."


def test_a_failure_closes_when_the_same_request_is_sent_again(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Count the notes.", state="FAILED", start=0, end=10, error="No.")
    fake.planning("c2", "Count  the notes.", state="RUNNING", start=30, end=None)
    issues = [i for i in find_issues(_load(fake, tmp_path), load_norms(), NOW) if i.kind == "Planning failed"]
    assert issues[0].closed_at == T0 + timedelta(minutes=30)


def test_waits_are_issues_while_open_and_when_long(fake: ForgeFake, tmp_path: Path) -> None:
    delivered_feature(fake, 1)  # a one-minute spec wait: not an issue
    fake.planning("c2", "Waiting one.")
    fake.build("b2", "FEAT-0002", "c2", status="COMPLETE")
    fake.merge_offer("b2", 81)
    factory = _load(fake, tmp_path)
    waits = [i for i in find_issues(factory, load_norms(), NOW) if i.kind.startswith("Waiting")]
    assert len(waits) == 1
    assert waits[0].kind == "Waiting for the merge decision" and waits[0].is_open
    assert waits[0].band == AMBER  # about 1 h 39 min so far


def test_recent_windows(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("old", "Old failure.", state="FAILED", start=0, end=5, error="x")
    late = T0 + timedelta(days=20)
    open_, closed, hidden = recent(find_issues(_load(fake, tmp_path, late), load_norms(), late), late)
    assert open_ == [] and closed == []  # a 20-day-old failure is out of the 14-day window
    assert hidden == 1                   # and counted, so the page can say so


def test_derive_uses_percentiles_of_recent_builds(fake: ForgeFake, tmp_path: Path) -> None:
    for n in range(1, 21):
        delivered_feature(fake, n)
    data = derive(_load(fake, tmp_path), NOW, 14)
    assert data["derived_on"] == NOW.date().isoformat()
    assert data["build_minutes"]["green_max"] == 59  # every fake build ran 59 min
    assert "20 builds" in data["build_minutes"]["how"]
    assert data["planning_minutes"]["green_max"] == 19  # 20 min, minus the 1 min spec wait


# --- coach round 1: a signal with no data is a gap, and an ending is not a verdict -------------


def _verdict(fake: ForgeFake, tmp_path: Path, key: str, now=NOW):  # type: ignore[no-untyped-def]
    f = _load(fake, tmp_path, now).feature(key)
    assert f is not None
    return f, on_track(f, load_norms(), now)


def test_waiting_is_not_measured_when_no_gate_was_reached(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Still planning.", state="RUNNING", end=None)
    _f, ot = _verdict(fake, tmp_path, "c1")
    waiting = next(s for s in ot.signals if s.name == "Waiting for a person")
    assert waiting.band == GAP and waiting.value == "—" and "never reached" in waiting.note


def test_nothing_measured_is_not_a_verdict(fake: ForgeFake, tmp_path: Path) -> None:
    """A request still in the queue has no measured signal: 'Not measured yet', never green."""
    fake.queue("q1", "Waiting in the queue.", status="QUEUED", minute=170)
    f, ot = _verdict(fake, tmp_path, "q1")
    assert f.in_progress
    assert ot.band == GAP and ot.words == "Not measured yet"
    planning = next(s for s in ot.signals if s.name == "Planning time")
    assert planning.note == "planning never started"  # not "it was sent already planned"


@pytest.mark.parametrize(("setup", "key", "words"), [
    ("cancelled", "FEAT-0001", "Cancelled"),
    ("withdrawn", "q1", "Stopped in the queue"),
    ("declined", "FEAT-0001", "Built, not merged"),
    ("record_stopped", "c1", "No longer active"),
])
def test_finished_without_delivery_shows_the_ending_not_on_track(
    fake: ForgeFake, tmp_path: Path, setup: str, key: str, words: str,
) -> None:
    now = NOW
    if setup == "cancelled":       # FEAT-44B9 on 17 October: a cancelled mission feature
        fake.planning("c1", "Show the done flag.")
        fake.build("b1", "FEAT-0001", "c1", status="CANCELLED", completed=60)
    elif setup == "withdrawn":
        fake.queue("q1", "Never mind.", status="WITHDRAWN")
    elif setup == "declined":
        fake.planning("c1", "Clear button.")
        fake.build("b1", "FEAT-0001", "c1")
        fake.merge_offer("b1", 81)
        fake.merge_decision("b1", 81.1, "reject")
    else:
        fake.planning("c1", "An old one.", state="BUILD_QUEUED")
        now = T0 + timedelta(days=9)
    _f, ot = _verdict(fake, tmp_path, key, now)
    assert ot.words == words and ot.band == GAP
    assert ot.words not in ("On track", "At risk", "Off track")


def test_a_failed_build_shows_the_failure_in_red(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Count notes.")
    fake.build("b1", "FEAT-0001", "c1", status="FAILED", completed=60, error="Tests failed.")
    _f, ot = _verdict(fake, tmp_path, "FEAT-0001")
    assert ot.band == RED and ot.words == "Build failed"


def test_a_delivered_feature_keeps_its_measured_verdict(fake: ForgeFake, tmp_path: Path) -> None:
    delivered_feature(fake, 1)
    _f, ot = _verdict(fake, tmp_path, "FEAT-0001")
    assert ot.band == GREEN and ot.words == "On track"


def test_a_retired_planning_run_says_so_and_is_not_in_progress(fake: ForgeFake, tmp_path: Path) -> None:
    """Request 660d487e: planned in July, retired in Forge's 9 October clean-up."""
    fake.planning("c1", "Report the API version.", state="BUILD_QUEUED")
    fake.event("c1", "build-queued", "BUILD_QUEUED", 20)
    fake.event("c1", "planning-handoff-retirement-administrative", "SKIPPED", 60 * 24 * 3,
               {"planning_handoff_retirement": {}})
    f, ot = _verdict(fake, tmp_path, "c1", T0 + timedelta(days=4))
    assert f.outcome is Outcome.RETIRED and not f.in_progress
    assert f.status_text == "Retired in the clean-up of 19 October"
    assert ot.words == "Retired"


def test_a_merge_decision_waiting_a_week_stays_in_progress(fake: ForgeFake, tmp_path: Path) -> None:
    """The week-old 'record stopped' rule must not swallow a feature that is waiting for a person."""
    fake.planning("c1", "Delete a note.")
    fake.build("b1", "FEAT-0001", "c1")
    fake.merge_offer("b1", 81)
    later = T0 + timedelta(days=8)
    f = _load(fake, tmp_path, later).feature("FEAT-0001")
    assert f is not None and f.in_progress and f.outcome is Outcome.IN_PROGRESS
    assert f.status_text == "Waiting for the merge decision"


def test_an_old_open_wait_is_still_listed(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Delete a note.")
    fake.build("b1", "FEAT-0001", "c1")
    fake.merge_offer("b1", 81)
    later = T0 + timedelta(days=20)
    open_, _closed, hidden = recent(find_issues(_load(fake, tmp_path, later), load_norms(), later), later)
    assert [i.kind for i in open_] == ["Waiting for the merge decision"] and hidden == 0


def test_the_cut_off_spares_open_waits_and_stalls_but_not_old_failures() -> None:
    now = T0 + timedelta(days=30)
    old = T0
    issues = [
        Issue("Waiting for the merge decision", RED, old, None, "waiting"),
        Issue("Stalled", RED, old, None, "quiet"),
        Issue("Build failed", RED, old, None, "boom"),
        Issue("Planning failed", RED, now - timedelta(days=1), None, "recent"),
    ]
    open_, _closed, hidden = recent(issues, now)
    assert sorted(i.kind for i in open_) == ["Planning failed", "Stalled", "Waiting for the merge decision"]
    assert hidden == 1
