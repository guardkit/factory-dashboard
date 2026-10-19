"""Stopping a mission, as the dashboard shows it (missions design, section 11, "The dashboard").

- The feed copies `stop_requested_at` and `stop_requested_by`; a ledger from before stopping has
  neither, and its missions show as they did.
- While a stop drains (asked for, mission not closed) the mission shows as "Stopping" wherever its
  status shows: its page, its features' mission sentence, its plan card, its features' status.
- A closed mission's reason shows on its page and its features' mission sentence, in plain words.
- No approver identity (a chat member id) is ever shown: a person is named only by the display
  name the dashboard's accounts give them, else the sentence leaves them out.
- A client account sees no mission, and a client's report never shows another client's work.
"""

from __future__ import annotations

import html
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from backend.app import create_app
from backend.projector.forge_mirror import mirror_once
from backend.projector.store import iso, write_heartbeat
from backend.readmodel import dbread, missionwords
from backend.readmodel.factory import Mission, load_factory
from starlette.testclient import TestClient

from tests.conftest import (
    CLIENT_PASSWORD,
    CLIENT_USER,
    LOOPBACK_URL,
    TENANTS_TEST,
    account_row,
    client_account_row,
    login,
    write_accounts,
)
from tests.forge_fake import T0, ForgeFake, store_from

# Placeholders only: no real member id appears in this public repository.
APPROVER = "U0PLACEHOLDER1"
STRANGER = "U0PLACEHOLDER2"          # an identity no dashboard account has
STOP_AT = datetime(2026, 10, 16, 10, 5, 0, tzinfo=UTC)   # 11:05:00 in London (summer time)
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
IDS = re.compile(r"U0PLACEHOLDER\d")


def add_stop_columns(fake: ForgeFake) -> None:
    """Forge's ledger once stopping shipped: the two columns its migration adds."""
    fake.conn.execute("ALTER TABLE missions ADD COLUMN stop_requested_at TEXT")
    fake.conn.execute("ALTER TABLE missions ADD COLUMN stop_requested_by TEXT")


def request_stop(fake: ForgeFake, mid: str, by: str = APPROVER, at: datetime = STOP_AT) -> None:
    fake.conn.execute("UPDATE missions SET stop_requested_at=?, stop_requested_by=? WHERE id=?", (iso(at), by, mid))


def landed_part(fake: ForgeFake, mid: str, corr: str, code: str, build: str) -> None:
    """A mission's feature built and joined onto the mission's branch."""
    fake.planning(f"{corr}-A-a1", "Add a done flag.")
    fake.build(build, code, f"{corr}-A-a1", mission_id=mid, mission_role="feature")
    fake.merge_decision(build, 81)
    fake.merged(build, 82, mission_branch=True)


def _mission(conn: sqlite3.Connection, mid: str, now: datetime = NOW) -> Mission:
    return next(m for m in load_factory(conn, now).missions if m.id == mid)


# --- the copy: both ledger shapes -------------------------------------------------------------------------


def test_a_ledger_from_before_stopping_still_copies_and_shows_its_missions(tmp_path: Path) -> None:
    fake = ForgeFake(tmp_path / "forge.db")           # the fake's missions table has no stop columns
    fake.mission("m-1", "Notes", "mc1")
    conn = store_from(fake, tmp_path / "store.db", NOW)
    assert conn.execute("SELECT stop_requested_at, stop_requested_by FROM forge_missions").fetchone() == (None, None)
    status = conn.execute("SELECT last_error FROM mirror_status WHERE source='forge'").fetchone()
    assert status == (None,)
    m = _mission(conn, "m-1")
    assert m.stop_requested_at is None and not m.stopping
    assert missionwords.status_words(m) == "running"
    assert missionwords.detail_words(m, NOW) == ""


def test_a_ledger_with_the_stop_columns_copies_them(tmp_path: Path) -> None:
    fake = ForgeFake(tmp_path / "forge.db")
    add_stop_columns(fake)
    fake.mission("m-1", "Notes", "mc1")
    fake.mission("m-2", "Tags", "mc2")
    request_stop(fake, "m-1")
    conn = store_from(fake, tmp_path / "store.db", NOW)
    assert conn.execute("SELECT stop_requested_at, stop_requested_by FROM forge_missions WHERE id='m-1'"
                        ).fetchone() == (iso(STOP_AT), APPROVER)
    m1, m2 = _mission(conn, "m-1"), _mission(conn, "m-2")
    assert m1.stopping and m1.stop_requested_at == STOP_AT
    assert not m2.stopping
    assert missionwords.status_words(m1) == "stopping"
    assert missionwords.status_words(m2) == "running"


def test_the_stop_reaches_the_copy_on_a_later_pass(tmp_path: Path) -> None:
    fake = ForgeFake(tmp_path / "forge.db")
    add_stop_columns(fake)
    fake.mission("m-1", "Notes", "mc1")
    conn = store_from(fake, tmp_path / "store.db", NOW)
    assert not _mission(conn, "m-1").stopping
    request_stop(fake, "m-1")
    assert mirror_once(conn, fake.path, NOW) is True
    assert _mission(conn, "m-1").stopping


# --- the words ---------------------------------------------------------------------------------------------


def _m(status: str, *, stop: datetime | None = None, by: str = "", reason: str = "") -> Mission:
    return Mission(id="m-1", title="Notes", target_repo="acme/notes", status=status, correlation_id="mc1",
                   created_at=T0, closed_at=None, closed_reason=reason, merge_build=None,
                   stop_requested_at=stop, stop_requested_by=by)


@pytest.mark.parametrize("status", ["STARTING", "RUNNING", "READY"])
def test_a_mission_draining_a_stop_is_stopping(status: str) -> None:
    m = _m(status, stop=STOP_AT, by=APPROVER)
    assert m.stopping and missionwords.status_words(m) == "stopping"
    assert missionwords.stop_words(m, NOW, {APPROVER: "Rich"}) == "stop asked for by Rich at 11:05:00"
    assert missionwords.stop_words(m, NOW) == "stop asked for at 11:05:00"
    assert missionwords.sentence(m, NOW) == \
        "Part of mission m-1 (Notes): the mission is stopping (stop asked for at 11:05:00)."


@pytest.mark.parametrize("status", ["REJECTED", "NOT_STARTED", "MERGED", "NOT_MERGED"])
def test_a_closed_mission_is_never_stopping(status: str) -> None:
    m = _m(status, stop=STOP_AT, by=APPROVER)
    assert not m.stopping and missionwords.status_words(m) != "stopping"


STOP_REASON = f"stopped by {APPROVER} at {iso(STOP_AT)}; 2 landed on the branch, 1 did not"


@pytest.mark.parametrize(("reason", "names", "now", "words"), [
    # Forge's drained stop (lifecycle/missions.py close_if_drained)
    (STOP_REASON, {APPROVER: "Rich"}, NOW, "stopped by Rich at 11:05:00; 2 landed on the branch, 1 did not"),
    (STOP_REASON, None, NOW, "stopped at 11:05:00; 2 landed on the branch, 1 did not"),
    (STOP_REASON, {STRANGER: "Someone Else"}, LATER, "stopped at 16 Oct 11:05; 2 landed on the branch, 1 did not"),
    (f"stopped by someone at {iso(STOP_AT)}; 0 landed on the branch, 3 did not", {}, NOW,
     "stopped at 11:05:00; 0 landed on the branch, 3 did not"),
    # a PROPOSED mission stopped at once (request_stop)
    (f"stopped by {APPROVER} at {iso(STOP_AT)} before it started", None, NOW, "stopped at 11:05:00 before it started"),
    # a declined merge card (pipeline/mission_finish.py)
    (f"{APPROVER} declined the merge card", {APPROVER: "Rich"}, NOW, "Rich declined the merge card"),
    (f"{APPROVER} declined the merge card", None, NOW, "the merge card was declined"),
    ("the approver declined the merge card", None, NOW, "the approver declined the merge card"),
    # plain sentences pass through, without a closing full stop
    ("nothing landed", None, NOW, "nothing landed"),
    ("nobody answered the mission plan card in time.", None, NOW, "nobody answered the mission plan card in time"),
    # a bare code becomes words
    ("merged-deploy-failed", None, NOW, "merged deploy failed"),
    # any other approver identity is never shown as it is
    (f"the merge was refused by {STRANGER}", None, NOW, "the merge was refused by someone"),
    ("asked by dashboard:rich to wait", {"dashboard:rich": "Rich"}, NOW, "asked by Rich to wait"),
    ("", None, NOW, ""),
])
def test_closed_reasons_read_as_plain_words(reason: str, names: dict[str, str] | None, now: datetime,
                                            words: str) -> None:
    assert missionwords.reason_words(reason, now, names) == words


def test_the_mission_sentence_carries_a_closed_reason() -> None:
    m = _m("NOT_MERGED", stop=STOP_AT, by=APPROVER, reason=STOP_REASON)
    assert missionwords.sentence(m, NOW) == ("Part of mission m-1 (Notes): the mission is not merged "
                                             "(stopped at 11:05:00; 2 landed on the branch, 1 did not).")
    assert missionwords.sentence(_m("RUNNING"), NOW) == "Part of mission m-1 (Notes): the mission is running."


# --- the pages -------------------------------------------------------------------------------------------


@dataclass
class World:
    client: TestClient
    fake: ForgeFake
    feed: sqlite3.Connection
    store: Path

    def refresh(self) -> None:
        now = datetime.now(UTC)
        write_heartbeat(self.feed, now)
        mirror_once(self.feed, self.fake.path, now)

    def page(self, path: str) -> str:
        r = self.client.get(path)
        assert r.status_code == 200, (path, r.status_code)
        return html.unescape(r.text)


@pytest.fixture
def world(tmp_path: Path, config_dir: Path) -> Iterator[World]:
    write_accounts(config_dir, [{**account_row(), "approver_identity": APPROVER}, client_account_row()])
    fake = ForgeFake(tmp_path / "forge.db")
    add_stop_columns(fake)
    store = tmp_path / "readmodel.db"
    feed = store_from(fake, store)
    app = create_app(db_path=store, tenants_path=TENANTS_TEST)
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        assert login(client).status_code == 200
        yield World(client, fake, feed, store)
    feed.close()


def _lead(body: str) -> str:
    found = re.search(r'<p class="page-lead">(.*?)</p>', body, re.S)
    assert found is not None
    return " ".join(found.group(1).split())


def _stop_time() -> str:
    """The stop's time as the pages show it, read now (a test may run on another day)."""
    from backend.readmodel import viewmodels as vm  # noqa: PLC0415

    return vm.fmt_wall(STOP_AT, now=datetime.now(UTC))


def test_a_stopping_mission_shows_stopping_on_its_page_and_its_features(world: World) -> None:
    world.fake.mission("m1", "Notes", "mc1")
    landed_part(world.fake, "m1", "mc1", "FEAT-000A", "bA")
    request_stop(world.fake, "m1")
    world.refresh()
    mission = world.page("/missions/m1")
    assert _lead(mission) == f"Stopping: stop asked for by Test Person at {_stop_time()}"
    feature = world.page("/features/FEAT-000A")
    assert (f"Part of mission m1 (Notes): the mission is stopping (stop asked for by Test Person at "
            f"{_stop_time()}).") in feature
    assert "On mission m1's branch; the mission is stopping, so it will not be merged" in feature
    for body in (mission, feature):
        assert not IDS.search(body)


def test_a_stop_by_someone_without_an_account_names_no_one(world: World) -> None:
    world.fake.mission("m1", "Notes", "mc1")
    request_stop(world.fake, "m1", by=STRANGER)
    world.refresh()
    body = world.page("/missions/m1")
    assert _lead(body) == f"Stopping: stop asked for at {_stop_time()}"
    assert not IDS.search(body)


def test_a_mission_stopped_and_closed_shows_why_in_plain_words(world: World) -> None:
    world.fake.mission("m1", "Notes", "mc1", status="NOT_MERGED", closed=120)
    landed_part(world.fake, "m1", "mc1", "FEAT-000A", "bA")
    request_stop(world.fake, "m1", by=STRANGER)
    world.fake.conn.execute("UPDATE missions SET closed_reason=? WHERE id='m1'",
                            (f"stopped by {STRANGER} at {iso(STOP_AT)}; 1 landed on the branch, 1 did not",))
    world.refresh()
    mission = world.page("/missions/m1")
    assert _lead(mission) == f"Not merged: stopped at {_stop_time()}; 1 landed on the branch, 1 did not"
    feature = world.page("/features/FEAT-000A")
    assert ("Part of mission m1 (Notes): the mission is not merged "
            f"(stopped at {_stop_time()}; 1 landed on the branch, 1 did not).") in feature
    assert "the mission was not merged" in feature            # the feature's own status, as before
    for body in (mission, feature):
        assert not IDS.search(body) and "Stopping" not in body


def test_a_proposed_mission_stopped_at_once_says_so_on_its_plan_card(world: World) -> None:
    rid = "mission-m1:mission-plan:0"
    world.fake.mission("m1", "Notes", "mc1", status="REJECTED", closed=30)
    request_stop(world.fake, "m1")
    world.fake.conn.execute("UPDATE missions SET plan_request_id=?, closed_reason=? WHERE id='m1'",
                            (rid, f"stopped by {APPROVER} at {iso(STOP_AT)} before it started"))
    world.refresh()
    body = world.page("/missions/m1")
    assert _lead(body) == f"Rejected: stopped by Test Person at {_stop_time()} before it started"
    assert f"Not started: stopped by Test Person at {_stop_time()} before it started." in body
    assert not IDS.search(body)


def test_a_closed_plan_card_on_a_stopping_mission_says_stopping(world: World) -> None:
    world.fake.mission("m1", "Notes", "mc1")
    world.fake.conn.execute("UPDATE missions SET plan_request_id='mission-m1:mission-plan:0', approved_at=NULL "
                            "WHERE id='m1'")
    request_stop(world.fake, "m1")
    world.refresh()
    body = world.page("/missions/m1")
    assert "The card closed; the mission is stopping." in body


def test_a_mission_closed_for_another_reason_shows_that_reason(world: World) -> None:
    world.fake.mission("m1", "Notes", "mc1", status="NOT_MERGED", closed=120)
    world.fake.conn.execute("UPDATE missions SET closed_reason='nothing landed' WHERE id='m1'")
    world.refresh()
    assert _lead(world.page("/missions/m1")) == "Not merged: nothing landed"


def test_a_ledger_without_the_columns_shows_the_page_as_before(tmp_path: Path, config_dir: Path) -> None:
    fake = ForgeFake(tmp_path / "forge.db")                # no stop columns
    fake.mission("m1", "Notes", "mc1")
    store = tmp_path / "readmodel.db"
    feed = store_from(fake, store)
    app = create_app(db_path=store, tenants_path=TENANTS_TEST)
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        login(client)
        body = html.unescape(client.get("/missions/m1").text)
    feed.close()
    assert _lead(body) == "Running"


# --- client scope ----------------------------------------------------------------------------------------


def test_a_client_sees_no_mission_and_no_other_clients_work(world: World) -> None:
    # One mission on the client's own project, one on another project; both stopping.
    world.fake.mission("m1", "Client notes", "mc1")
    world.fake.mission("m2", "Secret other work", "mc2")
    landed_part(world.fake, "m1", "mc1", "FEAT-000A", "bA")
    landed_part(world.fake, "m2", "mc2", "FEAT-000B", "bB")
    world.fake.conn.execute("UPDATE missions SET target_repo='clientco-app' WHERE id='m1'")
    world.fake.conn.execute("UPDATE builds SET repo='clientco-app' WHERE build_id='bA'")
    world.fake.conn.execute("UPDATE planning_runs SET target_repo='clientco-app' WHERE correlation_id='mc1-A-a1'")
    world.fake.conn.execute("UPDATE planning_runs SET request_text='Secret other work.' "
                            "WHERE correlation_id='mc2-A-a1'")
    request_stop(world.fake, "m1")
    request_stop(world.fake, "m2")
    world.refresh()
    # The client account: no mission page, no feature page, nothing about either mission at home.
    with TestClient(world.client.app, base_url=LOOPBACK_URL) as client:
        login(client, CLIENT_USER, CLIENT_PASSWORD)
        for path in ("/missions/m1", "/missions/m2", "/features/FEAT-000A", "/features/FEAT-000B"):
            assert client.get(path, follow_redirects=False).status_code == 303, path
        home = html.unescape(client.get("/").text)
        for words in ("Client notes", "Secret other work", "m1", "m2", "Stopping", "stopping"):
            assert words not in home, words
        assert not IDS.search(home)
    # The client's report (read by the operator) holds only its own feature, says it is stopping,
    # and names no identity; its client-safe export holds no work in progress and no cost.
    conn = sqlite3.connect(world.store)
    try:
        now = datetime.now(UTC)
        window = ((now - timedelta(days=30)).date().isoformat(), now.date().isoformat())
        report = dbread.weekly_report(conn, "clientco", now, window=window)
        rows = report.in_flight_rows
        assert [r.project for r in rows] == ["clientco-app"]
        assert "the mission is stopping" in rows[0].status_text
        assert not any("Secret other work" in r.title for r in rows)
        assert not IDS.search(repr(report))
        export = dbread.export_report(conn, "clientco", now, window=window)
        assert "Secret other work" not in repr(export) and "stopping" not in repr(export)
        assert not IDS.search(repr(export))
        assert "SpendSlot" not in repr(export) and "TimeSlot" not in repr(export)   # no cost, no time
    finally:
        conn.close()
