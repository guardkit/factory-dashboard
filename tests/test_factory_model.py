"""Features from Forge's record: delivery, missions, attempts, journey, waits and titles."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from backend import db
from backend.readmodel.factory import (
    BUILDING,
    MERGE_DECISION,
    MERGED,
    PLANNING,
    STATIONS,
    Factory,
    Outcome,
    load_factory,
    title_from,
)

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


def test_the_nine_steps_in_james_words() -> None:
    assert STATIONS == ("sent", "in queue", "planning", "spec approved", "plan approved", "building", "built",
                        "merge decision", "merged and checked")


def test_delivered_means_the_merge_step_passed(fake: ForgeFake, tmp_path: Path) -> None:
    code = delivered_feature(fake, 1, text="Add a search box above the notes list. It filters as you type.")
    f = _load(fake, tmp_path).feature(code)
    assert f is not None
    assert f.outcome is Outcome.DELIVERED and f.station == MERGED
    assert f.delivered_at == T0 + timedelta(minutes=90)
    assert f.title == "Add a search box above the notes list."
    assert f.label == "Add a search box above the notes list. (FEAT-0001)"
    assert f.status_text == "Merged and checked"


def test_built_and_declined_is_not_delivered(fake: ForgeFake, tmp_path: Path) -> None:
    """FEAT-2555 on 16 October: built, the merge word was 'reject'. Not delivered, not in flight."""
    fake.planning("c1", "Search box.")
    fake.build("b1", "FEAT-2555", "c1")
    fake.merge_offer("b1", 81)
    fake.merge_decision("b1", 81.1, "reject")
    f = _load(fake, tmp_path).feature("FEAT-2555")
    assert f is not None
    assert f.outcome is Outcome.NOT_MERGED and not f.in_progress
    assert f.status_text == "Built, not merged" and f.station == MERGE_DECISION
    assert f.delivered_at is None


def test_waiting_for_the_merge_decision_is_in_progress(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Search box.")
    fake.build("b1", "FEAT-0001", "c1")
    fake.merge_offer("b1", 81)
    fake.merge_offer("b1", 120)  # a re-offer after a restart: still one wait
    f = _load(fake, tmp_path).feature("FEAT-0001")
    assert f is not None
    assert f.in_progress and f.status_text == "Waiting for the merge decision"
    waits = [w for w in f.waits if w.kind == "merge decision"]
    assert len(waits) == 1 and waits[0].open
    assert waits[0].started_at == T0 + timedelta(minutes=81)


def test_approved_but_not_yet_merged_is_merging(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Search box.")
    fake.build("b1", "FEAT-0001", "c1")
    fake.merge_offer("b1", 81)
    fake.merge_decision("b1", 82)
    f = _load(fake, tmp_path).feature("FEAT-0001")
    assert f is not None and f.in_progress and f.status_text == "Merging and checking"


def test_merge_check_failed(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Search box.")
    fake.build("b1", "FEAT-0001", "c1")
    fake.merge_offer("b1", 81)
    fake.merge_decision("b1", 82)
    fake.merge_failed("b1", 85)
    f = _load(fake, tmp_path).feature("FEAT-0001")
    assert f is not None and f.outcome is Outcome.MERGE_FAILED and not f.in_progress


def test_a_retired_merge_offer_is_not_left_in_flight(fake: ForgeFake, tmp_path: Path) -> None:
    """Forge's 9 October clean-up retired old offers, some already approved; nothing reached main."""
    fake.planning("c1", "Old feature.")
    fake.build("b1", "FEAT-0001", "c1")
    fake.merge_offer("b1", 81)
    fake.merge_decision("b1", 82)
    fake.stage("b1", "merge-deploy", "merge_deploy_merge", "GATED", 82.1, {"merge_step": {}})
    fake.stage("b1", "merge-retirement-administrative", "merge_deploy_retirement", "SKIPPED", 100,
               {"merge_retirement": {"reason": "owner-retired"}})
    f = _load(fake, tmp_path).feature("FEAT-0001")
    assert f is not None and f.outcome is Outcome.NOT_MERGED and not f.in_progress


def test_fix_builds_are_attempts_at_the_same_feature(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Count notes.")
    fake.build("b1", "FEAT-0001", "c1", status="FAILED", completed=60, error="Merge check failed. More text.")
    fake.build("b2", "FEAT-0001", "fix-build-FEAT-0001-1", queued=70, started=70, completed=120,
               triggered_by="forge-internal")
    fake.merged("b2", 125)
    factory = _load(fake, tmp_path)
    f = factory.feature("FEAT-0001")
    assert f is not None and f.attempts == 2 and f.outcome is Outcome.DELIVERED
    assert len([x for x in factory.features if x.code == "FEAT-0001"]) == 1


def test_mission_merge_row_is_not_a_feature_or_a_delivery(fake: ForgeFake, tmp_path: Path) -> None:
    """Forge's missions (17 Oct): the merge row reuses the last feature's code; it is the mission's merge."""
    fake.mission("m-1", "Mark notes as done", "mc", status="MERGED", closed=200)
    fake.planning("mc-A-a1", "Add a done flag.")
    fake.build("bA", "FEAT-000A", "mc-A-a1", mission_id="m-1", mission_role="feature")
    fake.merge_decision("bA", 81)
    fake.merged("bA", 82, mission_branch=True)
    fake.build("bM", "FEAT-000A", "mc", queued=150, started=150, completed=150, mission_id="m-1",
               mission_role="mission-merge")
    fake.merge_offer("bM", 151)
    fake.merge_decision("bM", 152)
    fake.merged("bM", 160)
    factory = _load(fake, tmp_path)
    features = [f for f in factory.features if f.code == "FEAT-000A"]
    assert len(features) == 1
    f = features[0]
    assert f.attempts == 1  # the mission's merge is not an attempt
    assert f.outcome is Outcome.DELIVERED and f.delivered_at == T0 + timedelta(minutes=160)
    assert [b.build_id for b in factory.mission_merges()] == ["bM"]
    assert factory.missions[0].label == "mission m-1's merge"


def test_mission_feature_waits_for_the_missions_merge(fake: ForgeFake, tmp_path: Path) -> None:
    fake.mission("m-2", "Two", "mc2")
    fake.planning("mc2-A-a1", "Add a done flag.")
    fake.build("bA", "FEAT-000B", "mc2-A-a1", mission_id="m-2", mission_role="feature")
    fake.merge_decision("bA", 81)
    fake.merged("bA", 82, mission_branch=True)
    f = _load(fake, tmp_path).feature("FEAT-000B")
    assert f is not None and f.outcome is Outcome.ON_MISSION_BRANCH and f.delivered_at is None
    assert f.in_progress  # waiting for its mission's merge is not finished
    assert "mission m-2's branch" in f.status_text


def test_mission_not_merged(fake: ForgeFake, tmp_path: Path) -> None:
    fake.mission("m-3", "Three", "mc3", status="NOT_MERGED", closed=100)
    fake.planning("mc3-A-a1", "Add a done flag.")
    fake.build("bA", "FEAT-000C", "mc3-A-a1", mission_id="m-3", mission_role="feature")
    fake.merge_decision("bA", 81)
    fake.merged("bA", 82, mission_branch=True)
    f = _load(fake, tmp_path).feature("FEAT-000C")
    assert f is not None and f.outcome is Outcome.MISSION_NOT_MERGED and not f.in_progress


def test_planning_failed_keeps_forges_sentence(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Show a count.", state="FAILED", error="The plan check refused. Details follow here.")
    f = _load(fake, tmp_path).feature("c1")
    assert f is not None and f.code == "" and f.key == "c1"
    assert f.outcome is Outcome.PLANNING_FAILED and f.station == PLANNING
    assert f.status_text == "Planning failed: The plan check refused."


def test_waiting_for_spec_approval(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Show a count.", state="RUNNING", end=None)
    fake.event("c1", "feature-spec-digest-review", "GATED", 5)
    f = _load(fake, tmp_path).feature("c1")
    assert f is not None and f.in_progress and f.status_text == "Waiting for the spec to be approved"
    assert f.open_wait() is not None and f.open_wait().kind == "spec approval"  # type: ignore[union-attr]


def test_nothing_to_build(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Show a count.", state="PLANNED_HANDOFF")
    fake.event("c1", "nothing-to-build", "PLANNED_HANDOFF", 10)
    f = _load(fake, tmp_path).feature("c1")
    assert f is not None and f.outcome is Outcome.NOTHING_TO_BUILD


def test_build_gate_wait_closed_by_the_resume(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Show a count.")
    fake.build("b1", "FEAT-0001", "c1", status="RUNNING", completed=None)
    fake.gate_pause("b1", 21)
    fake.gate_pause("b1", 30)  # re-offer
    fake.gate_resume("b1", 40)
    fake.work("b1", 41)
    f = _load(fake, tmp_path).feature("FEAT-0001")
    assert f is not None
    gate = [w for w in f.waits if w.kind == "build approval"]
    assert len(gate) == 1 and gate[0].seconds(NOW) == 19 * 60
    assert f.status_text == "Building"


def test_work_then_a_second_pause_closes_the_first_wait_where_last_seen(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Show a count.")
    fake.build("b1", "FEAT-0001", "c1", status="PAUSED", completed=None)
    fake.gate_pause("b1", 21)
    fake.gate_pause("b1", 25)
    fake.work("b1", 30)          # back at work with no resume recorded
    fake.gate_pause("b1", 60)    # parked at a second gate
    f = _load(fake, tmp_path).feature("FEAT-0001")
    assert f is not None
    gate = [w for w in f.waits if w.kind == "build approval"]
    assert [(w.started_at - T0, w.ended_at - T0 if w.ended_at else None) for w in gate] == [
        (timedelta(minutes=21), timedelta(minutes=25)),
        (timedelta(minutes=60), None),
    ]
    assert f.status_text == "Waiting for the go-ahead to build"


def test_a_failed_build_closes_its_open_wait_where_last_seen(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Show a count.")
    fake.build("b1", "FEAT-0001", "c1", status="FAILED", completed=50, error="boom")
    fake.gate_pause("b1", 21)
    fake.gate_pause("b1", 22)
    f = _load(fake, tmp_path).feature("FEAT-0001")
    assert f is not None and f.outcome is Outcome.BUILD_FAILED and f.station == BUILDING
    gate = [w for w in f.waits if w.kind == "build approval"]
    assert gate[0].ended_at == T0 + timedelta(minutes=22) and f.open_wait() is None


def test_a_person_skip_closes_the_wait(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("c1", "Show a count.")
    fake.build("b1", "FEAT-0001", "c1", status="RUNNING", completed=None)
    fake.gate_pause("b1", 21)
    fake.stage("b1", "autobuild", "cli-skip", "SKIPPED", 33)
    f = _load(fake, tmp_path).feature("FEAT-0001")
    assert f is not None and [w.ended_at for w in f.waits] == [T0 + timedelta(minutes=33)]


def test_a_record_quiet_for_a_week_is_still_in_progress(fake: ForgeFake, tmp_path: Path) -> None:
    """Codex review, 18 October 2026: age alone never ends a feature. Forge still records this one
    as waiting for its build, so it stays in progress (and is shown as stalled, in the issues)."""
    fake.planning("c1", "An old one.", state="BUILD_QUEUED")
    f = _load(fake, tmp_path, now=T0 + timedelta(days=9)).feature("c1")
    assert f is not None and f.outcome is Outcome.IN_PROGRESS and f.in_progress
    assert f.status_text == "Plan approved; waiting for the build to start" and f.ended_at is None


def test_a_queue_item_that_never_reached_planning(fake: ForgeFake, tmp_path: Path) -> None:
    fake.queue("q1", "Blocked thing.", status="BLOCKED", reason="Not allowed here. Because.")
    fake.queue("q2", "What is the time?", status="DONE", kind="question")
    factory = _load(fake, tmp_path)
    f = factory.feature("q1")
    assert f is not None and f.outcome is Outcome.STOPPED_IN_QUEUE
    assert f.status_text == "Stopped in the queue: Not allowed here."
    assert factory.feature("q2") is None  # a question is not a feature


def test_titles_are_the_first_sentence_cut_at_a_word() -> None:
    assert title_from("Short one. Then more.", "x") == "Short one."
    long = "Add a done flag to notes: a new migration adds a done column that is false by default, and more words"
    t = title_from(long, "x")
    assert len(t) <= 90 and t.endswith("…") and not t[:-1].endswith(" ")
    assert title_from("", "FEAT-1") == "FEAT-1"


def test_newest_first(fake: ForgeFake, tmp_path: Path) -> None:
    fake.planning("old", "Old.", start=0)
    fake.planning("new", "New.", start=30)
    assert [f.key for f in _load(fake, tmp_path).features] == ["new", "old"]
