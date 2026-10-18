"""Voice (design 2026-10-17, requirement 10): POST /api/voice/ask with stand-in speech models.

The chat is the real one (the fixture ledger and a stubbed chat model, as in tests/test_chat.py);
the speech-to-text and text-to-speech models are an httpx MockTransport.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from backend import voice
from backend.app import create_app
from backend.chat import service
from backend.chat.service import LIMITED, NOT_SET_UP, HourlyLimit
from starlette.requests import ClientDisconnect, Request
from starlette.testclient import TestClient

from tests.conftest import LOOPBACK_URL, login
from tests.test_chat import TENANTS_TEST, FakeModel, _chat, _ledger

ORIGIN = {"Origin": LOOPBACK_URL}
FAKE_WAV = b"RIFF" + b"\x00" * 40 + b"spoken answer"
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 200


def wav(seconds: float, rate: int = 8000) -> bytes:
    """A mono 16-bit WAV of `seconds` of silence."""
    byte_rate = rate * 2
    data_size = int(seconds * byte_rate)
    fmt = (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + rate.to_bytes(4, "little") \
        + byte_rate.to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    body = b"WAVE" + b"fmt " + (16).to_bytes(4, "little") + fmt + b"data" + data_size.to_bytes(4, "little")
    return b"RIFF" + (len(body) + data_size).to_bytes(4, "little") + body + b"\x00" * data_size


def ogg_page(granule: int, payload: bytes, header_type: int = 0) -> bytes:
    """One Ogg page: the 27-byte header, a segment table, and the payload (under 255 bytes here)."""
    return (b"OggS\x00" + bytes([header_type]) + granule.to_bytes(8, "little") + b"\x01\x00\x00\x00"
            + b"\x00" * 8 + bytes([1, len(payload)]) + payload)


def ogg(seconds: float) -> bytes:
    return (ogg_page(0, b"OpusHead" + b"\x00" * 11, 2) + ogg_page(0xFFFFFFFFFFFFFFFF, b"x" * 100)
            + ogg_page(int(seconds * 48000), b"y" * 60, 4))


class Chunks(httpx.SyncByteStream):
    """A streamed reply, counting how many chunks were taken."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self.chunks, self.taken = chunks, 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.taken += 1
            yield chunk


class FakeSpeech:
    """The two speech models. `stt` and `tts` are (status, body) or an exception to raise."""

    def __init__(self) -> None:
        self.stt: tuple[int, object] | Exception = (200, {"text": "What was delivered this week?"})
        self.tts: tuple[int, bytes] | Exception = (200, FAKE_WAV)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.stt if request.url.path.endswith("/transcriptions") else self.tts
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        if isinstance(body, Chunks):
            return httpx.Response(status, stream=body)
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return _ledger(tmp_path)


@pytest.fixture
def world(store: Path) -> Iterator[tuple[TestClient, FakeModel, FakeSpeech]]:
    app = create_app(db_path=store, tenants_path=TENANTS_TEST)
    model, speech = FakeModel(), FakeSpeech()
    app.state.chat = _chat(store, model)
    app.state.voice = voice.Voice(voice.VoiceSettings(), transport=httpx.MockTransport(speech.handler))
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        login(client)
        yield client, model, speech


def _ask(client: TestClient, audio: bytes = WEBM, content_type: str = "audio/webm;codecs=opus",
         **extra: str) -> httpx.Response:
    r: httpx.Response = client.post("/api/voice/ask", files={"audio": ("question.webm", audio, content_type)},
                                    data=extra or None, headers=ORIGIN)
    return r


# --- the happy path -------------------------------------------------------------------------------------


def test_a_spoken_question_gets_the_chats_answer_and_audio(world: tuple[TestClient, FakeModel, FakeSpeech]) -> None:
    client, model, speech = world
    model.replies.append('{"query": "delivered", "args": {"period": "this week"}}')
    r = _ask(client)
    assert r.status_code == 200
    d = r.json()
    assert d["transcript"] == "What was delivered this week?"
    assert "2 features were merged and checked this week" in d["answer"]["text"]
    assert d["answer"]["kind"] == "answer"
    assert {"kind": "feature", "label": d["answer"]["refs"][0]["label"], "href": "/features/FEAT-0001"} \
        in d["answer"]["refs"]
    assert base64.b64decode(d["audio"]["base64"]) == FAKE_WAV and d["audio"]["content_type"] == "audio/wav"
    assert d["spoken"] == voice.spoken_version(d["answer"]["text"], 45)
    assert d["spoken"].startswith("2 features were merged") and "(FEAT-" not in d["spoken"]
    assert d["voice_note"] == ""
    # the browser's fragment: the chat's own exchange, marked as spoken, links included
    assert 'class="chat-exchange chat-exchange--answer"' in d["html"]
    assert 'What was delivered this week? <span class="chat-exchange__how">Spoken</span></p>' in d["html"]
    assert 'href="/features/FEAT-0001"' in d["html"]
    # the chat model saw the transcript as the question
    assert model.calls[0][0][-1] == {"role": "user", "content": "What was delivered this week?"}
    # the speech models were called as the groundwork says
    stt, tts = speech.requests
    assert str(stt.url) == voice.DEFAULT_STT_URL
    body = stt.content
    assert b'name="model"' in body and b"parakeet-tdt-0.6b-v3" in body
    assert b'name="file"; filename="question.webm"' in body and WEBM in body
    assert str(tts.url) == voice.DEFAULT_TTS_URL
    assert json.loads(tts.content) == {"model": "qwen3-tts-0.6b", "voice": "Ryan", "input": d["spoken"],
                                       "response_format": "wav"}


@pytest.mark.parametrize(("audio", "content_type", "filename"), [
    (WEBM, "audio/webm;codecs=opus", "question.webm"),
    (ogg(4), "audio/ogg;codecs=opus", "question.ogg"),
    (b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 100, "audio/mp4", "question.m4a"),
    (wav(3), "audio/wav", "question.wav"),
])
def test_each_browsers_recording_format_is_accepted(world: tuple[TestClient, FakeModel, FakeSpeech],
                                                    audio: bytes, content_type: str, filename: str) -> None:
    client, _, speech = world
    r = _ask(client, audio, content_type)
    assert r.status_code == 200, r.text
    assert f'filename="{filename}"'.encode() in speech.requests[0].content


def test_a_follow_up_reply_by_voice(world: tuple[TestClient, FakeModel, FakeSpeech]) -> None:
    client, model, speech = world
    speech.stt = (200, {"text": "acme notes"})
    model.replies.append('{"query": "project_summary", "args": {"project": "acme/notes"}}')
    r = _ask(client, prior_question="How is the project?", prior_clarify="Which project do you mean?")
    assert r.status_code == 200
    messages = model.calls[0][0]
    assert messages[1:] == [{"role": "user", "content": "How is the project?"},
                            {"role": "assistant", "content": '{"clarify": "Which project do you mean?"}'},
                            {"role": "user", "content": "acme notes"}]


def test_settings_come_from_the_environment(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {"FACTORY_DASH_STT_URL": "http://spark.test:9000/v1/audio/transcriptions",
                        "FACTORY_DASH_STT_MODEL": "other-stt", "FACTORY_DASH_TTS_URL": "http://spark.test/tts",
                        "FACTORY_DASH_TTS_MODEL": "other-tts", "FACTORY_DASH_TTS_VOICE": "Vivian",
                        "FACTORY_DASH_VOICE_TIMEOUT": "12", "FACTORY_DASH_SPOKEN_WORDS": "30"}.items():
        monkeypatch.setenv(name, value)
    s = voice.VoiceSettings.from_env()
    assert s == voice.VoiceSettings("http://spark.test:9000/v1/audio/transcriptions", "other-stt",
                                    "http://spark.test/tts", "other-tts", "Vivian", 12.0, 30)
    monkeypatch.setenv("FACTORY_DASH_STT_URL", "")
    assert voice.VoiceSettings.from_env().stt_url == voice.DEFAULT_STT_URL
    for bad in ("", "0", "-5", "lots"):
        monkeypatch.setenv("FACTORY_DASH_SPOKEN_WORDS", bad)
        assert voice.VoiceSettings.from_env().spoken_words == 45
    monkeypatch.setenv("FACTORY_DASH_SPOKEN_WORDS", "30")
    assert create_app(db_path=store, tenants_path=TENANTS_TEST).state.voice.settings.tts_voice == "Vivian"


def test_the_spoken_version_is_the_first_piece() -> None:
    sentence = "This sentence has exactly ten words in it, you see. "
    rest = " " + voice.RESTS_ON_SCREEN
    # whole sentences up to the limit (45 by default: four sentences), then the honest ending
    spoken = voice.spoken_version(sentence * 30)
    assert spoken == (sentence * 4).strip() + rest
    assert voice.spoken_version(sentence * 30, 120) == (sentence * 12).strip() + rest
    # exactly at the limit, or under it: all of it, no ending
    assert voice.spoken_version(sentence * 4, 40) == (sentence * 4).strip()
    assert voice.spoken_version("Short answer. Two sentences.") == "Short answer. Two sentences."
    # one sentence longer than the limit: its first whole words, never part of a word
    one_long = " ".join(["unbelievably"] * 300)
    assert voice.spoken_version(one_long) == " ".join(["unbelievably"] * 45) + rest
    # a short first sentence and a long second one: stop at the end of the first
    assert voice.spoken_version("It is fine. " + " ".join(["word"] * 60) + ".") == "It is fine." + rest
    assert voice.spoken_version("Search box. (FEAT-0001) on 16 Oct 2026; and Export. (FEAT-0007) today.") \
        == "Search box. on 16 Oct 2026; and Export. today."


def test_a_long_answer_speaks_only_its_first_piece(world: tuple[TestClient, FakeModel, FakeSpeech],
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, speech = world
    long_text = "One two three four five six seven eight nine ten. " * 20
    monkeypatch.setattr(service.Chat, "ask", lambda self, q, user, follow_up=None: service.Answer(long_text))
    d = _ask(client).json()
    assert d["answer"]["text"] == long_text
    spoken = json.loads(speech.requests[1].content)["input"]
    assert spoken == "One two three four five six seven eight nine ten. " * 4 + voice.RESTS_ON_SCREEN
    assert d["spoken"] == spoken


# --- each failure falls back to text --------------------------------------------------------------------


@pytest.mark.parametrize("failure", [
    (500, {"error": "down"}),
    (503, {"error": "loading"}),
    (200, {"no": "text"}),
    (200, b"not json"),
    httpx.ReadTimeout("slow"),
    httpx.ConnectError("refused"),
])
def test_when_speech_to_text_fails_there_is_no_question(world: tuple[TestClient, FakeModel, FakeSpeech],
                                                        failure: object) -> None:
    client, model, speech = world
    speech.stt = failure  # type: ignore[assignment]
    r = _ask(client)
    assert r.status_code == 503
    assert r.json() == {"message": voice.CANT_HEAR}
    assert model.calls == []                         # no question, so no chat model call
    assert speech.paths() == ["/v1/audio/transcriptions"]


def test_a_recording_with_no_words_says_so(world: tuple[TestClient, FakeModel, FakeSpeech]) -> None:
    client, model, speech = world
    speech.stt = (200, {"text": "   "})
    r = _ask(client)
    assert r.status_code == 422 and r.json() == {"message": voice.HEARD_NOTHING}
    assert model.calls == []


@pytest.mark.parametrize("failure", [
    (500, b"boom"), (200, b"<html>not a wav</html>"), httpx.ReadTimeout("slow"), httpx.ConnectError("refused"),
])
def test_when_text_to_speech_fails_the_answer_is_text(world: tuple[TestClient, FakeModel, FakeSpeech],
                                                      failure: object) -> None:
    client, model, speech = world
    speech.tts = failure  # type: ignore[assignment]
    model.replies.append('{"query": "open_issues"}')
    r = _ask(client)
    assert r.status_code == 200
    d = r.json()
    assert "open issue" in d["answer"]["text"]
    assert d["audio"] is None and d["spoken"] == "" and d["voice_note"] == voice.CANT_SPEAK
    assert voice.CANT_SPEAK.replace("'", "&#39;") in d["html"]
    assert "Traceback" not in r.text


def test_when_the_chat_is_not_switched_on_no_model_is_called(store: Path) -> None:
    app = create_app(db_path=store, tenants_path=TENANTS_TEST)
    speech = FakeSpeech()
    app.state.chat = _chat(store, FakeModel(configured=False))
    app.state.voice = voice.Voice(voice.VoiceSettings(), transport=httpx.MockTransport(speech.handler))
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        login(client)
        r = _ask(client)
    assert r.status_code == 503 and r.json() == {"message": NOT_SET_UP}
    assert speech.requests == []


# --- the recording is checked first ---------------------------------------------------------------------


@pytest.mark.parametrize("content_type", ["text/plain", "video/webm", "application/octet-stream", "image/png"])
def test_a_recording_in_another_format_is_refused(world: tuple[TestClient, FakeModel, FakeSpeech],
                                                  content_type: str) -> None:
    client, _, speech = world
    r = _ask(client, content_type=content_type)
    assert r.status_code == 415 and r.json() == {"message": voice.NOT_AUDIO}
    assert speech.requests == []


def test_an_oversize_or_overlong_recording_is_refused(world: tuple[TestClient, FakeModel, FakeSpeech]) -> None:
    client, _, speech = world
    too_big_body = _ask(client, b"x" * (voice.MAX_BODY_BYTES + 1), "audio/wav")
    assert too_big_body.status_code == 413 and too_big_body.json() == {"message": voice.TOO_BIG}
    too_big_webm = _ask(client, WEBM + b"x" * voice.MAX_COMPRESSED_BYTES)
    assert too_big_webm.status_code == 413 and too_big_webm.json() == {"message": voice.TOO_BIG}
    long_wav = _ask(client, wav(31), "audio/wav")
    assert long_wav.status_code == 413 and long_wav.json() == {"message": voice.TOO_LONG}
    long_ogg = _ask(client, ogg(45), "audio/ogg")
    assert long_ogg.status_code == 413 and long_ogg.json() == {"message": voice.TOO_LONG}
    assert speech.requests == []
    assert _ask(client, wav(29), "audio/wav").status_code == 200


def test_a_request_without_a_recording_is_refused(world: tuple[TestClient, FakeModel, FakeSpeech]) -> None:
    client, _, speech = world
    empty = _ask(client, b"")
    assert empty.status_code == 400 and empty.json() == {"message": voice.NO_RECORDING}
    no_field = client.post("/api/voice/ask", data={"question": "hi"}, headers=ORIGIN)
    assert no_field.status_code == 400 and no_field.json() == {"message": voice.NO_RECORDING}
    garbled = client.post("/api/voice/ask", content=b"--x\r\nnonsense", headers={
        **ORIGIN, "Content-Type": "multipart/form-data; boundary=x"})
    assert garbled.status_code == 400
    assert speech.requests == []


# --- the guards -----------------------------------------------------------------------------------------


def test_voice_needs_sign_in(tmp_path: Path) -> None:
    app = create_app(db_path=tmp_path / "rm.db", tenants_path=TENANTS_TEST)
    with TestClient(app, base_url=LOOPBACK_URL) as c:
        assert _ask(c).status_code == 401


def test_voice_refuses_a_client_account(clientco_client: TestClient) -> None:
    assert _ask(clientco_client).status_code == 403
    assert "chat-mic" not in clientco_client.get("/").text


def test_voice_from_another_site_is_refused(world: tuple[TestClient, FakeModel, FakeSpeech]) -> None:
    client, model, speech = world
    r = client.post("/api/voice/ask", files={"audio": ("q.webm", WEBM, "audio/webm")},
                    headers={"Origin": "https://elsewhere.example"})
    assert r.status_code == 403 and r.json() == {"message": voice.CROSS_SITE}
    assert speech.requests == [] and model.calls == []


# --- the chat's limits cover voice ----------------------------------------------------------------------


def _limited_world(store: Path, limit: int) -> tuple[TestClient, FakeModel, FakeSpeech]:
    app = create_app(db_path=store, tenants_path=TENANTS_TEST)
    model, speech = FakeModel(), FakeSpeech()
    app.state.chat = _chat(store, model, limit=HourlyLimit(limit))
    app.state.voice = voice.Voice(voice.VoiceSettings(), transport=httpx.MockTransport(speech.handler))
    client = TestClient(app, base_url=LOOPBACK_URL)
    login(client)
    return client, model, speech


@pytest.mark.parametrize("first", ["text", "voice"])
def test_voice_and_text_share_the_hourly_limit(store: Path, first: str) -> None:
    client, model, speech = _limited_world(store, 1)
    ask_text: Callable[[], str] = lambda: client.post(  # noqa: E731
        "/chat", data={"question": "Any issues?"}, headers={"HX-Request": "true", **ORIGIN}).text
    def ask_voice() -> str:
        d = _ask(client).json()
        return str(d["answer"]["text"] if "answer" in d else d["message"])

    if first == "text":
        first_answer = ask_text()
        speech.requests.clear()
        second_answer = ask_voice()
        assert speech.requests == []  # refused before any speech model is called
    else:
        first_answer, second_answer = ask_voice(), ask_text()
    assert LIMITED not in first_answer.replace("&#39;", "'")
    assert LIMITED in second_answer.replace("&#39;", "'")
    assert len(model.calls) == 1


def test_the_limit_check_takes_nothing() -> None:
    now = [0.0]
    limit = HourlyLimit(2, clock=lambda: now[0])
    assert not limit.full() and not limit.full()
    assert limit.try_take() and not limit.full() and limit.try_take()
    assert limit.full() and not limit.try_take()
    now[0] = 3600.0
    assert not limit.full()


def test_a_voice_question_waits_for_no_one(store: Path) -> None:
    """One question at a time: a spoken question while a typed one is being answered gets the chat's
    busy sentence (spoken), and a second spoken question while one is in hand is refused at once."""
    gate = threading.Event()
    client, model, _ = _limited_world(store, 30)
    model.gate = gate
    worker = threading.Thread(target=lambda: client.post(
        "/chat", data={"question": "Any issues?"}, headers={"HX-Request": "true", **ORIGIN}))
    worker.start()
    assert model.entered.wait(5)
    busy = _ask(client).json()
    assert busy["answer"]["kind"] == "busy"
    gate.set()
    worker.join(5)
    app = client.app
    assert app.state.voice.one_at_a_time.acquire(timeout=5)  # type: ignore[attr-defined]
    try:
        r = _ask(client)
    finally:
        app.state.voice.one_at_a_time.release()  # type: ignore[attr-defined]
    assert r.status_code == 429 and r.json() == {"message": voice.VOICE_BUSY}


# --- the page -------------------------------------------------------------------------------------------


def test_the_chat_carries_a_hidden_microphone(world: tuple[TestClient, FakeModel, FakeSpeech]) -> None:
    client, _, _ = world
    for path in ("/chat", "/"):
        body = client.get(path).text
        assert body.count('class="chat-mic"') == 1, path
        assert '<button type="button" class="chat-mic" aria-pressed="false" hidden>' in body
        assert '<div class="chat-voice" role="status" hidden>' in body
        assert body.count('class="chat-voice__bar"') == 7
        assert '<script src="/static/mic.js" defer></script>' in body
        assert '<script src="/static/chat.js" defer></script>' in body
    assert "<title>Ask about the factory · Appmilla Factory</title>" in client.get("/chat").text
    assert client.get("/static/mic.js").status_code == 200


# --- coach round 1 ------------------------------------------------------------------------------------


def _voice_with(store: Path, speech: FakeSpeech, *, real_clock: bool = False, **settings: object) -> TestClient:
    app = create_app(db_path=store, tenants_path=TENANTS_TEST)
    app.state.chat = _chat(store, FakeModel())
    ticks = iter(range(10**6))
    clock = time.monotonic if real_clock else lambda: float(next(ticks))  # or one second at every look
    app.state.voice = voice.Voice(voice.VoiceSettings(**settings), transport=httpx.MockTransport(speech.handler),  # type: ignore[arg-type]
                                  clock=clock)
    client = TestClient(app, base_url=LOOPBACK_URL)
    login(client)
    return client


def test_an_oversize_speech_reply_is_not_read_to_the_end(store: Path) -> None:
    speech = FakeSpeech()
    tts = Chunks(iter([b"RIFF" + b"\x00" * (1024 * 1024 - 4)] + [b"\x00" * 1024 * 1024] * 59))  # 60 MB
    speech.tts = (200, tts)  # type: ignore[assignment]
    d = _ask(_voice_with(store, speech)).json()
    assert d["audio"] is None and d["voice_note"] == voice.CANT_SPEAK
    assert tts.taken <= voice.MAX_TTS_REPLY_BYTES // (1024 * 1024) + 1
    speech = FakeSpeech()
    stt = Chunks(iter([b'{"text": "' + b"a" * 1024] * 200))  # 200 KB
    speech.stt = (200, stt)
    r = _ask(_voice_with(store, speech))
    assert r.status_code == 503 and r.json() == {"message": voice.CANT_HEAR}
    assert stt.taken <= voice.MAX_STT_REPLY_BYTES // 1024 + 1


def test_a_trickling_speech_reply_is_cut_off_at_the_deadline(store: Path) -> None:
    speech = FakeSpeech()
    tts = Chunks(iter([b"RIFF"] + [b"\x00"] * 1000))
    speech.tts = (200, tts)  # type: ignore[assignment]
    d = _ask(_voice_with(store, speech, timeout_seconds=2.0)).json()
    assert d["audio"] is None and d["voice_note"] == voice.CANT_SPEAK
    assert tts.taken <= 3
    speech = FakeSpeech()
    stt = Chunks(iter([b'{"text": "hello"'] + [b" "] * 1000 + [b"}"]))
    speech.stt = (200, stt)
    r = _ask(_voice_with(store, speech, timeout_seconds=2.0))
    assert r.status_code == 503 and stt.taken <= 3


def test_a_wav_must_say_its_length_truthfully(world: tuple[TestClient, FakeModel, FakeSpeech]) -> None:
    client, _, speech = world
    good = wav(20)
    lying_rate = good[:28] + (1_000_000).to_bytes(4, "little") + good[32:]     # stated byte rate 1 MB/s
    no_fmt = good[:12] + good[36:]                                                # sound data, no format
    data_first = good[:12] + good[36:44] + good[12:36] + good[44:]               # format after the sound
    no_data = good[:36]
    # a second format chunk, honest in itself, saying a faster rate so the length looks shorter
    fast_fmt = b"fmt " + (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + (2).to_bytes(2, "little") \
        + (48000).to_bytes(4, "little") + (192000).to_bytes(4, "little") + (4).to_bytes(2, "little") \
        + (16).to_bytes(2, "little")
    two_fmt = good[:36] + fast_fmt + good[36:]
    for refused in (lying_rate, no_fmt, data_first, no_data, two_fmt, b"RIFF....WAVEjunk", b"not a wav at all"):
        r = _ask(client, refused, "audio/wav")
        assert r.status_code == 415 and r.json() == {"message": voice.NOT_AUDIO}
    # sound hidden after the data chunk still counts: 20 s stated plus 15 s of "junk"
    padded = good + b"junk" + (15 * 16000).to_bytes(4, "little") + b"\x00" * (15 * 16000)
    r = _ask(client, padded, "audio/wav")
    assert r.status_code == 413 and r.json() == {"message": voice.TOO_LONG}
    assert speech.requests == []
    assert voice.wav_seconds(good) == 20.0


def test_an_ogg_must_say_its_length(world: tuple[TestClient, FakeModel, FakeSpeech]) -> None:
    client, _, speech = world
    for refused in (b"OggS" + b"x" * 200, ogg(10) + b"trailing", ogg(10)[:-10], WEBM,
                    ogg_page(0xFFFFFFFFFFFFFFFF, b"x" * 50)):
        for label in ("audio/ogg;codecs=opus", "audio/opus"):
            r = _ask(client, refused, label)
            assert r.status_code == 415 and r.json() == {"message": voice.NOT_AUDIO}, (refused[:8], label)
    assert speech.requests == []
    assert voice.ogg_seconds(ogg(10)) == 10.0
    assert voice.MAX_COMPRESSED_BYTES <= 1024 * 1024


@pytest.fixture
def form_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts reads of the request's form, so a refusal can be shown to come before the body is read."""
    calls: list[int] = []
    real = Request.form

    def counting(self: Request, **kw: object):  # type: ignore[no-untyped-def]
        calls.append(1)
        return real(self, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Request, "form", counting)
    return calls


def test_a_body_without_a_length_is_refused(world: tuple[TestClient, FakeModel, FakeSpeech],
                                            form_reads: list[int]) -> None:
    client, _, speech = world
    r = client.post("/api/voice/ask", content=iter([b"--x\r\n", b"chunked"]), headers={
        **ORIGIN, "Content-Type": "multipart/form-data; boundary=x"})
    assert r.status_code == 411 and r.json() == {"message": voice.NO_LENGTH}
    assert form_reads == [] and speech.requests == []


def test_an_oversize_declared_length_is_refused_before_reading(world: tuple[TestClient, FakeModel, FakeSpeech],
                                                               form_reads: list[int]) -> None:
    client, _, speech = world
    r = client.post("/api/voice/ask", content=b"x", headers={
        **ORIGIN, "Content-Type": "multipart/form-data; boundary=x", "Content-Length": str(voice.MAX_BODY_BYTES + 1)})
    assert r.status_code == 413 and r.json() == {"message": voice.TOO_BIG}
    assert form_reads == [] and speech.requests == []


def test_a_caller_leaving_mid_upload_is_not_an_error(world: tuple[TestClient, FakeModel, FakeSpeech],
                                                     monkeypatch: pytest.MonkeyPatch,
                                                     caplog: pytest.LogCaptureFixture) -> None:
    client, _, speech = world

    async def gone(self: Request, **kw: object) -> None:
        raise ClientDisconnect()

    monkeypatch.setattr(Request, "form", gone)
    with caplog.at_level(logging.WARNING):
        r = _ask(client)
    assert r.status_code == 400 and r.content == b""
    assert not [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert speech.requests == []


def test_a_speech_model_silent_after_one_byte_cannot_hold_the_lock(store: Path) -> None:
    """The watchdog: the reply sends one byte and then nothing (a stand-in that blocks, so no read
    timeout can help); the spoken question still ends at the deadline and the voice lock is free."""
    release = threading.Event()

    def stalled() -> Iterator[bytes]:
        yield b"R"
        release.wait(10)
        yield b"IFF"

    speech = FakeSpeech()
    speech.tts = (200, Chunks(stalled()))  # type: ignore[assignment]
    client = _voice_with(store, speech, real_clock=True, timeout_seconds=0.5)
    start = time.monotonic()
    try:
        d = _ask(client).json()
        took = time.monotonic() - start
    finally:
        release.set()
    assert d["audio"] is None and d["voice_note"] == voice.CANT_SPEAK
    assert took < 3
    assert client.app.state.voice.one_at_a_time.acquire(blocking=False)  # type: ignore[attr-defined]
    client.app.state.voice.one_at_a_time.release()  # type: ignore[attr-defined]


def test_a_real_connection_silent_after_one_byte_is_cut_off() -> None:
    """Over a real socket on loopback: headers and one byte, a second byte just before the deadline, then
    silence. The second byte restarts httpx's own read timeout, so only the watchdog's socket shutdown
    can end the worker near the deadline (2 s) rather than at deadline plus timeout (3.6 s)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    stop = threading.Event()

    def serve() -> None:
        conn, _ = server.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\nR")
        if not stop.wait(1.6):
            conn.sendall(b"I")
        stop.wait(10)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    port = server.getsockname()[1]
    v = voice.Voice(voice.VoiceSettings(tts_url=f"http://127.0.0.1:{port}/v1/audio/speech", timeout_seconds=2.0))
    start = time.monotonic()
    try:
        with pytest.raises(voice.VoiceUnavailable):
            v.speak("Hello.")
        took = time.monotonic() - start
        while any(t.name == "voice-model-call" for t in threading.enumerate()) and time.monotonic() - start < 6:
            time.sleep(0.02)
        worker_gone_at = time.monotonic() - start
    finally:
        stop.set()
        server.close()
    assert took < 2.5
    assert worker_gone_at < 2.8, worker_gone_at


def test_the_speech_to_text_reply_cap(world: tuple[TestClient, FakeModel, FakeSpeech]) -> None:
    """A well-formed transcript reply over 64 KB is refused (only the cap can refuse it); one just under is
    read."""
    client, model, speech = world
    over = json.dumps({"text": "a" * voice.MAX_STT_REPLY_BYTES}).encode()
    speech.stt = (200, over)
    r = _ask(client)
    assert r.status_code == 503 and r.json() == {"message": voice.CANT_HEAR}
    assert model.calls == []
    under = json.dumps({"text": "Any issues? " + " " * (voice.MAX_STT_REPLY_BYTES - 100)}).encode()
    assert len(under) <= voice.MAX_STT_REPLY_BYTES
    speech.stt = (200, under)
    model.replies.append('{"query": "open_issues"}')
    r = _ask(client)
    assert r.status_code == 200 and r.json()["transcript"] == "Any issues?"


def test_every_voice_failure_is_one_sentence() -> None:
    sentences = [voice.NO_LENGTH, voice.TOO_BIG, voice.TOO_LONG, voice.NOT_AUDIO, voice.NO_RECORDING,
                 voice.CROSS_SITE, voice.VOICE_BUSY, voice.CANT_HEAR, voice.HEARD_NOTHING, voice.CANT_SPEAK]
    for sentence in sentences:
        assert len(re.findall(r"[.!?](\s|$)", sentence)) == 1, sentence
    mic = (Path(__file__).resolve().parent.parent / "frontend" / "static" / "mic.js").read_text(encoding="utf-8")
    words = re.findall(r'(?:error|offline|signin|failed): "([^"]+)"', mic)
    words += re.findall(r'set\("idle", "([^"]+)"\)', mic)
    assert len(words) == 5
    for sentence in words:
        assert len(re.findall(r"[.!?](\s|$)", sentence)) == 1, sentence
