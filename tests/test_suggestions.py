"""Part F (design requirement 11): Suggest a change, the Suggestions page, What changed, the voice note,
and the read-only command line. suggestions.db is the only file the board writes while it runs."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from backend import suggestions, voice
from backend.app import create_app
from backend.auth import ResolvedUser, make_credential_hash
from starlette.testclient import TestClient

from tests.conftest import LOOPBACK_URL, TEST_DISPLAY_NAME, TEST_USER, account_row, login, write_accounts
from tests.test_chat import FakeModel, _chat, _ledger
from tests.test_voice import WEBM, FakeSpeech, wav

ROOT = Path(__file__).resolve().parent.parent
TENANTS_TEST = Path(__file__).resolve().parent / "fixtures" / "tenants_test.yaml"
ORIGIN = {"Origin": LOOPBACK_URL}
EVIL = {"Origin": "http://evil.example"}
JAMES = ResolvedUser("james", "James", "operator")


@pytest.fixture
def data(tmp_path: Path) -> Path:
    folder = tmp_path / "data"
    folder.mkdir()
    return folder


@pytest.fixture
def board(data: Path) -> Iterator[tuple[TestClient, Path]]:
    app = create_app(db_path=data / "readmodel.db", tenants_path=TENANTS_TEST)
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        login(client)
        yield client, data / suggestions.DB_NAME


def _suggest(client: TestClient, text: str, from_page: str = "/features", headers: dict[str, str] | None = None,
             ) -> httpx.Response:
    r: httpx.Response = client.post("/suggest", data={"text": text, "from_page": from_page},
                                    headers=ORIGIN if headers is None else headers, follow_redirects=False)
    return r


# --- storage --------------------------------------------------------------------------------------------


def test_a_suggestion_is_stored_with_who_when_page_and_text(board: tuple[TestClient, Path]) -> None:
    client, path = board
    r = _suggest(client, "Show the cost on the Now page,\nplease.", "/features?days=30")
    assert r.status_code == 303 and r.headers["location"] == "/suggestions?sent=1"
    (s,) = suggestions.list_all(path)
    assert (s.username, s.display_name, s.from_page, s.status) == (TEST_USER, TEST_DISPLAY_NAME, "/features?days=30",
                                                                   "new")
    assert s.text == "Show the cost on the Now page,\nplease."
    assert abs(datetime.fromisoformat(s.created_at) - datetime.now(UTC)) < timedelta(minutes=1)
    page = client.get("/suggestions?sent=1").text
    assert "Thank you. Your suggestion is saved." in page
    assert "Show the cost on the Now page,\nplease." in page and TEST_DISPLAY_NAME in page
    assert 'href="/features?days=30"' in page
    assert "<title>Suggestions · Appmilla Factory</title>" in page


def test_the_database_is_created_at_start_beside_the_read_model(board: tuple[TestClient, Path]) -> None:
    _, path = board
    assert path.exists() and path.parent.name == "data"
    assert suggestions.list_all(path) == []


def test_every_operator_page_links_to_suggest_a_change_with_its_address(board: tuple[TestClient, Path]) -> None:
    client, _ = board
    for path, encoded in (("/", "/"), ("/features?days=30", "/features%3Fdays%3D30"), ("/issues", "/issues"),
                          ("/suggestions", "/suggestions"), ("/changes", "/changes")):
        body = client.get(path).text
        assert body.count('class="suggest-link') == 1, path
        assert f'<a href="/suggest?from={encoded}">Suggest a change</a>' in body, path
        assert '<a href="/changes">What changed</a>' in body, path
    # The chat page and the suggestion form itself do not repeat it.
    assert "suggest-link" not in client.get("/chat").text
    form = client.get("/suggest?from=/issues").text
    assert "suggest-link" not in form
    assert '<input type="hidden" name="from_page" value="/issues">' in form
    assert "<title>Suggest a change · Appmilla Factory</title>" in form


def test_the_suggest_page_offers_a_microphone_once(board: tuple[TestClient, Path]) -> None:
    """mic.js is loaded once (by the chat drawer); the form's box is marked for the voice note."""
    client, _ = board
    body = client.get("/suggest").text
    assert body.count('src="/static/mic.js"') == 1
    assert "data-voice-fill" in body and body.count('class="chat-mic"') == 2  # the drawer's and the form's


# --- guards ---------------------------------------------------------------------------------------------


def test_signed_out_requests_are_refused(data: Path) -> None:
    app = create_app(db_path=data / "readmodel.db", tenants_path=TENANTS_TEST)
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        for path in ("/suggest", "/suggestions", "/changes"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/login", path
        assert _suggest(client, "hello").status_code == 401
        assert client.post("/suggestions/1/status", data={"status": "done"}, headers=ORIGIN).status_code == 401
        assert client.post("/api/voice/transcribe", files={"audio": ("n.webm", WEBM, "audio/webm")},
                           headers=ORIGIN).status_code == 401
    assert suggestions.list_all(data / suggestions.DB_NAME) == []


def test_a_client_account_is_refused(clientco_client: TestClient, app) -> None:  # type: ignore[no-untyped-def]
    for path in ("/suggest", "/suggestions", "/changes"):
        r = clientco_client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/", path
    assert _suggest(clientco_client, "let me in").status_code == 403
    assert clientco_client.post("/suggestions/1/status", data={"status": "done"}, headers=ORIGIN).status_code == 403
    assert clientco_client.post("/api/voice/transcribe", files={"audio": ("n.webm", WEBM, "audio/webm")},
                                headers=ORIGIN).status_code == 403
    assert suggestions.list_all(app.state.suggestions_path) == []
    assert "suggest-link" not in clientco_client.get("/").text  # the client layout offers nothing


def test_another_sites_posts_are_refused(board: tuple[TestClient, Path]) -> None:
    client, path = board
    r = _suggest(client, "from elsewhere", headers=EVIL)
    assert r.status_code == 403 and r.text == suggestions.CROSS_SITE
    assert _suggest(client, "null origin", headers={"Origin": "null"}).status_code == 403
    assert suggestions.list_all(path) == []
    assert _suggest(client, "mine").status_code == 303
    (s,) = suggestions.list_all(path)
    r = client.post(f"/suggestions/{s.id}/status", data={"status": "declined"}, headers=EVIL, follow_redirects=False)
    assert r.status_code == 403
    assert suggestions.list_all(path)[0].status == "new"
    r = client.post("/api/voice/transcribe", files={"audio": ("n.webm", WEBM, "audio/webm")}, headers=EVIL)
    assert r.status_code == 403 and r.json()["message"] == voice.NOTE_CROSS_SITE


# --- limits ---------------------------------------------------------------------------------------------


def test_length_and_empty_limits(board: tuple[TestClient, Path]) -> None:
    client, path = board
    long = "x" * (suggestions.MAX_CHARS + 1)
    r = _suggest(client, long)
    assert r.status_code == 400 and suggestions.TOO_LONG in r.text
    assert long in r.text  # the words come back in the box, so nothing typed is lost
    assert _suggest(client, "   \n\t ").status_code == 400
    assert _suggest(client, "x" * suggestions.MAX_CHARS).status_code == 303
    assert len(suggestions.list_all(path)) == 1


def test_the_hourly_limit_is_per_person_and_rolls_on(tmp_path: Path) -> None:
    path = tmp_path / suggestions.DB_NAME
    suggestions.init_db(path)
    t0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    for i in range(suggestions.PER_HOUR):
        assert suggestions.add(path, JAMES, f"idea {i}", "/", now=t0 + timedelta(minutes=i))[0] is not None
    assert suggestions.add(path, JAMES, "one more", "/", now=t0 + timedelta(minutes=30)) == (None, suggestions.LIMITED)
    rich = ResolvedUser("rich", "Rich", "operator")
    assert suggestions.add(path, rich, "mine", "/", now=t0 + timedelta(minutes=30))[0] is not None
    # An hour after the first, there is room for one again.
    assert suggestions.add(path, JAMES, "later", "/", now=t0 + timedelta(minutes=60, seconds=1))[0] is not None
    assert len(suggestions.list_all(path)) == suggestions.PER_HOUR + 2


def test_the_limit_over_http_says_so_in_one_sentence(board: tuple[TestClient, Path]) -> None:
    client, path = board
    for i in range(suggestions.PER_HOUR):
        assert _suggest(client, f"idea {i}").status_code == 303
    r = _suggest(client, "one too many")
    assert r.status_code == 429 and suggestions.LIMITED in r.text
    assert len(suggestions.list_all(path)) == suggestions.PER_HOUR
    for sentence in (suggestions.EMPTY, suggestions.TOO_LONG, suggestions.LIMITED, suggestions.CROSS_SITE,
                     suggestions.UNKNOWN):
        assert len(re.findall(r"[.!?](\s|$)", sentence)) == 1, sentence


# --- escaping -------------------------------------------------------------------------------------------


def test_hostile_text_and_page_render_inert(board: tuple[TestClient, Path]) -> None:
    client, path = board
    hostile = '<script>alert(1)</script> "><img src=x onerror=alert(2)> [x](javascript:alert(3))'
    assert _suggest(client, hostile, from_page="javascript:alert(4)").status_code == 303
    assert _suggest(client, "two", from_page="//evil.example/").status_code == 303
    assert _suggest(client, "three", from_page="/features\" onmouseover=\"alert(5)").status_code == 303
    body = client.get("/suggestions").text
    assert "<script>alert(1)" not in body and "<img src=x" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "javascript:alert(4)" not in body and "//evil.example" not in body and "onmouseover" not in body
    assert [s.from_page for s in suggestions.list_all(path)] == ["", "", ""]
    assert body.count("page not known") == 3


def test_control_characters_are_removed_before_storing(tmp_path: Path) -> None:
    path = tmp_path / suggestions.DB_NAME
    suggestions.init_db(path)
    suggestions.add(path, JAMES, "red \x1b[31mtext\x1b[0m\r\nline two\u202e\x07\tend", "/")
    (s,) = suggestions.list_all(path)
    assert s.text == "red [31mtext[0m\nline two end"
    assert "\x1b" not in s.text and "\u202e" not in s.text


# --- status changes -------------------------------------------------------------------------------------


def test_an_operator_sets_the_status(board: tuple[TestClient, Path]) -> None:
    client, path = board
    _suggest(client, "first")
    (s,) = suggestions.list_all(path)
    for status in ("seen", "done", "declined", "new"):
        r = client.post(f"/suggestions/{s.id}/status", data={"status": status}, headers=ORIGIN,
                        follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/suggestions#suggestion-{s.id}"
        got = suggestions.list_all(path)[0]
        assert (got.status, got.status_changed_by) == (status, TEST_USER)
    client.post(f"/suggestions/{s.id}/status", data={"status": "done"}, headers=ORIGIN)
    body = client.get("/suggestions").text
    assert 'status-pill status-pill--done' in body and '<option value="done" selected>' in body


@pytest.mark.parametrize(("ident", "status"), [("1", "deleted"), ("99", "done"), ("abc", "done"), ("1", "")])
def test_an_unknown_suggestion_or_status_changes_nothing(board: tuple[TestClient, Path], ident: str,
                                                         status: str) -> None:
    client, path = board
    _suggest(client, "first")
    r = client.post(f"/suggestions/{ident}/status", data={"status": status}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 400 and suggestions.UNKNOWN in r.text
    assert suggestions.list_all(path)[0].status == "new"


def test_the_database_wait_is_three_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped wait for a busy file (the robustness tests shorten it); every connection uses it."""
    assert suggestions._BUSY_TIMEOUT_SECS == 3.0
    seen: list[object] = []
    real = sqlite3.connect

    def spy(*args: Any, **kw: Any) -> sqlite3.Connection:
        seen.append(kw.get("timeout"))
        conn: sqlite3.Connection = real(*args, **kw)
        return conn

    monkeypatch.setattr(sqlite3, "connect", spy)
    path = tmp_path / suggestions.DB_NAME
    suggestions.init_db(path)
    suggestions.add(path, JAMES, "x", "/")
    suggestions.set_status(path, 1, "done", JAMES)
    suggestions.list_all(path)
    assert seen == [3.0, 3.0, 3.0, 3.0]


def test_the_database_refuses_an_unknown_status_itself(tmp_path: Path) -> None:
    path = tmp_path / suggestions.DB_NAME
    suggestions.init_db(path)
    suggestions.add(path, JAMES, "x", "/")
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE suggestions SET status = 'deleted'")
    conn.close()


# --- What changed (CHANGES.md) --------------------------------------------------------------------------


HOSTILE_CHANGES = """# What changed

<script>alert('intro')</script>

## 19 October 2026 <b>bold</b>

- A line with <script>alert(1)</script> in it.
- A [link](javascript:alert(2)) and <img src=x onerror=alert(3)>.
<iframe src="https://evil.example"></iframe>
  - an indented line is not an item

## 18 October 2026

- The second day.
- Another.
"""


def test_changes_are_read_as_plain_dated_lines() -> None:
    days = suggestions.parse_changes(HOSTILE_CHANGES)
    assert [d.date for d in days] == ["19 October 2026 <b>bold</b>", "18 October 2026"]
    assert days[0].items == ("A line with <script>alert(1)</script> in it.",
                             "A [link](javascript:alert(2)) and <img src=x onerror=alert(3)>.")
    assert suggestions.recent_changes(days, 3) == [
        ("19 October 2026 <b>bold</b>", days[0].items[0]), ("19 October 2026 <b>bold</b>", days[0].items[1]),
        ("18 October 2026", "The second day.")]


def test_the_changes_pages_never_pass_html_through(board: tuple[TestClient, Path], tmp_path: Path) -> None:
    client, _ = board
    changes = tmp_path / "CHANGES.md"
    changes.write_text(HOSTILE_CHANGES, encoding="utf-8")
    client.app.state.changes_path = changes  # type: ignore[attr-defined]
    for path in ("/changes", "/suggest", "/suggestions"):
        body = client.get(path).text
        for raw in ("<script>alert", "<img src=x", "<iframe", "<b>bold</b>", 'href="javascript'):
            assert raw not in body, (path, raw)
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body, path
    body = client.get("/changes").text
    assert "an indented line" not in body and "alert(&#39;intro&#39;)" not in body
    assert "<title>What changed · Appmilla Factory</title>" in body
    # The short note shows the newest three lines only.
    note = client.get("/suggest").text
    assert "The second day." in note and "Another." not in note


def test_the_repositorys_changes_list_is_plain_and_newest_first() -> None:
    days = suggestions.read_changes()
    assert days, "CHANGES.md has no dated lines"
    dates = [datetime.strptime(d.date, "%d %B %Y") for d in days]
    assert dates == sorted(dates, reverse=True)
    for d in days:
        for item in d.items:
            assert not re.search(r"[<>`*\[\]]", item), item  # plain words, no markup to render
            assert len(item) <= 160, item


def test_a_missing_changes_file_says_so(board: tuple[TestClient, Path], tmp_path: Path) -> None:
    client, _ = board
    client.app.state.changes_path = tmp_path / "nothing.md"  # type: ignore[attr-defined]
    assert "No changes are listed yet." in client.get("/changes").text
    assert "No changes are listed yet." in client.get("/suggest").text


# --- the voice note -------------------------------------------------------------------------------------


@pytest.fixture
def heard(data: Path) -> Iterator[tuple[TestClient, FakeModel, FakeSpeech]]:
    store = _ledger(data)
    app = create_app(db_path=store, tenants_path=TENANTS_TEST)
    model, speech = FakeModel(), FakeSpeech()
    speech.stt = (200, {"text": "  Please make   the Features page shorter. ", "duration": 4.0})
    app.state.chat = _chat(store, model)
    app.state.voice = voice.Voice(voice.VoiceSettings(), transport=httpx.MockTransport(speech.handler))
    app.state.note_limit = voice.PerUserHourly(limit=2)
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        login(client)
        yield client, model, speech


def _note(client: TestClient, audio: bytes = WEBM, content_type: str = "audio/webm;codecs=opus") -> httpx.Response:
    r: httpx.Response = client.post("/api/voice/transcribe", files={"audio": ("note.webm", audio, content_type)},
                                    headers=ORIGIN)
    return r


def test_a_voice_note_comes_back_as_text_only(heard: tuple[TestClient, FakeModel, FakeSpeech]) -> None:
    client, model, speech = heard
    r = _note(client)
    assert r.status_code == 200 and r.json() == {"transcript": "Please make the Features page shorter."}
    assert speech.paths() == ["/v1/audio/transcriptions"]  # speech to text only: nothing is spoken back
    assert model.calls == []                                # and the chat is not asked
    assert suggestions.list_all(client.app.state.suggestions_path) == []  # type: ignore[attr-defined]


def test_voice_notes_have_their_own_hourly_limit(heard: tuple[TestClient, FakeModel, FakeSpeech]) -> None:
    client, _, speech = heard
    assert _note(client).status_code == 200 and _note(client).status_code == 200
    r = _note(client)
    assert r.status_code == 429 and r.json()["message"] == voice.NOTES_LIMITED
    assert len(speech.requests) == 2


def test_voice_note_refusals_speak_of_the_suggestion(heard: tuple[TestClient, FakeModel, FakeSpeech]) -> None:
    client, _, speech = heard
    client.app.state.note_limit = voice.PerUserHourly()  # type: ignore[attr-defined]  # room for every case
    r = _note(client, wav(40), "audio/wav")
    assert r.status_code == 413 and r.json()["message"] == voice.NOTE_WORDS[voice.TOO_LONG]
    r = _note(client, b"not audio at all", "audio/webm")
    assert r.status_code == 415 and "suggestion" in r.json()["message"]
    speech.stt = (200, {"text": "  ", "duration": 2.0})
    r = _note(client)
    assert r.status_code == 422 and r.json()["message"] == voice.NOTE_HEARD_NOTHING
    speech.stt = httpx.ConnectError("down")
    r = _note(client)
    assert r.status_code == 503 and r.json()["message"] == voice.NOTE_CANT_HEAR
    speech.stt = (200, {"text": "Zebra crossing", "duration": 45.0})
    r = _note(client)
    assert r.status_code == 413 and "Zebra" not in r.text  # the transcript is dropped unread


def test_a_voice_note_waits_for_no_one(heard: tuple[TestClient, FakeModel, FakeSpeech]) -> None:
    client, _, speech = heard
    v: voice.Voice = client.app.state.voice  # type: ignore[attr-defined]
    assert v.one_at_a_time.acquire(blocking=False)
    try:
        r = _note(client)
        assert r.status_code == 429 and r.json()["message"] == voice.NOTE_BUSY
    finally:
        v.one_at_a_time.release()
    assert speech.requests == []


# --- the only file the board writes ---------------------------------------------------------------------


def _snapshot(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir()) if p.is_file()}


def test_suggestions_db_is_the_only_file_the_board_writes(heard: tuple[TestClient, FakeModel, FakeSpeech],
                                                         data: Path) -> None:
    """After start-up, every page, every fragment, the chat, a voice note, a suggestion and a status change
    leave every file in the data folder as it was, except suggestions.db."""
    client, _, _ = heard
    before = _snapshot(data)
    assert "suggestions.db" in before
    for path in ("/", "/features", "/features?days=all", "/projects", "/delivered", "/reports",
                 "/reports?view=export", "/issues", "/fleet", "/chat", "/suggest", "/suggestions", "/changes",
                 "/fragments/freshness", "/fragments/p2", "/events?once=1", "/healthz"):
        assert client.get(path).status_code == 200, path
    client.post("/chat", data={"question": "What was delivered this week?"}, headers=ORIGIN)
    _note(client)
    assert _suggest(client, "a change").status_code == 303
    (s,) = suggestions.list_all(data / suggestions.DB_NAME)
    client.post(f"/suggestions/{s.id}/status", data={"status": "seen"}, headers=ORIGIN)
    after = _snapshot(data)
    assert set(after) == set(before), set(after) ^ set(before)
    changed = {name for name in after if after[name] != before[name]}
    assert changed == {"suggestions.db"}


def test_the_feed_never_opens_suggestions() -> None:
    for src in (ROOT / "backend" / "projector").rglob("*.py"):
        assert "suggestion" not in src.read_text(encoding="utf-8"), src


# --- the command line -----------------------------------------------------------------------------------


def _cli(*args: str, env_db: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}
    if env_db:
        env["FACTORY_DASH_DB"] = env_db
    return subprocess.run([sys.executable, "-m", "backend.suggestions", *args], capture_output=True, text=True,
                          timeout=60, check=False, env=env, cwd=ROOT)


def test_the_command_line_lists_read_only(tmp_path: Path) -> None:
    path = tmp_path / suggestions.DB_NAME
    suggestions.init_db(path)
    first, _ = suggestions.add(path, JAMES, "Bigger numbers\non the Now page \x1b[2J", "/")
    second, _ = suggestions.add(path, JAMES, "A darker dark mode", "/fleet")
    assert first and second
    suggestions.set_status(path, first, "done", JAMES)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    out = _cli("list", "--data", str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert out.stdout.index(f"#{second}  New") < out.stdout.index(f"#{first}  Done")  # newest first
    assert "James (james)  from /fleet" in out.stdout and "    on the Now page [2J" in out.stdout
    assert "\x1b" not in out.stdout

    out = _cli("list", "--status", "new", "--json", env_db=str(tmp_path / "readmodel.db"))
    rows = json.loads(out.stdout)
    assert [(r["id"], r["text"]) for r in rows] == [(second, "A darker dark mode")]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest  # nothing written


def test_the_command_line_never_creates_the_database(tmp_path: Path) -> None:
    out = _cli("list", "--data", str(tmp_path))
    assert out.returncode == 0 and "No suggestions yet" in out.stderr
    assert not (tmp_path / suggestions.DB_NAME).exists()


def test_a_second_operator_sees_and_sets_the_same_list(board: tuple[TestClient, Path], config_dir: Path) -> None:
    client, path = board
    _suggest(client, "from the tester")
    write_accounts(config_dir, [account_row(), {"username": "rich", "display_name": "Rich",
                                                "password_hash": make_credential_hash("another-password")}])
    client.post("/logout", headers=ORIGIN)
    login(client, "rich", "another-password")
    assert "from the tester" in client.get("/suggestions").text
    (s,) = suggestions.list_all(path)
    client.post(f"/suggestions/{s.id}/status", data={"status": "seen"}, headers=ORIGIN)
    assert suggestions.list_all(path)[0].status_changed_by == "rich"
