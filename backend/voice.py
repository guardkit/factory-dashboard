"""Voice for the chat (design 2026-10-17, requirement 10): a spoken question in, a spoken answer out.

`POST /api/voice/ask` takes one recording (multipart field `audio`), turns it into text with the
speech-to-text model, asks the chat exactly as the text box does (`Chat.ask`, so the chat's
one-at-a-time rule and its hourly limit cover voice too), and reads a short version of the answer
aloud with the text-to-speech model. The full text answer is always returned. Nothing here is
browser-specific: a robot can send the same recording and play the same audio later.

The models sit behind the estate's llama-swap on :9000 and are moving machines, so their addresses,
names and voice are settings (environment variables, with the defaults below). No key is needed.

Failures never show a stack trace. If speech-to-text fails there is no question, so the reply says
so in one sentence and the text box carries on. If text-to-speech fails, the answer comes back as
text with one sentence saying voice isn't available right now.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.auth import ResolvedUser
from backend.chat.service import LIMITED, NOT_SET_UP, Answer, Chat, FollowUp

log = logging.getLogger("factory_dashboard.voice")

DEFAULT_STT_URL = "http://127.0.0.1:9000/v1/audio/transcriptions"
DEFAULT_STT_MODEL = "parakeet-tdt-0.6b-v3"
DEFAULT_TTS_URL = "http://127.0.0.1:9000/v1/audio/speech"
DEFAULT_TTS_MODEL = "qwen3-tts-0.6b"
DEFAULT_TTS_VOICE = "Ryan"
DEFAULT_TIMEOUT_SECONDS = 30.0

MAX_SECONDS = 30
#: The text-to-speech model stops at about 60 seconds of audio, and the chat's answers (codes, dates,
#: counts) are read at about one word a second, so by default at most 45 words of an answer are spoken
#: (FACTORY_DASH_SPOKEN_WORDS). When an answer is cut, the spoken version ends with RESTS_ON_SCREEN.
DEFAULT_SPOKEN_WORDS = 45
RESTS_ON_SCREEN = "The rest is on screen."

#: Accepted recordings: Chrome and Firefox record webm or ogg (Opus); Safari records mp4 (AAC).
#: The value is the file name the speech-to-text server is sent, since it may decode by extension.
ACCEPTED_TYPES = {
    "audio/webm": "question.webm",
    "audio/ogg": "question.ogg",
    "audio/opus": "question.ogg",
    "audio/mp4": "question.m4a",
    "audio/m4a": "question.m4a",
    "audio/x-m4a": "question.m4a",
    "audio/wav": "question.wav",
    "audio/x-wav": "question.wav",
    "audio/wave": "question.wav",
}
#: 30 seconds of compressed audio at 128 kbps is about 480 KB; 30 seconds of uncompressed 48 kHz
#: stereo WAV is about 5.8 MB.
MAX_COMPRESSED_BYTES = 1024 * 1024
MAX_WAV_BYTES = 6 * 1024 * 1024
#: The whole upload, with room for the form's own few lines.
MAX_BODY_BYTES = MAX_WAV_BYTES + 64 * 1024
#: The most a speech model's reply may be: a transcript is a few hundred bytes; 45 spoken words are
#: about 1 MB of WAV (the 60-second stop is about 2.9 MB).
MAX_STT_REPLY_BYTES = 64 * 1024
MAX_TTS_REPLY_BYTES = 8 * 1024 * 1024

# Each refusal or failure is one plain sentence.
NO_LENGTH = "Please send the recording with its length."
TOO_BIG = "That recording is too large, so please keep a question under 30 seconds."
TOO_LONG = "That recording is longer than 30 seconds, so please ask a shorter question."
NOT_AUDIO = "That recording is in a format the dashboard can't read, so please type your question instead."
NO_RECORDING = "No recording came with the request, so please try again or type your question."
CROSS_SITE = "This question came from another website, so it was refused."
VOICE_BUSY = "Another spoken question is being answered just now, so please try again in a moment."
CANT_HEAR = "Voice isn't available right now, so I couldn't hear the question and you can type it in the box instead."
HEARD_NOTHING = "I didn't catch a question in that recording, so please try again or type it in the box."
CANT_SPEAK = "Voice isn't available right now, so the answer is in text only."


class VoiceUnavailable(Exception):
    """A voice model could not be reached, refused, timed out, or sent back something unusable."""


@dataclass(frozen=True)
class VoiceSettings:
    stt_url: str = DEFAULT_STT_URL
    stt_model: str = DEFAULT_STT_MODEL
    tts_url: str = DEFAULT_TTS_URL
    tts_model: str = DEFAULT_TTS_MODEL
    tts_voice: str = DEFAULT_TTS_VOICE
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    spoken_words: int = DEFAULT_SPOKEN_WORDS

    @classmethod
    def from_env(cls) -> VoiceSettings:
        """FACTORY_DASH_STT_URL, _STT_MODEL, _TTS_URL, _TTS_MODEL, _TTS_VOICE, _VOICE_TIMEOUT and
        _SPOKEN_WORDS; empty (or, for the numbers, not a positive number) means the default."""
        def env(name: str, default: str) -> str:
            return (os.environ.get(name) or "").strip() or default

        try:
            timeout = float(env("FACTORY_DASH_VOICE_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS)))
        except ValueError:
            timeout = DEFAULT_TIMEOUT_SECONDS
        raw_words = env("FACTORY_DASH_SPOKEN_WORDS", str(DEFAULT_SPOKEN_WORDS))
        spoken_words = int(raw_words) if raw_words.isdigit() and int(raw_words) > 0 else DEFAULT_SPOKEN_WORDS
        return cls(env("FACTORY_DASH_STT_URL", DEFAULT_STT_URL), env("FACTORY_DASH_STT_MODEL", DEFAULT_STT_MODEL),
                   env("FACTORY_DASH_TTS_URL", DEFAULT_TTS_URL), env("FACTORY_DASH_TTS_MODEL", DEFAULT_TTS_MODEL),
                   env("FACTORY_DASH_TTS_VOICE", DEFAULT_TTS_VOICE), timeout, spoken_words)


@dataclass
class _Call:
    """One speech-model call shared between the asking thread and its worker. The lock decides who goes
    last: once the asker has given up (`abandoned`), the worker's result is dropped."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    done: bool = False
    abandoned: bool = False
    body: bytes = b""
    error: VoiceUnavailable | None = None
    sockets: list[socket.socket] = field(default_factory=list)

    def finish(self, body: bytes = b"", error: VoiceUnavailable | None = None) -> None:
        with self.lock:
            if not self.abandoned:
                self.done, self.body, self.error = True, body, error

    def trace(self, event: str, info: dict[str, object]) -> None:
        """httpcore's trace hook: keep each new connection's socket, so the asker can shut it."""
        get = getattr(info.get("return_value"), "get_extra_info", None)
        sock = get("socket") if callable(get) and event.endswith("connect_tcp.complete") else None
        if isinstance(sock, socket.socket):
            with self.lock:
                self.sockets.append(sock)
                late = self.abandoned
            if late:
                _shut(sock)

    def give_up(self) -> bool:
        """Called by the asker when its time is up; False when the worker had already finished."""
        with self.lock:
            if self.done:
                return False
            self.abandoned = True
            sockets = list(self.sockets)
        for sock in sockets:
            _shut(sock)
        return True


def _shut(sock: socket.socket) -> None:
    """Shut a socket another thread may be blocked reading: its read then returns at once."""
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


class Voice:
    """The two model calls (bytes in, bytes out), and one spoken question at a time.
    `transport` lets the tests stand in for the models."""

    def __init__(self, settings: VoiceSettings | None = None, *,
                 transport: httpx.BaseTransport | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings or VoiceSettings.from_env()
        self._transport = transport
        self._clock = clock
        self.one_at_a_time = threading.Lock()

    def _post(self, what: str, url: str, cap: int, **request: Any) -> bytes:
        """The reply's body within one overall deadline (the timeout setting) of wall-clock time, and at
        most `cap` bytes. The request runs in a worker thread; this thread waits for it at most that
        long and, when the time is up, shuts the connection's socket so the worker's read fails at once
        (the approach of Part D's chat-model call). So a model that is slow, silent after one byte, or
        trickling cannot hold the voice lock past the limit. Raises VoiceUnavailable on any failure."""
        t = self.settings.timeout_seconds
        call = _Call()
        worker = threading.Thread(target=self._run, args=(call, what, url, cap, request), daemon=True,
                                  name="voice-model-call")
        worker.start()
        worker.join(t)
        if call.give_up():
            raise VoiceUnavailable(f"{what}: no reply within {t:g} s")
        if call.error is not None:
            raise call.error
        return call.body

    def _run(self, call: _Call, what: str, url: str, cap: int, request: dict[str, Any]) -> None:
        try:
            call.finish(body=self._request(call, what, url, cap, request))
        except VoiceUnavailable as exc:
            call.finish(error=exc)
        except Exception as exc:  # anything else is the model being unavailable, never a crash
            call.finish(error=VoiceUnavailable(f"{what}: {type(exc).__name__}"))

    def _request(self, call: _Call, what: str, url: str, cap: int, request: dict[str, Any]) -> bytes:
        t = self.settings.timeout_seconds
        deadline = self._clock() + t
        body = bytearray()
        try:
            with (httpx.Client(transport=self._transport, timeout=httpx.Timeout(t, connect=min(5.0, t))) as client,
                  client.stream("POST", url, extensions={"trace": call.trace}, **request) as r):
                r.raise_for_status()
                for chunk in r.iter_bytes():  # as they arrive, so both limits are checked often
                    body += chunk
                    if len(body) > cap:
                        raise VoiceUnavailable(f"{what}: reply larger than {cap} bytes")
                    if self._clock() > deadline or call.abandoned:
                        raise VoiceUnavailable(f"{what}: reply took longer than {t:g} s")
        except httpx.HTTPError as exc:
            raise VoiceUnavailable(f"{what}: {type(exc).__name__}") from None
        return bytes(body)

    def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        body = self._post("speech-to-text", self.settings.stt_url, MAX_STT_REPLY_BYTES,
                          data={"model": self.settings.stt_model}, files={"file": (filename, audio, content_type)})
        try:
            text = json.loads(body)["text"]
        except (ValueError, KeyError, TypeError):
            raise VoiceUnavailable("speech-to-text: no text in the reply") from None
        if not isinstance(text, str):
            raise VoiceUnavailable("speech-to-text: no text")
        return text

    def speak(self, text: str) -> bytes:
        wav = self._post("text-to-speech", self.settings.tts_url, MAX_TTS_REPLY_BYTES,
                         json={"model": self.settings.tts_model, "voice": self.settings.tts_voice, "input": text,
                               "response_format": "wav"})
        if wav[:4] != b"RIFF":
            raise VoiceUnavailable("text-to-speech: not a WAV")
        return wav


# --- the recording ---------------------------------------------------------------------------------------


def base_type(content_type: str) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def sniff(audio: bytes) -> str | None:
    """The recording's format from its own first bytes, never from its label: "wav" (RIFF....WAVE),
    "ogg" (OggS), "webm" (the EBML header 1A 45 DF A3, as WebM and Matroska start) or "m4a" ("ftyp" at
    offset 4, as MP4 starts). None for anything else."""
    if audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
        return "wav"
    if audio[:4] == b"OggS":
        return "ogg"
    if audio[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if audio[4:8] == b"ftyp":
        return "m4a"
    return None


def check_recording(audio: bytes, content_type: str) -> tuple[int, str] | None:  # noqa: PLR0911 (one per refusal)
    """(status, sentence) when the recording is refused, else None.

    The format is worked out from the bytes (`sniff`), and must be one the dashboard accepts and the same
    family as the label (webm, ogg/opus, mp4/m4a, wav), or the recording is refused (415). Both rules,
    because the label then decides nothing: the size limit and the length check follow the bytes, and the
    speech-to-text model is sent a file name that matches what it will decode. A browser's own recordings
    always agree with their labels, so a disagreement is a mistake or a way round the length check.
    A WAV or Ogg recording must say its length, and one that cannot be read is refused (415). Chrome's
    webm and Safari's mp4 do not say, so for those the browser's 30-second stop and the 1 MB limit (about a
    minute even at 128 kbps) bound it."""
    kind = base_type(content_type)
    if kind not in ACCEPTED_TYPES:
        return 415, NOT_AUDIO
    if not audio:
        return 400, NO_RECORDING
    found = sniff(audio)
    if found is None or not ACCEPTED_TYPES[kind].endswith("." + found):
        return 415, NOT_AUDIO
    if len(audio) > (MAX_WAV_BYTES if found == "wav" else MAX_COMPRESSED_BYTES):
        return 413, TOO_BIG
    if found in ("webm", "m4a"):
        return None
    seconds = wav_seconds(audio) if found == "wav" else ogg_seconds(audio)
    if seconds is None:
        return 415, NOT_AUDIO
    if seconds > MAX_SECONDS + 0.5:
        return 413, TOO_LONG
    return None


def wav_seconds(b: bytes) -> float | None:
    """The length of a WAV, from everything after the start of its sound data (so trailing chunks count)
    at the byte rate its sample rate, channels and sample size give. None when it cannot be read: no
    format chunk before the sound, a stated byte rate that disagrees, or no sound data."""
    if b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        return None
    pos, byte_rate = 12, 0
    while pos + 8 <= len(b):
        chunk, size, body = b[pos:pos + 4], int.from_bytes(b[pos + 4:pos + 8], "little"), pos + 8
        if chunk == b"fmt ":
            if byte_rate or size < 16 or len(b) < body + 16:
                return None
            channels = int.from_bytes(b[body + 2:body + 4], "little")
            rate = int.from_bytes(b[body + 4:body + 8], "little")
            stated = int.from_bytes(b[body + 8:body + 12], "little")
            bits = int.from_bytes(b[body + 14:body + 16], "little")
            byte_rate = rate * channels * bits // 8
            if not byte_rate or stated != byte_rate:
                return None
        elif chunk == b"data":
            return (len(b) - body) / byte_rate if byte_rate else None
        pos = body + size + (size & 1)
    return None


def ogg_seconds(b: bytes) -> float | None:
    """Opus in Ogg: the highest granule position (48 kHz samples), walking every page from the start.
    None unless the pages run exactly to the end, at least one gives a position, and there is one logical
    stream only: every page carries the first page's serial number, and no page after the first begins a
    stream. (A chained file of two 20-second streams would otherwise read as 20 seconds; a browser's
    recording is always one stream.)"""
    pos, highest = 0, -1
    while pos < len(b):
        if b[pos:pos + 4] != b"OggS" or len(b) < pos + 27 or b[pos + 4] != 0:
            return None
        if pos and (b[pos + 14:pos + 18] != b[14:18] or b[pos + 5] & 0x02):  # another stream, or a new one
            return None
        segments = b[pos + 27:pos + 27 + b[pos + 26]]
        if len(segments) < b[pos + 26]:
            return None
        granule = int.from_bytes(b[pos + 6:pos + 14], "little")
        if granule != 0xFFFFFFFFFFFFFFFF:
            highest = max(highest, granule)
        pos += 27 + len(segments) + sum(segments)
    return highest / 48000 if pos == len(b) and highest >= 0 else None


# --- the spoken version ----------------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
#: "(FEAT-0001)" after a feature's title: shown on screen, but slow and unhelpful read aloud (the live
#: check on 18 October took about 52 seconds of audio for a 56-word answer full of codes and dates).
_RECORD_CODE = re.compile(r"\s*\([A-Z]+-[0-9A-Z]+\)")


def spoken_version(text: str, max_words: int = DEFAULT_SPOKEN_WORDS) -> str:
    """The first piece of the answer, split on sentences the way the study tutor splits for its speech
    model: whole sentences up to `max_words`, or, when the first sentence alone is longer, its first
    `max_words` whole words. When anything is left out, RESTS_ON_SCREEN is added (it is not counted).
    Record codes in brackets are left out of the spoken version only."""
    all_words = " ".join(_RECORD_CODE.sub("", text).split())
    words: list[str] = []
    for sentence in _SENTENCE_END.split(all_words):
        more = sentence.split()
        if words and len(words) + len(more) > max_words:
            break
        words.extend(more)
        if len(words) >= max_words:
            break
    spoken = " ".join(words[:max_words])
    return spoken if spoken == all_words else f"{spoken} {RESTS_ON_SCREEN}"


# --- one spoken question ---------------------------------------------------------------------------------


@dataclass
class VoiceReply:
    status: int = 200
    #: set when there is no answer (refused, busy, or the question could not be heard)
    message: str = ""
    transcript: str = ""
    answer: Answer | None = None
    spoken: str = ""
    audio: bytes = b""
    #: one plain sentence when the answer could not be read aloud
    voice_note: str = ""


def ask_by_voice(  # noqa: PLR0911 (one return per refusal)
    chat: Chat, voice: Voice, audio: bytes, content_type: str, user: ResolvedUser,
    follow_up: FollowUp | None = None,
) -> VoiceReply:
    """Speech to text, the chat, then text to speech. Never raises for a model failure."""
    refused = check_recording(audio, content_type)
    if refused:
        return VoiceReply(refused[0], refused[1])
    if not chat.model.configured:  # say so before spending a speech-to-text call on it
        return VoiceReply(503, NOT_SET_UP)
    if chat.limit.full():  # likewise; Chat.ask still takes the limit itself
        return VoiceReply(429, LIMITED)
    if not voice.one_at_a_time.acquire(blocking=False):
        return VoiceReply(429, VOICE_BUSY)
    try:
        try:
            heard = " ".join(voice.transcribe(audio, ACCEPTED_TYPES[base_type(content_type)], content_type).split())
        except VoiceUnavailable as exc:
            log.warning("voice: %s", exc)
            return VoiceReply(503, CANT_HEAR)
        if not heard:
            return VoiceReply(422, HEARD_NOTHING)
        answer = chat.ask(heard, user, follow_up=follow_up)
        spoken = spoken_version(answer.text, voice.settings.spoken_words)
        reply = VoiceReply(transcript=heard, answer=answer, spoken=spoken)
        try:
            reply.audio = voice.speak(reply.spoken)
        except VoiceUnavailable as exc:
            log.warning("voice: %s", exc)
            reply.spoken, reply.voice_note = "", CANT_SPEAK
        log.info("voice: %s, %d bytes spoken", answer.kind, len(reply.audio))
        return reply
    finally:
        voice.one_at_a_time.release()
