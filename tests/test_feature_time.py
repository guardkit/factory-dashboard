"""Time per feature (requirement 6): elapsed, machine and waiting time, each case worked out by hand
from the rows (design of 17 October 2026, "Checks for part B").

Fake times are minutes after T0. By default a planning run goes 0 → 20 and its build 21 → 80.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from backend.readmodel import dbread
from backend.readmodel.factory import Feature, load_factory
from backend.readmodel.timing import FeatureTime, build_start, feature_time

from tests import forge_fake
from tests.forge_fake import ForgeFake, delivered_feature, store_from

MIN = 60.0


def _time(tmp_path: Path, fake: ForgeFake, key: str, now_min: float = 300) -> tuple[Feature, FeatureTime]:
    now = forge_fake.T0 + timedelta(minutes=now_min)
    conn = store_from(fake, tmp_path / "readmodel.db", now)
    f = load_factory(conn, now).feature(key)
    conn.close()
    assert f is not None
    t = feature_time(f, now)
    assert t is not None
    return f, t


def _check_sum(t: FeatureTime) -> None:
    """Machine + waiting + between steps = elapsed, always."""
    assert t.machine + t.waiting + t.part("not_placed") == t.elapsed


def test_a_merged_feature(tmp_path: Path) -> None:
    fake = ForgeFake(tmp_path / "forge.db")
    delivered_feature(fake, 1)  # spec gate 5 → 6, offer 81, decision 82, merged and checked 90
    _f, t = _time(tmp_path, fake, "FEAT-0001")
    assert t.elapsed == 90 * MIN and not t.so_far
    assert t.part("planning") == 19 * MIN           # 0 → 20, minus the 1-minute spec approval
    assert t.part("building") == 59 * MIN           # 21 → 80
    assert t.part("merge_check") == 8 * MIN         # decision 82 → merged 90
    assert t.part("approvals") == 1 * MIN and t.part("merge_decision") == 1 * MIN
    assert t.part("not_placed") == 2 * MIN          # 20 → 21 and 80 → 81
    _check_sum(t)


def test_three_re_offers_of_one_merge_card_are_one_wait(tmp_path: Path) -> None:
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "A thing.")
    fake.build("b1", "FEAT-0001", "c1")
    for minute in (81, 83, 85):                      # Forge re-posts the card after restarts
        fake.merge_offer("b1", minute)
    fake.merge_decision("b1", 90)
    fake.merged("b1", 95)
    _f, t = _time(tmp_path, fake, "FEAT-0001")
    assert t.part("merge_decision") == 9 * MIN      # 81 → 90, once
    assert t.part("merge_check") == 5 * MIN
    assert t.elapsed == 95 * MIN
    _check_sum(t)


def test_a_build_still_paused_at_the_gate(tmp_path: Path) -> None:
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "A thing.")
    fake.build("b1", "FEAT-0001", "c1", status="PAUSED", completed=None)
    fake.gate_pause("b1", 30)
    _f, t = _time(tmp_path, fake, "FEAT-0001", now_min=60)
    assert t.so_far and t.elapsed == 60 * MIN
    assert t.part("approvals") == 30 * MIN          # 30 → now, still open
    assert any(p.key == "approvals" and p.open for p in t.parts)
    assert t.part("building") == 9 * MIN            # 21 → 60, minus the 30-minute wait
    _check_sum(t)


def test_a_persons_skip_closes_the_gate_wait(tmp_path: Path) -> None:
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "A thing.")
    fake.build("b1", "FEAT-0001", "c1")
    fake.gate_pause("b1", 30)
    fake.stage("b1", "autobuild", "cli-skip", "SKIPPED", 40)
    fake.merge_offer("b1", 81)
    fake.merge_decision("b1", 82, "reject")
    _f, t = _time(tmp_path, fake, "FEAT-0001")
    assert t.part("approvals") == 10 * MIN
    assert t.part("building") == 49 * MIN           # 59 minutes, minus the 10-minute wait
    _check_sum(t)


def test_work_after_a_pause_then_a_second_pause(tmp_path: Path) -> None:
    """The first wait closes at the last moment it was seen waiting (its own pause row), so only the
    second, 40 → 50, counts."""
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "A thing.")
    fake.build("b1", "FEAT-0001", "c1")
    fake.gate_pause("b1", 30)
    fake.work("b1", 35)
    fake.gate_pause("b1", 40)
    fake.gate_resume("b1", 50)
    _f, t = _time(tmp_path, fake, "FEAT-0001")
    assert t.part("approvals") == 10 * MIN
    assert t.part("building") == 49 * MIN
    _check_sum(t)


def test_a_failed_build_with_an_open_wait(tmp_path: Path) -> None:
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "A thing.")
    fake.build("b1", "FEAT-0001", "c1", status="FAILED", completed=60, error="Tests failed.")
    fake.gate_pause("b1", 30)
    _f, t = _time(tmp_path, fake, "FEAT-0001")
    assert not t.so_far and t.elapsed == 60 * MIN   # ends when the build failed
    assert t.part("approvals") == 0                 # closed at the last moment it was seen waiting
    assert t.part("building") == 39 * MIN
    _check_sum(t)


def test_a_declined_merge(tmp_path: Path) -> None:
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "A thing.")
    fake.build("b1", "FEAT-0001", "c1")
    fake.merge_offer("b1", 81)
    fake.merge_decision("b1", 85, "reject")
    _f, t = _time(tmp_path, fake, "FEAT-0001")
    assert t.elapsed == 85 * MIN                    # ends at the decision
    assert t.part("merge_decision") == 4 * MIN
    assert t.part("merge_check") == 0
    _check_sum(t)


def test_queue_time_counts_as_waiting(tmp_path: Path) -> None:
    fake = ForgeFake(tmp_path / "forge.db")
    fake.queue("c1", "A thing.", minute=-30)         # queued 30 minutes before planning started
    fake.planning("c1", "A thing.")
    fake.build("b1", "FEAT-0001", "c1", queued=21, started=41, completed=80)   # 20 minutes in the build queue
    fake.merge_offer("b1", 81)
    fake.merge_decision("b1", 82, "reject")
    _f, t = _time(tmp_path, fake, "FEAT-0001")
    # The fake admits a queue item the moment it is queued; the planning run is queued at 0.
    assert t.part("queue") == 20 * MIN
    assert t.part("building") == 39 * MIN
    _check_sum(t)


def test_a_build_whose_start_was_written_at_the_end_starts_at_its_runners_first_row(tmp_path: Path) -> None:
    """Forge's 17 October mission builds recorded started_at at completion; the runner's own stage row
    shows when the build really started."""
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "A thing.")
    fake.build("b1", "FEAT-0001", "c1", queued=21, started=79.99, completed=80)
    fake.stage("b1", "autobuild", "autobuild_runner", "PASSED", 21.01, {"mission_gate": {}})
    f, t = _time(tmp_path, fake, "FEAT-0001")
    assert build_start(f.builds[0]) == forge_fake.T0 + timedelta(minutes=21.01)
    assert round(t.part("building")) == round(58.99 * MIN)
    assert t.part("queue") < 1


def test_an_auto_approved_gate_shows_no_wait(tmp_path: Path) -> None:
    fake = ForgeFake(tmp_path / "forge.db")
    delivered_feature(fake, 1)
    fake.planning("c2", "Another.")
    fake.build("b2", "FEAT-0002", "c2")
    fake.gate_pause("b2", 30)
    fake.gate_resume("b2", 30)                       # a card policy answers at once
    _f, t = _time(tmp_path, fake, "FEAT-0002")
    assert t.part("approvals") == 0


def test_the_time_slot_puts_waiting_first_by_size(tmp_path: Path) -> None:
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "A thing.")
    fake.build("b1", "FEAT-0001", "c1")
    fake.gate_pause("b1", 30)
    fake.gate_resume("b1", 31)
    fake.merge_offer("b1", 81)
    fake.merge_decision("b1", 91)
    fake.merged("b1", 95)
    _f, t = _time(tmp_path, fake, "FEAT-0001")
    slot = dbread.time_slot(t)
    assert slot.measured and not slot.so_far
    assert [s.label for s in slot.waiting_parts] == ["Waiting for the merge decision", "Waiting for approvals"]
    assert slot.waiting == "11 min" and slot.elapsed == "1 h 35 min"
    assert {s.seg for s in slot.segments} >= {"plan", "build", "review", "wait"}
    assert dbread.time_slot(None).state == "not_measured"


# --- coach round 1: overlapping builds, builds closed by a later clean-up, and the invariant ------------

DAY = 24 * 60.0   # minutes


def test_overlapping_builds_are_counted_once(tmp_path: Path) -> None:
    """Two attempts recorded as running at the same time: machine time is their union, never the sum."""
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "A thing.")
    fake.build("b1", "FEAT-0001", "c1", status="FAILED", queued=21, started=21, completed=80, error="x")
    fake.build("b2", "FEAT-0001", "c1-fix", status="FAILED", queued=30, started=30, completed=90, error="y")
    _f, t = _time(tmp_path, fake, "FEAT-0001")
    assert t.elapsed == 90 * MIN
    assert t.part("building") == 69 * MIN          # 21 → 90, not 59 + 60
    assert t.machine + t.waiting <= t.elapsed
    _check_sum(t)


def test_a_build_closed_by_the_clean_up_months_later_is_not_measured(tmp_path: Path) -> None:
    """The 1 October shape: rows in May, started_at = completed_at = the clean-up."""
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "An old thing.", start=-140 * DAY, end=-140 * DAY + 20)
    for n in range(3):
        bid = f"b{n}"
        fake.build(bid, "FEAT-0001", f"c1-{n}", status="FAILED", queued=-140 * DAY + 21 + n,
                   started=-15 * DAY, completed=-15 * DAY, error="sandbox-required")
        fake.work(bid, -140 * DAY + 21 + n)
    f, t = _time(tmp_path, fake, "FEAT-0001")
    assert t.unmeasured.startswith("Not measured: a build was closed in a clean-up on 1 October 2026, 124 days")
    assert t.parts == () and t.machine == 0
    slot = dbread.time_slot(t)
    assert slot.state == "not_measured" and slot.reason == t.unmeasured and slot.machine == ""
    assert build_start(f.builds[0]) == f.builds[0].started_at   # the 17 October workaround is not applied


def test_a_real_start_and_a_clean_up_completion_is_not_measured(tmp_path: Path) -> None:
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "A July thing.", start=-84 * DAY, end=-84 * DAY + 20)
    fake.build("b1", "FEAT-0001", "c1", status="FAILED", queued=-84 * DAY + 21, started=-84 * DAY + 21,
               completed=-15 * DAY, error="x")
    fake.work("b1", -84 * DAY + 24)
    _f, t = _time(tmp_path, fake, "FEAT-0001")
    assert t.unmeasured and t.machine == 0


def test_a_long_build_with_no_rows_near_its_end_is_still_measured(tmp_path: Path) -> None:
    """The runner writes rows at the start: 8 hours with no row near the end is a real build."""
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "A slow thing.")
    fake.build("b1", "FEAT-0001", "c1", status="CANCELLED", queued=21, started=21, completed=21 + 8 * 60)
    fake.work("b1", 22)
    _f, t = _time(tmp_path, fake, "FEAT-0001")
    assert not t.unmeasured and t.part("building") == 8 * 60 * MIN


def test_the_start_written_at_the_end_needs_a_row_backing_the_end(tmp_path: Path) -> None:
    """Started = completed, runner rows at queue time, and no row near the end (a clean-up, not the
    17 October defect): the workaround is not applied."""
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "A thing.")
    fake.build("b1", "FEAT-0001", "c1", queued=21, started=21 + 3 * DAY, completed=21 + 3 * DAY)
    fake.stage("b1", "autobuild", "autobuild_runner", "PASSED", 21.01, {"mission_gate": {}})
    f, t = _time(tmp_path, fake, "FEAT-0001", now_min=10 * DAY)
    assert build_start(f.builds[0]) == f.builds[0].started_at
    assert t.unmeasured


def test_a_project_sum_leaves_out_features_it_cannot_measure(tmp_path: Path) -> None:
    fake = ForgeFake(tmp_path / "forge.db")
    delivered_feature(fake, 1)
    fake.planning("c2", "An old thing.", start=-140 * DAY, end=-140 * DAY + 20)
    fake.build("b2", "FEAT-0002", "c2", status="FAILED", queued=-140 * DAY + 21, started=-15 * DAY,
               completed=-15 * DAY, error="x")
    fake.work("b2", -140 * DAY + 21)
    now = forge_fake.T0 + timedelta(minutes=300)
    conn = store_from(fake, tmp_path / "readmodel.db", now)
    factory = load_factory(conn, now)
    conn.close()
    slot = dbread.sum_time(feature_time(f, now) for f in factory.features)
    assert slot.measured and slot.elapsed_secs == 90 * MIN and slot.not_measured_count == 1
    assert slot.reason.startswith("1 feature is left out")


def _many_shapes(fake: ForgeFake) -> None:
    """Live-shaped records: delivered, declined, paused, failed with an open wait, overlapping and fix
    attempts, mission parts on a branch, a mission that was not merged, the 17 October start defect,
    builds closed in a clean-up, queued work and a planning run that failed."""
    delivered_feature(fake, 1)
    fake.planning("c2", "Declined.")
    fake.build("b2", "FEAT-0002", "c2")
    fake.merge_offer("b2", 81)
    fake.merge_offer("b2", 84)
    fake.merge_decision("b2", 86, "reject")
    fake.planning("c3", "Paused.")
    fake.build("b3", "FEAT-0003", "c3", status="PAUSED", completed=None)
    fake.gate_pause("b3", 30)
    fake.work("b3", 40)
    fake.gate_pause("b3", 50)
    fake.planning("c4", "Failed with a wait.")
    fake.build("b4", "FEAT-0004", "c4", status="FAILED", completed=60, error="x")
    fake.gate_pause("b4", 30)
    fake.build("b4f", "FEAT-0004", "fix-b4", status="COMPLETE", queued=55, started=58, completed=120)
    fake.merge_offer("b4f", 121)
    fake.merge_decision("b4f", 125)
    fake.merged("b4f", 130)
    fake.mission("m-1", "A mission", "mc")
    fake.planning("mc-A-a1", "Part A.", start=100, end=110)
    fake.build("bA", "FEAT-000A", "mc-A-a1", queued=110, started=150, completed=150.001, mission_id="m-1",
               mission_role="feature")
    fake.stage("bA", "autobuild", "autobuild_runner", "PASSED", 110.01, {"mission_gate": {}})
    fake.merge_decision("bA", 150.002)
    fake.merged("bA", 152, mission_branch=True)
    fake.mission("m-2", "Not merged", "md", status="NOT_MERGED", closed=200)
    fake.planning("md-A-a1", "Part B.", start=100, end=110)
    fake.build("bB", "FEAT-000B", "md-A-a1", queued=110, started=111, completed=150, mission_id="m-2",
               mission_role="feature")
    fake.merge_decision("bB", 150)
    fake.merged("bB", 151, mission_branch=True)
    fake.planning("old", "Cleaned up.", start=-140 * DAY, end=-140 * DAY + 20)
    for n in range(4):
        fake.build(f"bo{n}", "FEAT-00OL", f"old-{n}", status="FAILED", queued=-140 * DAY + 21 + n,
                   started=-15 * DAY, completed=-15 * DAY, error="x")
        fake.work(f"bo{n}", -140 * DAY + 21 + n)
    fake.queue("q1", "Still queued.", status="QUEUED", minute=200)
    fake.planning("pf", "Planning failed.", state="FAILED", start=10, end=15, error="No.")


@pytest.mark.parametrize("now_min", [60, 151, 300, 10 * DAY])
def test_machine_plus_waiting_never_exceeds_elapsed_on_any_shape(tmp_path: Path, now_min: float) -> None:
    fake = ForgeFake(tmp_path / "forge.db")
    _many_shapes(fake)
    now = forge_fake.T0 + timedelta(minutes=now_min)
    conn = store_from(fake, tmp_path / "readmodel.db", now)
    factory = load_factory(conn, now)
    conn.close()
    checked = 0
    for f in factory.features:
        t = feature_time(f, now)
        if t is None:
            continue
        assert t.machine + t.waiting <= t.elapsed + 1e-6, (f.key, t)
        assert all(p.seconds >= 0 for p in t.parts), f.key
        if not t.unmeasured:
            assert abs(t.machine + t.waiting + t.part("not_placed") - t.elapsed) < 1e-6, f.key
        checked += 1
    assert checked >= 8
