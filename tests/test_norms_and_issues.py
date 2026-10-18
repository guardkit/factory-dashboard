"""On-track thresholds (dated, derived, gaps never green) and issues that close when the record moves on."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from backend import db
from backend.readmodel import dbread
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
                 norms.waiting_hours, norms.quiet_hours):
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
    """The factory records nothing between a build's start and finish, so silence there is not a stall."""
    fake.planning("c1", "Build it.")
    fake.build("b1", "FEAT-0001", "c1", status="RUNNING", completed=None)
    f = _load(fake, tmp_path).feature("FEAT-0001")
    assert f is not None
    stall = next(s for s in on_track(f, load_norms(), NOW).signals if s.name == "No activity while running")
    assert stall.band == GAP and "the factory records nothing" in stall.note


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


# --- Codex round 1 -----------------------------------------------------------------------------
#
# R1: age alone never ends a feature. Old work Forge still records as queued, planning or building
# stays in progress (counted in progress, kept in the default Features list and the weekly report)
# and is shown as stalled, with its stall issue.

OLD = T0 + timedelta(days=9)


def _old_queue(fake: ForgeFake) -> str:
    fake.queue("q1", "Sent and never picked up.", status="QUEUED", minute=0)
    return "q1"


def _old_planning_queued(fake: ForgeFake) -> str:
    fake.planning("c1", "Waiting for planning to start.", state="QUEUED", end=None)
    return "c1"


def _old_waiting_for_build(fake: ForgeFake) -> str:
    fake.planning("c1", "Planned, never built.", state="BUILD_QUEUED")
    return "c1"


def _old_build_queued(fake: ForgeFake) -> str:
    fake.planning("c1", "Queued build.")
    fake.build("b1", "FEAT-0001", "c1", status="QUEUED", started=None, completed=None)
    return "FEAT-0001"


def _old_build_running(fake: ForgeFake) -> str:
    fake.planning("c1", "Running build.")
    fake.build("b1", "FEAT-0001", "c1", status="RUNNING", completed=None)
    return "FEAT-0001"


def _old_planning_running(fake: ForgeFake) -> str:
    fake.planning("c1", "Planning that went quiet.", state="RUNNING", end=None)
    fake.event("c1", "feature-spec", "RUNNING", 3)
    return "c1"


OLD_RECORDS = {
    "queue item still queued": (_old_queue, "In the queue"),
    "planning still queued": (_old_planning_queued, "Waiting for planning to start"),
    "plan approved, build not started": (_old_waiting_for_build, "Plan approved; waiting for the build to start"),
    "build still queued": (_old_build_queued, "Waiting for the build to start"),
    "build still running": (_old_build_running, "Building"),
    "planning still running": (_old_planning_running, "Planning"),
}


@pytest.mark.parametrize("name", list(OLD_RECORDS))
def test_old_unfinished_work_stays_in_progress_and_is_stalled(fake: ForgeFake, tmp_path: Path, name: str) -> None:
    setup, status = OLD_RECORDS[name]
    key = setup(fake)
    store = tmp_path / "store.db"
    store_from(fake, store, now=OLD).close()
    conn = db.connect_ro(store)
    try:
        factory = load_factory(conn, OLD)
    finally:
        conn.close()
    f = factory.feature(key)
    assert f is not None and f.in_progress and f.outcome is Outcome.IN_PROGRESS, name
    assert f.status_text == status and f.ended_at is None

    stalls = [i for i in find_issues(factory, load_norms(), OLD) if i.kind == "Stalled"]
    assert len(stalls) == 1 and stalls[0].feature is f and stalls[0].is_open and stalls[0].band == RED
    assert " days " in stalls[0].detail             # eight or nine days, said in days
    open_, _closed, hidden = recent(find_issues(factory, load_norms(), OLD), OLD)
    assert stalls[0] in open_ and hidden == 0          # an open stall is listed however old

    signal = next(s for s in on_track(f, load_norms(), OLD).signals if s.name == "No activity while running")
    assert signal.band == RED and " days " in signal.value
    assert on_track(f, load_norms(), OLD).words == "Off track"

    # The page counts: in progress on the Now page, in the default (last 7 days) Features list,
    # in the project's in-progress count and in the weekly report's in-progress count.
    home = dbread.home_view(store, OLD)
    assert len(home.in_flight.rows) == 1 and home.in_flight.rows[0].in_progress
    features = dbread.features_view(store, 7, OLD)
    assert [r.link for r in features.features.rows] == [f.link]
    assert dbread.project_view(store, f.project, OLD).in_progress == 1
    report = dbread.weekly_report_view(store, "operator", now=OLD)
    assert report.headline.in_flight_count == 1
    issues = dbread.issues_view(store, OLD)
    assert any(r.kind == "Stalled" for r in issues.open_rows)


def test_a_build_quiet_for_a_normal_build_time_is_not_stalled(fake: ForgeFake, tmp_path: Path) -> None:
    """Forge records nothing inside a build, so up to 4 h of silence is normal: no stall issue."""
    fake.planning("c1", "Running build.")
    fake.build("b1", "FEAT-0001", "c1", status="RUNNING", completed=None)   # started at minute 21
    now = T0 + timedelta(minutes=21 + 3 * 60)
    factory = _load(fake, tmp_path, now)
    assert not [i for i in find_issues(factory, load_norms(), now) if i.kind == "Stalled"]
    f = factory.feature("FEAT-0001")
    assert f is not None
    signal = next(s for s in on_track(f, load_norms(), now).signals if s.name == "No activity while running")
    assert signal.band == GAP and "records nothing" in signal.note


def test_a_build_quiet_for_five_hours_is_an_amber_stall(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Running build.")
    fake.build("b1", "FEAT-0001", "c1", status="RUNNING", completed=None)
    now = T0 + timedelta(minutes=21 + 5 * 60)
    stalls = [i for i in find_issues(_load(fake, tmp_path, now), load_norms(), now) if i.kind == "Stalled"]
    assert [i.band for i in stalls] == [AMBER] and stalls[0].detail == "nothing recorded for 5 h 00 min"


def test_a_stall_goes_when_the_record_moves_on(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Running build.")
    fake.build("b1", "FEAT-0001", "c1", status="RUNNING", completed=None)
    fake.work("b1", 60 * 24 * 9 - 10)    # something recorded ten minutes ago
    stalls = [i for i in find_issues(_load(fake, tmp_path, OLD), load_norms(), OLD) if i.kind == "Stalled"]
    assert stalls == []


def test_a_feature_on_a_mission_branch_is_not_called_stalled(fake: ForgeFake, tmp_path: Path) -> None:
    """Waiting for the mission's own merge is the mission's business, not a stall of the feature."""
    fake.mission("m-1", "Mark notes as done", "mc", status="RUNNING")
    fake.planning("mc-A-a1", "Add a done flag.")
    fake.build("bA", "FEAT-000A", "mc-A-a1", mission_id="m-1", mission_role="feature")
    fake.merge_decision("bA", 81)
    fake.merged("bA", 82, mission_branch=True)
    factory = _load(fake, tmp_path, OLD)
    f = factory.feature("FEAT-000A")
    assert f is not None and f.outcome is Outcome.ON_MISSION_BRANCH and f.in_progress
    assert not [i for i in find_issues(factory, load_norms(), OLD) if i.kind == "Stalled"]


# R4: a failed check of a mission's own merge is an issue of the mission; it closes on a retry or
# when the merge passes, and the mission's merge row is still never a feature or an attempt.


def _mission_with_merge(fake: ForgeFake, status: str = "COMPLETE") -> None:
    fake.mission("m-2", "Tag notes", "mc2", status="MERGING")
    fake.planning("mc2-A-a1", "Add tags.")
    fake.build("bA", "FEAT-000A", "mc2-A-a1", mission_id="m-2", mission_role="feature")
    fake.merge_decision("bA", 81)
    fake.merged("bA", 82, mission_branch=True)
    error = "The press stopped." if status == "FAILED" else None
    fake.build("bM", "FEAT-000A", "mc2", status=status, queued=100, started=100, completed=101,
               mission_id="m-2", mission_role="mission-merge", error=error)
    fake.merge_offer("bM", 102)
    fake.merge_decision("bM", 103)


def test_a_failed_mission_merge_check_is_an_issue_of_the_mission(fake: ForgeFake, tmp_path: Path) -> None:
    _mission_with_merge(fake)
    fake.merge_failed("bM", 104, "The mission branch no longer matches its last landing. Kept.")
    factory = _load(fake, tmp_path)
    failed = [i for i in find_issues(factory, load_norms(), NOW) if i.kind == "Merge check failed"]
    assert len(failed) == 1
    issue = failed[0]
    assert issue.mission is not None and issue.mission.id == "m-2" and issue.feature is None
    assert issue.is_open and issue.band == RED and issue.opened_at == T0 + timedelta(minutes=104)
    assert issue.detail == "The mission branch no longer matches its last landing."
    assert issue.subject == "mission m-2's merge (Tag notes)" and issue.project == "acme/notes"
    # Still one feature with one attempt: the mission's merge row is never counted as either.
    assert [f.key for f in factory.features] == ["FEAT-000A"]
    assert factory.features[0].attempts == 1


def test_a_mission_merge_failure_closes_on_a_retry_that_passes(fake: ForgeFake, tmp_path: Path) -> None:
    _mission_with_merge(fake)
    fake.merge_failed("bM", 104, "A check failed.")
    fake.merged("bM", 130)
    factory = _load(fake, tmp_path)
    failed = [i for i in find_issues(factory, load_norms(), NOW) if i.kind == "Merge check failed"]
    assert [i.closed_at for i in failed] == [T0 + timedelta(minutes=130)]
    assert factory.missions[0].delivered_at() == T0 + timedelta(minutes=130)
    feature = factory.feature("FEAT-000A")
    assert feature is not None and feature.outcome is Outcome.DELIVERED and feature.attempts == 1


def test_a_failed_mission_merge_build_without_a_merge_step_is_an_issue(fake: ForgeFake, tmp_path: Path) -> None:
    _mission_with_merge(fake, status="FAILED")
    failed = [i for i in find_issues(_load(fake, tmp_path), load_norms(), NOW) if i.kind == "Merge check failed"]
    assert len(failed) == 1 and failed[0].detail == "The press stopped." and failed[0].mission is not None
