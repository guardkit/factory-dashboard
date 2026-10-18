"""Part F, coach review fixes: a locked or corrupt suggestions.db, form size before reading, suggestion
ids, the voice note's own wording on every refusal, parallel posts, the per-person voice-note limit,
the client-safe export and the order of CHANGES.md."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from backend import app as app_module
from backend import suggestions, voice
from backend.app import MAX_FORM_BYTES, create_app
from backend.auth import ResolvedUser, make_credential_hash
from fastapi import FastAPI
from starlette.requests import ClientDisconnect, Request
from starlette.testclient import TestClient

from tests.conftest import LOOPBACK_URL, account_row, login, write_accounts
from tests.test_chat import FakeModel, _chat, _ledger
from tests.test_voice import WEBM, FakeSpeech, wav

ROOT = Path(__file__).resolve().parent.parent
TENANTS_TEST = Path(__file__).resolve().parent / "fixtures" / "tenants_test.yaml"
ORIGIN = {"Origin": LOOPBACK_URL}
JAMES = ResolvedUser("james", "James", "operator")
WORDS = "Please make the numbers on the Now page bigger."


@pytest.fixture
def data(tmp_path: Path) -> Path:
    folder = tmp_path / "data"
    folder.mkdir()
    return folder


@pytest.fixture(autouse=True)
def short_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A locked file is given up on quickly in these tests (3 s in production)."""
    monkeypatch.setattr(suggestions, "_BUSY_TIMEOUT_SECS", 0.2)


def _board(data: Path) -> TestClient:
    return TestClient(create_app(db_path=data / "readmodel.db", tenants_path=TENANTS_TEST), base_url=LOOPBACK_URL)


def _suggest(client: TestClient, text: str = WORDS) -> httpx.Response:
    r: httpx.Response = client.post("/suggest", data={"text": text, "from_page": "/"}, headers=ORIGIN,
                                    follow_redirects=False)
    return r


def _assert_unavailable(client: TestClient) -> None:
    started = time.monotonic()
    r = _suggest(client)
    assert r.status_code == 503 and suggestions.CANT_SAVE.replace("'", "&#39;") in r.text
    assert WORDS in r.text                                   # the words are back in the box
    r = client.get("/suggestions")
    assert r.status_code == 503 and suggestions.CANT_READ.replace("'", "&#39;") in r.text
    r = client.post("/suggestions/1/status", data={"status": "done"}, headers=ORIGIN)
    assert r.status_code == 503 and suggestions.CANT_SET.replace("'", "&#39;") in r.text
    assert time.monotonic() - started < 5
    assert client.get("/").status_code == 200 and client.get("/features").status_code == 200  # the rest works


# --- 1. a locked or corrupt database ------------------------------------------------------------------


def test_a_locked_database_gives_one_sentence_and_keeps_the_words(data: Path) -> None:
    with _board(data) as client:
        login(client)
        holder = sqlite3.connect(data / suggestions.DB_NAME, isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            _assert_unavailable(client)
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        assert _suggest(client).status_code == 303           # works again once the file is free
        assert client.get("/suggestions").status_code == 200


def test_a_database_corrupted_while_running_gives_one_sentence(data: Path) -> None:
    with _board(data) as client:
        login(client)
        (data / suggestions.DB_NAME).write_bytes(b"this is not a database at all " * 100)
        _assert_unavailable(client)


def test_a_corrupt_database_at_start_does_not_stop_the_board(data: Path, caplog: pytest.LogCaptureFixture) -> None:
    (data / suggestions.DB_NAME).write_bytes(b"this is not a database at all " * 100)
    with caplog.at_level(logging.WARNING), _board(data) as client:
        login(client)
        _assert_unavailable(client)
    assert any("suggestions.db cannot be opened" in rec.getMessage() for rec in caplog.records)


def test_a_locked_database_at_start_does_not_stop_the_board(data: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = data / suggestions.DB_NAME
    suggestions.init_db(path)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with caplog.at_level(logging.WARNING), _board(data) as client:
            login(client)
            _assert_unavailable(client)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert any("suggestions.db cannot be opened" in rec.getMessage() for rec in caplog.records)


def test_the_command_line_says_a_corrupt_file_cannot_be_read(tmp_path: Path) -> None:
    (tmp_path / suggestions.DB_NAME).write_bytes(b"this is not a database at all " * 100)
    out = subprocess.run([sys.executable, "-m", "backend.suggestions", "list", "--data", str(tmp_path)],
                         capture_output=True, text=True, timeout=60, check=False,
                         env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}, cwd=ROOT)
    assert out.returncode == 1 and suggestions.CANT_READ in out.stderr and "Traceback" not in out.stderr


def test_every_new_sentence_is_one_sentence() -> None:
    for sentence in (suggestions.CANT_SAVE, suggestions.CANT_READ, suggestions.CANT_SET, app_module.NO_FORM_LENGTH,
                     app_module.FORM_TOO_BIG):
        assert len(re.findall(r"[.!?](\s|$)", sentence)) == 1, sentence


# --- 2. form size before reading ----------------------------------------------------------------------


def _raw_post(app: FastAPI, path: str, headers: list[tuple[bytes, bytes]]) -> tuple[int, int, bytes]:
    """POST straight to the ASGI app with a spy on the body: (status, how many times the body was read,
    the response body). The spy hands over a small body if asked, so a missing guard shows as a read."""
    reads: list[int] = []

    async def receive() -> dict[str, object]:
        reads.append(1)
        return {"type": "http.request", "body": b"text=x", "more_body": False}

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"localhost:8090"), *headers], "client": ("127.0.0.1", 50000),
             "server": ("127.0.0.1", 8090)}
    asyncio.run(app(scope, receive, send))  # type: ignore[arg-type]
    status = next(int(str(m["status"])) for m in sent if m["type"] == "http.response.start")
    body = b"".join(cast(bytes, m.get("body", b"")) for m in sent if m["type"] == "http.response.body")
    return status, len(reads), body


SIZE_GUARDED = ["/suggest", "/suggestions/1/status", "/login", "/logout", "/chat"]


@pytest.mark.parametrize("path", SIZE_GUARDED)
@pytest.mark.parametrize("kind", [b"application/x-www-form-urlencoded", b"multipart/form-data; boundary=x"])
def test_a_signed_out_oversize_form_is_refused_before_reading(data: Path, path: str, kind: bytes) -> None:
    app = create_app(db_path=data / "readmodel.db", tenants_path=TENANTS_TEST)
    big = b"40000000" if kind.startswith(b"multipart") else b"30000000"
    status, reads, body = _raw_post(app, path, [(b"content-type", kind), (b"content-length", big)])
    assert (status, reads) == (413, 0) and body.decode() == app_module.FORM_TOO_BIG
    status, reads, body = _raw_post(app, path, [(b"content-type", kind), (b"transfer-encoding", b"chunked")])
    assert (status, reads) == (411, 0) and body.decode() == app_module.NO_FORM_LENGTH


@pytest.mark.parametrize("path", SIZE_GUARDED)
def test_a_transfer_encoding_is_refused_even_with_a_length(data: Path, path: str) -> None:
    """If the server ever reads chunked bodies itself (h11), a small Content-Length beside a
    Transfer-Encoding must not let a large chunked body through."""
    app = create_app(db_path=data / "readmodel.db", tenants_path=TENANTS_TEST)
    status, reads, body = _raw_post(app, path, [(b"content-type", b"application/x-www-form-urlencoded"),
                                                (b"content-length", b"6"), (b"transfer-encoding", b"chunked")])
    assert (status, reads) == (411, 0) and body.decode() == app_module.NO_FORM_LENGTH


def test_a_form_at_the_limit_goes_on_to_the_sign_in_check(data: Path) -> None:
    app = create_app(db_path=data / "readmodel.db", tenants_path=TENANTS_TEST)
    status, reads, _ = _raw_post(app, "/suggest", [(b"content-type", b"application/x-www-form-urlencoded"),
                                                   (b"content-length", str(MAX_FORM_BYTES).encode())])
    assert status == 401 and reads >= 1


def test_the_largest_real_suggestion_fits_the_limit(data: Path) -> None:
    """2,000 characters of non-Latin text, URL-encoded, is under the form limit and is stored."""
    with _board(data) as client:
        login(client)
        assert _suggest(client, "日本" * 1000).status_code == 303


# --- 3. suggestion ids --------------------------------------------------------------------------------


@pytest.mark.parametrize("ident", ["%C2%B2", "99999999999999999999999", "9999999999999999999", "-1", "1.0", "%D9%A3"])
def test_odd_suggestion_ids_are_refused_with_a_sentence(data: Path, ident: str) -> None:
    with _board(data) as client:
        login(client)
        r = client.post(f"/suggestions/{ident}/status", data={"status": "done"}, headers=ORIGIN)
        assert r.status_code == 400 and "nothing was changed" in r.text


# --- 4 and 5. the voice note: its own wording on every refusal, and a limit per person ------------------


@pytest.fixture
def heard(data: Path) -> Iterator[TestClient]:
    store = _ledger(data)
    app = create_app(db_path=store, tenants_path=TENANTS_TEST)
    speech = FakeSpeech()
    speech.stt = (200, {"text": "Shorter pages.", "duration": 2.0})
    app.state.chat = _chat(store, FakeModel())
    app.state.voice = voice.Voice(voice.VoiceSettings(), transport=httpx.MockTransport(speech.handler))
    app.state.note_limit = voice.PerUserHourly(limit=2)
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        login(client)
        yield client


def _upload(audio: bytes, content_type: str = "audio/webm") -> dict[str, tuple[str, bytes, str]]:
    return {"audio": ("note.webm", audio, content_type)}


REFUSALS: dict[str, tuple[Callable[[TestClient], httpx.Response], int, str]] = {
    "another site": (lambda c: c.post("/api/voice/transcribe", files=_upload(WEBM), headers={"Origin": "http://evil.example"}),
                     403, voice.NOTE_CROSS_SITE),
    "no length": (lambda c: c.post("/api/voice/transcribe", content=iter([b"--x\r\n", b"chunked"]),
                                   headers={**ORIGIN, "Content-Type": "multipart/form-data; boundary=x"}),
                  411, voice.NO_LENGTH),
    "declared too large": (lambda c: c.post("/api/voice/transcribe", content=b"x", headers={
        **ORIGIN, "Content-Type": "multipart/form-data; boundary=x", "Content-Length": str(voice.MAX_BODY_BYTES + 1)}),
        413, voice.NOTE_WORDS[voice.TOO_BIG]),
    "malformed form": (lambda c: c.post("/api/voice/transcribe", files=_upload(WEBM),
                                        data={"a": "1", "b": "2", "c": "3", "d": "4"}, headers=ORIGIN),
                       400, voice.NOTE_WORDS[voice.NO_RECORDING]),
    "no recording": (lambda c: c.post("/api/voice/transcribe", data={"text": "hello"}, headers=ORIGIN),
                     400, voice.NOTE_WORDS[voice.NO_RECORDING]),
    "empty recording": (lambda c: c.post("/api/voice/transcribe", files=_upload(b""), headers=ORIGIN),
                        400, voice.NOTE_WORDS[voice.NO_RECORDING]),
    "not audio": (lambda c: c.post("/api/voice/transcribe", files=_upload(b"plain text"), headers=ORIGIN),
                  415, voice.NOTE_WORDS[voice.NOT_AUDIO]),
    "too large a file": (lambda c: c.post("/api/voice/transcribe", files=_upload(WEBM + b"\0" * (1024 * 1024)),
                                          headers=ORIGIN), 413, voice.NOTE_WORDS[voice.TOO_BIG]),
    "too long": (lambda c: c.post("/api/voice/transcribe", files=_upload(wav(40), "audio/wav"), headers=ORIGIN),
                 413, voice.NOTE_WORDS[voice.TOO_LONG]),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_every_voice_note_refusal_speaks_of_the_suggestion(heard: TestClient, case: str) -> None:
    send, status, sentence = REFUSALS[case]
    r = send(heard)
    assert r.status_code == status and r.json() == {"message": sentence}
    assert "question" not in sentence


def test_a_caller_leaving_a_voice_note_mid_upload_gets_an_empty_400(heard: TestClient,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    async def gone(self: Request, **kw: object) -> None:
        raise ClientDisconnect()

    monkeypatch.setattr(Request, "form", gone)
    r = heard.post("/api/voice/transcribe", files=_upload(WEBM), headers=ORIGIN)
    assert r.status_code == 400 and r.content == b""


def test_the_spoken_question_keeps_its_own_wording(heard: TestClient) -> None:
    r = heard.post("/api/voice/ask", files=_upload(b"plain text"), headers=ORIGIN)
    assert r.status_code == 415 and r.json() == {"message": voice.NOT_AUDIO}
    r = heard.post("/api/voice/ask", files=_upload(WEBM), headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json() == {"message": voice.CROSS_SITE}


def test_the_voice_note_limit_is_per_person(heard: TestClient, config_dir: Path) -> None:
    write_accounts(config_dir, [account_row(), {"username": "rich", "display_name": "Rich",
                                                "password_hash": make_credential_hash("another-password")}])
    for _ in range(2):
        assert heard.post("/api/voice/transcribe", files=_upload(WEBM), headers=ORIGIN).status_code == 200
    r = heard.post("/api/voice/transcribe", files=_upload(WEBM), headers=ORIGIN)
    assert r.status_code == 429 and r.json()["message"] == voice.NOTES_LIMITED
    with TestClient(heard.app, base_url=LOOPBACK_URL) as other:
        login(other, "rich", "another-password")
        r = other.post("/api/voice/transcribe", files=_upload(WEBM), headers=ORIGIN)
        assert r.status_code == 200 and r.json() == {"transcript": "Shorter pages."}


# --- 5. parallel posts store exactly the limit ----------------------------------------------------------


def test_parallel_posts_store_exactly_the_limit_without_locking_errors(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(suggestions, "_BUSY_TIMEOUT_SECS", 3.0)  # the production wait
    path = tmp_path / suggestions.DB_NAME
    suggestions.init_db(path)
    threads, limit = 24, 10
    start = threading.Barrier(threads)
    results: list[tuple[int | None, str]] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def post(i: int) -> None:
        start.wait()
        try:
            got = suggestions.add(path, JAMES, f"idea {i}", "/", per_hour=limit)
        except BaseException as exc:
            with guard:
                errors.append(exc)
            return
        with guard:
            results.append(got)

    for _trial in range(3):
        with sqlite3.connect(path) as conn:
            conn.execute("DELETE FROM suggestions")
        results.clear()
        workers = [threading.Thread(target=post, args=(i,)) for i in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(30)
        assert errors == []
        assert sum(1 for ident, _ in results if ident is not None) == limit
        assert sum(1 for _, why in results if why == suggestions.LIMITED) == threads - limit
        assert len(suggestions.list_all(path)) == limit


# --- 6. the client-safe export and the order of CHANGES.md ----------------------------------------------


def test_the_client_safe_export_has_no_suggest_link(data: Path) -> None:
    with _board(data) as client:
        login(client)
        assert "suggest-link" in client.get("/reports").text
        export = client.get("/reports?view=export&tenant=clientco")
        assert export.status_code == 200 and "Client-safe report" in export.text
        assert "suggest-link" not in export.text and "/suggest?" not in export.text


def test_the_18_october_changes_are_newest_first() -> None:
    (day,) = [d for d in suggestions.read_changes() if d.date == "18 October 2026"]
    order = ["suggest a change", "about cost and time", "by voice", "what each build cost", "Appmilla look",
             "client account", "from the dashboard's own records", "live data", "signs in"]
    found = [next(i for i, item in enumerate(day.items) if key in item) for key in order]
    assert found == sorted(found) == list(range(len(order)))
