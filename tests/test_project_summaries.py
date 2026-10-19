"""The summary file for the Linear door (design "Linear front door", 11.5): its contract, that its
figures are the pages' own, the health mapping, Linear attempts' merge cost, the cost of every build
that reached main (11.8), and the atomic write.
All through the real feed code over a fake Forge record and a fake spend log."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import sqlite3
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from backend.projector import forge_mirror
from backend.projector.cost_import import import_once
from backend.projector.store import iso, open_rw, write_heartbeat
from backend.projector.summary import (
    HEALTH_OF_BAND,
    SUMMARY_FILE,
    build_summary,
    linear_attempts,
    merges_on_main,
    mission_parts,
    project_health,
    remove_leftovers,
    summary_loop,
    summary_path,
    write_atomic,
    write_summary,
)
from backend.readmodel import dbread
from backend.readmodel.costs import load_cost_book
from backend.readmodel.factory import load_factory
from backend.readmodel.viewmodels import SpendSlot

from tests import forge_fake
from tests.forge_fake import REPO, ForgeFake, delivered_feature, store_from
from tests.spend_fake import IN_PRICE, OUT_PRICE, FakeSpend

T = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)    # after calls began to be tagged (17 October)
NOW = T + timedelta(minutes=200)
ISSUE = "lin-APP-12-1"                          # a single issue's attempt (the door's id shape)
MISSION = "lin-m-" + "0" * 32 + "-1"            # a mission's attempt
MERGE = "mission-merge-corr"                    # the mission's own merge build
PART = MISSION + "-APP13-a1"                    # Forge's part attempt id: <mission correlation id>-<KEY>-a<n>
FAILED_PART = MISSION + "-APP14-a1"             # a part that failed in planning, never built (keys: [A-Z0-9]{1,12})


def _cost(prompt: int, completion: int) -> float:
    return prompt * IN_PRICE + completion * OUT_PRICE


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(forge_fake, "T0", T)
    fake = ForgeFake(tmp_path / "forge.db")
    delivered_feature(fake, 1)                                       # not from Linear
    # A single Linear issue: queued, planned and built under the door's correlation id, merged.
    fake.queue(ISSUE, "Show a Clear button.", minute=-1)
    fake.planning(ISSUE, "Show a Clear button.")
    fake.build("bL", "FEAT-0012", ISSUE)
    fake.merge_decision("bL", 82)
    fake.merged("bL", 85)
    # A Linear mission: one part joined the mission's branch, another failed in planning, then the
    # mission merged what it had.
    fake.mission("m-2", "Mark notes as done", MISSION, status="MERGED", closed=170)
    fake.planning(PART, "Add a done flag.", start=100, end=110)
    fake.build("bP", "FEAT-0013", PART, queued=110, started=110, completed=150, mission_id="m-2",
               mission_role="feature")
    fake.planning(FAILED_PART, "Filter by done.", state="FAILED", start=100, end=130, error="The planner gave up.")
    fake.merge_decision("bP", 150)
    fake.merged("bP", 152, mission_branch=True)
    fake.build("bM", "FEAT-0013", MERGE, queued=160, started=160, completed=165, mission_id="m-2",
               mission_role="mission-merge")
    fake.merged("bM", 166)
    # Still building, so the project has something in progress.
    fake.planning("c-run", "Sort the notes.", start=180, end=185)
    fake.build("bR", "FEAT-0020", "c-run", status="RUNNING", queued=186, started=186, completed=None)
    # A Linear attempt from long ago: left out of the attempts.
    fake.planning("lin-APP-1-1", "An old issue.", state="CANCELLED", start=-40 * 24 * 60, end=-40 * 24 * 60 + 20)

    spend = FakeSpend(T)
    spend.add("c1", 30, "c-1", prompt=1_000, completion=100)
    spend.add("l1", 30, ISSUE, prompt=10_000, completion=1_000)
    spend.add("l2", 60, ISSUE, prompt=10_000, completion=1_000)
    spend.add("p1", 120, PART, prompt=20_000, completion=2_000)
    spend.add("f1", 115, FAILED_PART, prompt=5_000, completion=500)
    spend.add("mm", 158, MISSION, prompt=3_000, completion=300)
    spend.add("mb", 162, MERGE, prompt=4_000, completion=400)
    path = tmp_path / "readmodel.db"
    conn = store_from(fake, path, NOW)
    import_once(conn, spend, NOW)
    conn.close()
    return path


def test_the_contract_has_every_field(store: Path) -> None:
    s = build_summary(store, NOW)
    assert s["version"] == 1 and s["written_at"] == iso(NOW)
    assert s["forge"] == {"last_success_at": iso(NOW), "stale": False, "valid_until": iso(NOW + timedelta(seconds=60))}
    assert s["cost"] == {"read_at": iso(NOW), "state": "fresh", "stale_after_seconds": 600}
    p = s["projects"][REPO]
    assert set(p) == {"delivered_total", "delivered_7d", "in_progress", "open_issues_total", "open_issues_words",
                      "health", "spend_total", "spend_week"}
    assert set(p["spend_total"]) == {f.name for f in dataclasses.fields(SpendSlot)}
    json.dumps(s)                                   # plain JSON throughout


def test_project_figures_are_the_project_pages_own(store: Path) -> None:
    p = build_summary(store, NOW)["projects"][REPO]
    view = dbread.project_view(store, REPO, NOW)
    assert (p["delivered_total"], p["delivered_7d"], p["in_progress"], p["open_issues_total"]) == (
        view.delivered_total, view.delivered_7d, view.in_progress, view.open_issues_total)
    assert p["in_progress"] == 1 and p["delivered_total"] == 3
    assert p["open_issues_words"] == view.open_issues_words
    # A SpendSlot survives the round trip, every field.
    assert SpendSlot(**p["spend_total"]) == view.spend and SpendSlot(**p["spend_week"]) == view.spend_week
    assert view.spend.state == "measured" and view.spend.calls == 7


def test_health_is_the_worst_band_of_in_progress_features(store: Path) -> None:
    view = dbread.project_view(store, REPO, NOW)
    running = [r.band.band for r in view.features.rows if r.in_progress]
    assert running == ["G"]
    assert build_summary(store, NOW)["projects"][REPO]["health"] == "on_track"


@pytest.mark.parametrize(("bands", "health"), [
    (["G", "G"], "on_track"),
    (["G", "A"], "at_risk"),
    (["A", "R", "G"], "off_track"),
    (["—", "A"], "at_risk"),        # an unmeasured feature is left out of the worst
    (["—"], "unknown"),             # nothing measured: not shown as on track
    ([], "on_track"),               # nothing in progress
])
def test_the_band_mapping(bands: list[str], health: str) -> None:
    rows = [SimpleNamespace(in_progress=True, band=SimpleNamespace(band=b)) for b in bands]
    rows.append(SimpleNamespace(in_progress=False, band=SimpleNamespace(band="R")))   # finished: ignored
    view = SimpleNamespace(features=SimpleNamespace(rows=rows))
    assert project_health(view, forge_stale=False) == health  # type: ignore[arg-type]
    assert project_health(view, forge_stale=True) == "unknown"  # type: ignore[arg-type]
    assert HEALTH_OF_BAND == {"G": "on_track", "A": "at_risk", "R": "off_track"}


def test_health_is_unknown_when_the_forge_copy_is_stale(store: Path) -> None:
    later = NOW + timedelta(minutes=10)
    s = build_summary(store, later)
    assert s["forge"]["stale"] is True and s["forge"]["last_success_at"] == iso(NOW)
    assert s["projects"][REPO]["health"] == "unknown"
    assert s["cost"]["state"] == "stale"


def test_valid_until_is_the_earlier_of_the_two_limits(store: Path) -> None:
    conn = open_rw(store)
    write_heartbeat(conn, NOW + timedelta(seconds=100))     # the feed alive later than the last read
    conn.close()
    s = build_summary(store, NOW + timedelta(seconds=100))
    assert s["forge"]["valid_until"] == iso(NOW + timedelta(seconds=120))   # the read's limit comes first
    assert s["forge"]["stale"] is False


def test_linear_attempts_carry_their_merge_cost(store: Path) -> None:
    attempts = build_summary(store, NOW)["attempts"]
    assert set(attempts) == {ISSUE, MISSION}        # not c-1, not the 40-day-old attempt
    issue = SpendSlot(**attempts[ISSUE])
    assert issue.state == "measured" and issue.calls == 2 and issue.scope == "attempt"
    assert issue.amount_usd == pytest.approx(2 * _cost(10_000, 1_000))
    # The mission: every part's calls, built or not (R1), plus its own correlation and merge build
    # tags, each once. The failed part's attempt is not a single-issue attempt of its own.
    mission = SpendSlot(**attempts[MISSION])
    assert mission.calls == 4 and mission.state == "measured" and "not counted" not in mission.reason
    assert mission.amount_usd == pytest.approx(_cost(20_000, 2_000) + _cost(5_000, 500) + _cost(3_000, 300)
                                               + _cost(4_000, 400))


def test_a_mission_part_that_never_built_belongs_to_its_mission(store: Path) -> None:
    conn = dbread.snapshot(store)
    try:
        attempts = linear_attempts(load_factory(conn, NOW), NOW)
    finally:
        conn.close()
    mission, parts = attempts[MISSION]
    assert mission is not None and mission.id == "m-2"
    assert sorted(f.key for f in parts) == sorted(["FEAT-0013", FAILED_PART])
    assert FAILED_PART not in attempts and PART not in attempts


def test_an_unmeasured_member_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mission begun after tagging with one part sent before it: that part is named, not hidden."""
    monkeypatch.setattr(forge_fake, "T0", T)
    fake = ForgeFake(tmp_path / "forge.db")
    fake.mission("m-3", "Tags", MISSION, status="MERGED", closed=170)
    fake.planning(PART, "Add tags.", start=100, end=110)
    fake.planning(FAILED_PART, "Colour the tags.", state="FAILED", start=-3 * 24 * 60, end=-3 * 24 * 60 + 5)
    path = tmp_path / "readmodel.db"
    conn = store_from(fake, path, NOW)
    spend = FakeSpend(T)
    spend.add("a", 105, PART, prompt=1_000, completion=100)
    import_once(conn, spend, NOW)
    conn.close()
    slot = SpendSlot(**build_summary(path, NOW)["attempts"][MISSION])
    assert slot.state == "measured" and slot.calls == 1
    assert slot.reason.startswith("1 of its 2 features was sent before calls were tagged by feature, so it is not "
                                  "counted: Colour the tags.")


def test_an_attempt_with_no_cost_data_says_not_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forge_fake, "T0", T)
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning(ISSUE, "Show a Clear button.")
    path = tmp_path / "readmodel.db"
    store_from(fake, path, NOW).close()             # no spend log import ever
    s = build_summary(path, NOW)
    assert s["cost"] == {"read_at": None, "state": "never", "stale_after_seconds": 600}
    assert SpendSlot(**s["attempts"][ISSUE]).state == "not_available"
    assert s["projects"][REPO]["spend_total"]["state"] == "not_available"


def test_an_attempt_from_before_tagging_is_not_measured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forge_fake, "T0", T - timedelta(days=3))
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning(ISSUE, "Show a Clear button.")
    path = tmp_path / "readmodel.db"
    conn = store_from(fake, path, NOW)
    import_once(conn, FakeSpend(T), NOW)
    conn.close()
    slot = SpendSlot(**build_summary(path, NOW)["attempts"][ISSUE])
    assert slot.state == "not_measured" and slot.amount_usd is None
    assert "not tagged by feature" in slot.reason


def test_projects_view_reads_on_a_given_snapshot_and_leaves_it_open(store: Path) -> None:
    conn = dbread.snapshot(store)
    try:
        view = dbread.projects_view(store, NOW, conn=conn)
        assert [r.project for r in view.rows] == [REPO]
        assert view.rows[0].spend == dbread.projects_view(store, NOW).rows[0].spend
        conn.execute("SELECT 1").fetchone()           # still open
    finally:
        conn.close()


def test_the_file_is_written_atomically_and_readable(store: Path) -> None:
    out = summary_path(store)
    assert out == store.parent / SUMMARY_FILE
    data = write_summary(store, NOW)
    assert json.loads(out.read_text(encoding="utf-8")) == data
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o644
    assert [p.name for p in out.parent.iterdir() if p.name.endswith(".tmp")] == []
    # A write that fails half way leaves the old file whole and no temporary file behind.
    before = out.read_bytes()
    with pytest.raises(TypeError):
        write_atomic(out, {"version": 1, "bad": object()})
    assert out.read_bytes() == before
    assert [p.name for p in out.parent.iterdir() if p.name.endswith(".tmp")] == []


# --- coach D3: the summary has its own loop, so a slow one never delays the Forge copy -------------


def test_a_slow_summary_does_not_delay_the_forge_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    passes: list[float] = []
    monkeypatch.setattr(forge_mirror, "mirror_once", lambda *_a, **_k: passes.append(time.monotonic()))
    monkeypatch.setattr(forge_mirror, "prune_change_log", lambda *_a, **_k: None)
    monkeypatch.setattr(forge_mirror, "forge_db_path", lambda: tmp_path / "forge.db")
    writes: list[float] = []

    def slow_write(db_path: Path, now: datetime | None = None) -> None:  # the writer's own names
        writes.append(time.monotonic())
        time.sleep(0.6)                                  # far longer than the whole window below

    async def run() -> None:
        loops = asyncio.gather(forge_mirror.mirror_loop(None, interval=0.01),  # type: ignore[arg-type]
                               summary_loop(tmp_path / "readmodel.db", interval=0.01, write=slow_write))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(loops, timeout=0.3)

    asyncio.run(run())
    assert len(writes) == 1                              # the summary is still in its first write
    assert len(passes) >= 10                             # and the Forge copy kept its own pace


def test_a_failing_summary_is_skipped_and_retried(tmp_path: Path) -> None:
    calls: list[int] = []

    def failing(db_path: Path, now: datetime | None = None) -> None:  # the writer's own names
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    async def run() -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(summary_loop(tmp_path / "readmodel.db", interval=0.01, write=failing), timeout=0.2)

    asyncio.run(run())
    assert len(calls) >= 3


def test_leftover_temporary_files_are_removed_at_start(tmp_path: Path) -> None:
    left = tmp_path / f".{SUMMARY_FILE}.abc123.tmp"
    left.write_text("{", encoding="utf-8")
    keep = tmp_path / SUMMARY_FILE
    keep.write_text("{}", encoding="utf-8")
    remove_leftovers(tmp_path)
    assert not left.exists() and keep.exists()


# --- design 11.8: the cost of every build that reached main, whatever started it -----------------


def test_a_merged_single_build_carries_its_features_cost(store: Path) -> None:
    s = build_summary(store, NOW)
    merges = s["merges"]
    # On main: the plain feature, the Linear issue's build and the mission's merge build. The mission
    # part that joined only the mission's branch, and the build still running, are not.
    assert set(merges) == {"build-FEAT-0001", "bL", "bM"}
    plain = SpendSlot(**merges["build-FEAT-0001"])
    assert plain.state == "measured" and plain.calls == 1 and plain.scope == "merge"
    assert plain.amount_usd == pytest.approx(_cost(1_000, 100))
    issue = SpendSlot(**merges["bL"])                 # queue, planning and build tags, each once
    assert issue.calls == 2 and issue.amount_usd == pytest.approx(2 * _cost(10_000, 1_000))
    assert set(merges["bL"]) == {f.name for f in dataclasses.fields(SpendSlot)}


def test_a_linear_missions_merge_costs_the_same_as_its_attempt(store: Path) -> None:
    s = build_summary(store, NOW)
    merge, attempt = SpendSlot(**s["merges"]["bM"]), SpendSlot(**s["attempts"][MISSION])
    assert merge.scope == "merge" and attempt.scope == "attempt"
    assert dataclasses.replace(merge, scope="attempt") == attempt


def test_any_missions_merge_costs_every_part_and_the_missions_own_calls(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mission not from Linear: one part built onto the mission's branch, one failed in planning
    and never built (found only by the mission's correlation prefix), plus the mission's own calls
    and its merge build's."""
    monkeypatch.setattr(forge_fake, "T0", T)
    corr = "mission-7f3a"
    built, failed, merge_corr = corr + "-K1-a1", corr + "-K2-a1", "mission-7f3a-merge"
    fake = ForgeFake(tmp_path / "forge.db")
    fake.mission("m-7", "Archive old notes", corr, status="MERGED", closed=170)
    fake.planning(built, "Add an archive flag.", start=100, end=110)
    fake.build("b1", "FEAT-0071", built, queued=110, started=110, completed=150, mission_id="m-7",
               mission_role="feature", target_branch="mission/m-7")
    fake.merge_decision("b1", 150)
    fake.merged("b1", 152, mission_branch=True)
    fake.planning(failed, "Hide archived notes.", state="FAILED", start=100, end=130, error="The planner gave up.")
    fake.build("bM7", "FEAT-0071", merge_corr, queued=160, started=160, completed=165, mission_id="m-7",
               mission_role="mission-merge")
    fake.merge_decision("bM7", 165)
    fake.merged("bM7", 166)
    spend = FakeSpend(T)
    spend.add("p", 120, built, prompt=20_000, completion=2_000)
    spend.add("f", 115, failed, prompt=5_000, completion=500)
    spend.add("m", 158, corr, prompt=3_000, completion=300)
    spend.add("mb", 162, merge_corr, prompt=4_000, completion=400)
    spend.add("other", 50, "c-elsewhere", prompt=9_000, completion=900)    # not this mission's
    path = tmp_path / "readmodel.db"
    conn = store_from(fake, path, NOW)
    import_once(conn, spend, NOW)
    conn.close()

    s = build_summary(path, NOW)
    assert set(s["merges"]) == {"bM7"}                 # the part joined the mission's branch, not main
    assert s["attempts"] == {}                         # not a Linear attempt: attempts are unchanged
    slot = SpendSlot(**s["merges"]["bM7"])
    assert slot.state == "measured" and slot.calls == 4 and "not counted" not in slot.reason
    assert slot.amount_usd == pytest.approx(_cost(20_000, 2_000) + _cost(5_000, 500) + _cost(3_000, 300)
                                            + _cost(4_000, 400))
    conn = dbread.snapshot(path)
    try:
        factory = load_factory(conn, NOW)
    finally:
        conn.close()
    mission, parts = merges_on_main(factory, NOW)["bM7"]
    assert mission is not None and mission.id == "m-7"
    assert sorted(f.key for f in parts) == sorted(["FEAT-0071", failed])
    assert sorted(f.key for f in mission_parts(factory)["m-7"]) == sorted(["FEAT-0071", failed])


def test_a_build_not_on_main_has_no_merge_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forge_fake, "T0", T)
    fake = ForgeFake(tmp_path / "forge.db")
    for n, corr in enumerate(("c-pending", "c-elsewhere", "c-declined", "c-nulltarget", "c-reported"), 1):
        fake.planning(corr, f"Feature {n}.")
        fake.build(f"b{n}", f"FEAT-{n:04d}", corr)
        fake.merge_decision(f"b{n}", 82)
    fake.publication("b1", 85, result="publication pending")                # the word given, not yet published
    fake.publication("b2", 85, target="release")                            # published, to another branch
    fake.stage("b3", "the merge word", "merge_deploy_decision", "SKIPPED", 83,
               {"merge_decision": {"decision": "reject", "decided_by": "U1"}})  # declined: no record
    fake.publication("b4", 85, result="published, deployment pending", target=None)  # the build's own target
    fake.stage("b5", "merge-deploy", "merge_deploy_executor", "PASSED", 85, {"merged_sha": "abc"})  # report only
    path = tmp_path / "readmodel.db"
    store_from(fake, path, NOW).close()
    assert set(build_summary(path, NOW)["merges"]) == {"b4"}


def test_a_merge_older_than_30_days_is_left_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forge_fake, "T0", T)
    fake = ForgeFake(tmp_path / "forge.db")
    old = -31 * 24 * 60
    fake.planning("c-old", "An old feature.", start=old, end=old + 20)
    fake.build("bOld", "FEAT-0001", "c-old", queued=old + 21, started=old + 21, completed=old + 80)
    fake.merged("bOld", old + 90)
    recent = -29 * 24 * 60
    fake.planning("c-recent", "A recent feature.", start=recent, end=recent + 20)
    fake.build("bNew", "FEAT-0002", "c-recent", queued=recent + 21, started=recent + 21, completed=recent + 80)
    fake.merged("bNew", recent + 90)
    path = tmp_path / "readmodel.db"
    store_from(fake, path, NOW).close()
    assert set(build_summary(path, NOW)["merges"]) == {"bNew"}


def test_attempts_are_unchanged_by_merges(store: Path) -> None:
    s = build_summary(store, NOW)
    assert set(s) == {"version", "written_at", "forge", "cost", "projects", "attempts", "merges"}
    assert set(s["attempts"]) == {ISSUE, MISSION}
    assert all(SpendSlot(**slot).scope == "attempt" for slot in s["attempts"].values())
    # The attempt figures are the ones the attempts rule gives on its own.
    conn = dbread.snapshot(store)
    try:
        factory = load_factory(conn, NOW)
        book = load_cost_book(conn, NOW)
    finally:
        conn.close()
    for corr, (mission, features) in linear_attempts(factory, NOW).items():
        assert s["attempts"][corr] == dataclasses.asdict(dbread.attempt_spend(book, features, mission))


def test_the_feed_copies_publication_records_and_target_branches(store: Path) -> None:
    conn = sqlite3.connect(store)
    try:
        rows = dict(conn.execute("SELECT build_id, result FROM forge_publication_records").fetchall())
        assert rows["bL"] == "merged into the remote and running"
        assert rows["bP"] == "published to the mission branch"
        assert conn.execute("SELECT target_branch FROM forge_builds WHERE build_id='bL'").fetchone() == (None,)
    finally:
        conn.close()


def test_mission_parts_match_on_a_boundary_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two missions whose ids are prefixes of one another: `m-abcd-K2-a1` starts with `m-abc` but
    not with `m-abc-`, so each part (and its calls) counts only for its own mission."""
    monkeypatch.setattr(forge_fake, "T0", T)
    fake = ForgeFake(tmp_path / "forge.db")
    spend = FakeSpend(T)
    for mid, corr, part, key, code, tokens in (("m-1", "m-abc", "m-abc-K1-a1", "K1", "FEAT-0101", 1_000),
                                                ("m-2", "m-abcd", "m-abcd-K2-a1", "K2", "FEAT-0102", 7_000)):
        fake.mission(mid, f"Mission {key}", corr, status="MERGED", closed=170)
        fake.planning(part, f"Part {key}.", state="FAILED", start=100, end=110, error="The planner gave up.")
        fake.build(f"b-{mid}", code, f"{corr}-merge", queued=160, started=160, completed=165, mission_id=mid,
                   mission_role="mission-merge")
        fake.merge_decision(f"b-{mid}", 165)
        fake.merged(f"b-{mid}", 166)
        spend.add(f"s-{key}", 105, part, prompt=tokens, completion=tokens // 10)
    path = tmp_path / "readmodel.db"
    conn = store_from(fake, path, NOW)
    import_once(conn, spend, NOW)
    conn.close()

    conn = dbread.snapshot(path)
    try:
        factory = load_factory(conn, NOW)
    finally:
        conn.close()
    parts = mission_parts(factory)
    assert [f.key for f in parts["m-1"]] == ["m-abc-K1-a1"]
    assert [f.key for f in parts["m-2"]] == ["m-abcd-K2-a1"]
    merges = build_summary(path, NOW)["merges"]
    abc, abcd = SpendSlot(**merges["b-m-1"]), SpendSlot(**merges["b-m-2"])
    assert abc.calls == 1 and abc.amount_usd == pytest.approx(_cost(1_000, 100))
    assert abcd.calls == 1 and abcd.amount_usd == pytest.approx(_cost(7_000, 700))


def test_a_part_belongs_to_the_mission_whose_id_leaves_a_key_and_attempt(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Coach: mission A `c` has part `c-X-a1`; mission B's id is `c-X`. The part starts with both
    `c-` and `c-X-`, but only A leaves Forge's `<KEY>-a<n>` (`X-a1`); B would leave `a1`. So it is
    A's part, not B's, though B's id is the longer prefix."""
    monkeypatch.setattr(forge_fake, "T0", T)
    fake = ForgeFake(tmp_path / "forge.db")
    fake.mission("m-a", "Mission A", "c", status="MERGED", closed=170)
    fake.mission("m-b", "Mission B", "c-X", status="MERGED", closed=170)
    fake.planning("c-X-a1", "A's part X.", state="FAILED", start=100, end=110, error="The planner gave up.")
    fake.planning("c-X-Y-a2", "B's part Y.", state="FAILED", start=100, end=110, error="The planner gave up.")
    fake.planning("c-x-a1", "Not a part: a key is upper case.", state="FAILED", start=100, end=110)
    fake.planning("c-X-a1-extra", "Not a part: more after the attempt.", state="FAILED", start=100, end=110)
    path = tmp_path / "readmodel.db"
    store_from(fake, path, NOW).close()
    conn = dbread.snapshot(path)
    try:
        parts = mission_parts(load_factory(conn, NOW))
    finally:
        conn.close()
    assert [f.key for f in parts["m-a"]] == ["c-X-a1"]
    assert [f.key for f in parts["m-b"]] == ["c-X-Y-a2"]
