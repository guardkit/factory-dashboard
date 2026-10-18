"""Voice for the chat (design 2026-10-17, requirement 10): a spoken question in, a spoken answer out.

`POST /api/voice/ask` takes one recording (multipart field `audio`), turns it into text with the
speech-to-text model, asks the chat exactly as the text box does (`Chat.ask`, so the chat's
one-at-a-time rule and its hourly limit cover voice too), and reads a short version of the answer
aloud with the text-to-speech model. The full text answer is always returned. Nothing here is
browser-specific: a robot can send the same recording and play the same audio later.

`POST /api/voice/transcribe` (Part F) is the voice note for the suggestion box: the same recording
checks and one-at-a-time rule, then speech to text only (`transcribe_note`). The words go back to the
page for the person to check; the chat is not asked and nothing is spoken.

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
import math
import os
import re
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.auth import ResolvedUser
from backend.chat.service import LIMITED, NOT_SET_UP, Answer, Chat, FollowUp, HourlyLimit

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

#: Accepted recordings: Chrome, Edge and Firefox record webm (Opus); Safari records mp4 (AAC); WAV for
#: other callers. Ogg is not accepted at all: its length depends on the codec inside, and no browser the
#: dashboard supports needs it.
#: The value is the file name the speech-to-text server is sent, since it may decode by extension.
ACCEPTED_TYPES = {
    "audio/webm": "question.webm",
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

    def transcribe(self, audio: bytes, filename: str, content_type: str) -> tuple[str, float]:
        """(text, seconds): the transcript, and the length of the audio as the model decoded it
        (`response_format=verbose_json`). That length is the authority for the 30-second limit, whatever
        the file's headers say; a reply without a finite length is VoiceUnavailable, never a guess."""
        body = self._post("speech-to-text", self.settings.stt_url, MAX_STT_REPLY_BYTES,
                          data={"model": self.settings.stt_model, "response_format": "verbose_json"},
                          files={"file": (filename, audio, content_type)})
        try:
            reply = json.loads(body)
            text, seconds = reply["text"], reply.get("duration")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise VoiceUnavailable("speech-to-text: no text in the reply") from None
        if not isinstance(text, str):
            raise VoiceUnavailable("speech-to-text: no text")
        if isinstance(seconds, bool) or not isinstance(seconds, int | float) or not math.isfinite(seconds):
            raise VoiceUnavailable("speech-to-text: no decoded length in the reply")
        return text, float(seconds)

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
    "webm" (the EBML header 1A 45 DF A3, as WebM and Matroska start) or "m4a" ("ftyp" at offset 4, as MP4
    starts). None for anything else, Ogg included."""
    if audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
        return "wav"
    if audio[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if audio[4:8] == b"ftyp":
        return "m4a"
    return None


def check_recording(audio: bytes, content_type: str) -> tuple[int, str] | None:  # noqa: PLR0911 (one per refusal)
    """(status, sentence) when the recording is refused, else None.

    The format is worked out from the bytes (`sniff`), and must be one the dashboard accepts and the same
    family as the label (webm, mp4/m4a, wav), or the recording is refused (415); Ogg is refused whatever its
    label. Both rules, because the label then decides nothing: the size limit and the length check follow
    the bytes, and the speech-to-text model is sent a file name that matches what it will decode. A
    browser's own recordings always agree with their labels, so a disagreement is a mistake or a way round
    the length check.

    The 30 seconds are checked wherever the file states its length: a WAV always (and one whose length
    cannot be read is refused, 415); an MP4 in its movie header (mvhd); a WebM in its Info/Duration. A
    malformed MP4 box or WebM element is refused (415). Chrome's live WebM and Safari's fragmented MP4 may
    state no length at all; for those, the browser's 30-second stop and the 1 MB limit (about a minute even
    at 128 kbps) bound it."""
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
    try:
        seconds = {"wav": wav_seconds, "m4a": mp4_seconds, "webm": webm_seconds}[found](audio)
    except Malformed:
        return 415, NOT_AUDIO
    if seconds is None:
        return (415, NOT_AUDIO) if found == "wav" else None
    if seconds > MAX_SECONDS + 0.5:
        return 413, TOO_LONG
    return None


class Malformed(ValueError):
    """An MP4 box or WebM element whose size or contents do not fit the file."""


def _u(b: bytes, start: int, n: int) -> int:
    """An n-byte big-endian number at `start`; Malformed if it runs past the end."""
    if start < 0 or start + n > len(b):
        raise Malformed("read past the end")
    return int.from_bytes(b[start:start + n], "big")


def _boxes(b: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """(type, body start, body end) for each MP4 box in b[start:end]; the boxes must fill it exactly."""
    pos = start
    while pos < end:
        if end - pos < 8:
            raise Malformed("box header")
        size, kind, body = _u(b, pos, 4), b[pos + 4:pos + 8], pos + 8
        if size == 1:
            size, body = _u(b, pos + 8, 8), pos + 16
        elif size == 0:
            size = end - pos
        if size < body - pos or pos + size > end:
            raise Malformed("box size")
        yield kind, body, pos + size
        pos += size


def _stated(b: bytes, body: int, end: int, name: str) -> float | None:
    return _scaled(b, body, end, name)[1]


def _scaled(b: bytes, body: int, end: int, name: str) -> tuple[int, float | None]:
    """The length an mvhd or mdhd states (the same layout for both: version 0 has a 32-bit timescale and
    duration after two 32-bit times, version 1 has 64-bit times and duration). None when the duration is
    zero or "unknown" (all ones), with the timescale. Malformed for another version, a short box, or a
    timescale of zero."""
    version = _u(b, body, 1)
    if version == 0:
        scale, duration, unknown, last = _u(b, body + 12, 4), _u(b, body + 16, 4), 0xFFFFFFFF, body + 20
    elif version == 1:
        scale, duration, unknown, last = _u(b, body + 20, 4), _u(b, body + 24, 8), 0xFFFFFFFFFFFFFFFF, body + 32
    else:
        raise Malformed(f"{name} version")
    if last > end or not scale:
        raise Malformed(name)
    return scale, (None if duration in (0, unknown) else duration / scale)


def mp4_seconds(b: bytes) -> float | None:
    """The longest length an MP4 states: in moov/mvhd, moov/mvex/mehd, and every moov/trak/mdia/mdhd
    (an early refusal only: the speech model's decoded length is the authority). None only when none of
    them states a usable length (as in a fragmented recording, whose durations are zero). Malformed for a
    box that does not fit; the top-level boxes are all walked first, so they must fill the file exactly,
    and every box walked inside moov must be well formed too."""
    stated = [0.0]
    for kind, body, end in list(_boxes(b, 0, len(b))):
        if kind == b"moov":
            stated += _moov_lengths(b, body, end)
    return max(stated) or None


def _moov_lengths(b: bytes, body: int, end: int) -> list[float]:
    stated: list[float] = []
    movie_scale = 0
    for child, cbody, cend in _boxes(b, body, end):
        if child == b"mvhd":
            movie_scale, seconds = _scaled(b, cbody, cend, "mvhd")
            stated.append(seconds or 0.0)
        elif child == b"mvex":  # fragment_duration, in the movie's timescale (mvhd comes first)
            for part, pbody, pend in _boxes(b, cbody, cend):
                if part == b"mehd" and movie_scale:
                    width = 8 if _u(b, pbody, 1) == 1 else 4
                    if pbody + 4 + width > pend:
                        raise Malformed("mehd")
                    stated.append(_u(b, pbody + 4, width) / movie_scale)
        elif child == b"trak":
            for part, pbody, pend in _boxes(b, cbody, cend):
                if part == b"mdia":
                    stated += [_stated(b, mbody, mend, "mdhd") or 0.0
                               for media, mbody, mend in _boxes(b, pbody, pend) if media == b"mdhd"]
    return stated


_EBML, _SEGMENT, _INFO, _TIMECODE_SCALE, _DURATION = 0x1A45DFA3, 0x18538067, 0x1549A966, 0x2AD7B1, 0x4489


def _vint(b: bytes, pos: int, max_len: int, *, keep_marker: bool) -> tuple[int, int, bool]:
    """An EBML variable-length number at `pos`: (value, length, all value bits set)."""
    first = _u(b, pos, 1)
    length = 1
    while length <= max_len and not first & (0x80 >> (length - 1)):
        length += 1
    if length > max_len:
        raise Malformed("vint")
    value = _u(b, pos, length)
    bits = 7 * length
    data = value & ((1 << bits) - 1)
    return (value if keep_marker else data), length, data == (1 << bits) - 1


def _element(b: bytes, pos: int, end: int) -> tuple[int, int, int | None]:
    """(id, body start, body end or None for "unknown size") of the EBML element at `pos`."""
    ident, n, _ = _vint(b, pos, 4, keep_marker=True)
    size, m, unknown = _vint(b, pos + n, 8, keep_marker=False)
    body = pos + n + m
    if unknown:
        return ident, body, None
    if body + size > end:
        raise Malformed("element size")
    return ident, body, body + size


def webm_seconds(b: bytes) -> float | None:
    """The length a WebM states in Segment/Info/Duration (a float in TimecodeScale units, 1,000,000 ns
    by default). None when Info has no Duration, or no Info comes before an element of unknown size (a
    live recording's Cluster). Malformed for an element that does not fit."""
    ident, _, header_end = _element(b, 0, len(b))
    if ident != _EBML or header_end is None:
        raise Malformed("EBML header")
    ident, pos, segment_end = _element(b, header_end, len(b))
    if ident != _SEGMENT:
        raise Malformed("no Segment")
    end = len(b) if segment_end is None else segment_end
    while pos < end:
        ident, body, child_end = _element(b, pos, end)
        if child_end is None:
            return None
        if ident == _INFO:
            return _info_seconds(b, body, child_end)
        pos = child_end
    return None


def _info_seconds(b: bytes, pos: int, end: int) -> float | None:
    scale, duration = 1_000_000, None
    while pos < end:
        ident, body, child_end = _element(b, pos, end)
        if child_end is None:
            raise Malformed("unknown size inside Info")
        n = child_end - body
        if ident == _TIMECODE_SCALE:
            if not 1 <= n <= 8:
                raise Malformed("TimecodeScale")
            scale = _u(b, body, n)
        elif ident == _DURATION:
            if n not in (4, 8):
                raise Malformed("Duration")
            duration = struct.unpack(">f" if n == 4 else ">d", b[body:child_end])[0]
        pos = child_end
    if duration is None or duration == 0:
        return None
    if not scale or not math.isfinite(duration) or duration < 0:
        raise Malformed("Duration")
    return float(duration * scale / 1e9)


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
            text, seconds = voice.transcribe(audio, ACCEPTED_TYPES[base_type(content_type)], content_type)
        except VoiceUnavailable as exc:
            log.warning("voice: %s", exc)
            return VoiceReply(503, CANT_HEAR)
        if seconds > MAX_SECONDS + 0.5:  # the decoded length decides: the transcript is dropped unread
            log.warning("voice: refused %.1f s of decoded audio", seconds)
            return VoiceReply(413, TOO_LONG)
        heard = " ".join(text.split())
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


# --- a voice note for a suggestion (requirement 11): speech to text only -----------------------------------

#: At most this many voice notes per account in any hour (in memory; a restart clears it).
NOTES_PER_HOUR = 10
# The recording refusals, worded for a suggestion rather than a question.
NOTE_WORDS = {
    TOO_BIG: "That recording is too large, so please keep a voice note under 30 seconds.",
    TOO_LONG: "That recording is longer than 30 seconds, so please record a shorter note.",
    NOT_AUDIO: "That recording is in a format the dashboard can't read, so please type your suggestion instead.",
    NO_RECORDING: "No recording came with the request, so please try again or type your suggestion.",
}
NOTE_CANT_HEAR = "Voice isn't available right now, so please type your suggestion in the box."
NOTE_HEARD_NOTHING = "I didn't catch any words in that recording, so please try again or type your suggestion."
NOTE_BUSY = "The microphone is busy with another recording just now, so please try again in a moment."
NOTES_LIMITED = (f"You have recorded {NOTES_PER_HOUR} voice notes in the last hour, so please type your "
                 "suggestion instead.")
NOTE_CROSS_SITE = "This recording came from another website, so it was refused."
#: Part E's refusals reworded for the voice note, for the shared recording reader (backend/app.py).
NOTE_REFUSALS = {**NOTE_WORDS, CROSS_SITE: NOTE_CROSS_SITE}


class PerUserHourly:
    """At most `limit` in any rolling hour for each account (the chat's HourlyLimit, one per username)."""

    def __init__(self, limit: int = NOTES_PER_HOUR, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit, self._clock = limit, clock
        self._per_user: dict[str, HourlyLimit] = {}
        self._lock = threading.Lock()

    def try_take(self, username: str) -> bool:
        with self._lock:
            limit = self._per_user.setdefault(username, HourlyLimit(self._limit, self._clock))
        return limit.try_take()


@dataclass(frozen=True)
class NoteReply:
    status: int = 200
    #: set when there is no transcript
    message: str = ""
    transcript: str = ""


def transcribe_note(  # noqa: PLR0911 (one return per refusal)
    voice: Voice, audio: bytes, content_type: str, user: ResolvedUser,
    limit: PerUserHourly,
) -> NoteReply:
    """A voice note for the suggestion box: the same recording checks and the same one-at-a-time rule as a
    spoken question, then speech to text only. Nothing is asked of the chat and nothing is spoken back."""
    refused = check_recording(audio, content_type)
    if refused:
        return NoteReply(refused[0], NOTE_WORDS.get(refused[1], refused[1]))
    if not voice.one_at_a_time.acquire(blocking=False):
        return NoteReply(429, NOTE_BUSY)
    try:
        if not limit.try_take(user.username):
            return NoteReply(429, NOTES_LIMITED)
        try:
            text, seconds = voice.transcribe(audio, ACCEPTED_TYPES[base_type(content_type)], content_type)
        except VoiceUnavailable as exc:
            log.warning("voice note: %s", exc)
            return NoteReply(503, NOTE_CANT_HEAR)
        if seconds > MAX_SECONDS + 0.5:
            log.warning("voice note: refused %.1f s of decoded audio", seconds)
            return NoteReply(413, NOTE_WORDS[TOO_LONG])
        heard = " ".join(text.split())
        if not heard:
            return NoteReply(422, NOTE_HEARD_NOTHING)
        return NoteReply(transcript=heard)
    finally:
        voice.one_at_a_time.release()
