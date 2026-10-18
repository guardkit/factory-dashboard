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

import logging
import os
import re
import threading
from dataclasses import dataclass

import httpx

from backend.auth import ResolvedUser
from backend.chat.service import NOT_SET_UP, Answer, Chat, FollowUp

log = logging.getLogger("factory_dashboard.voice")

DEFAULT_STT_URL = "http://127.0.0.1:9000/v1/audio/transcriptions"
DEFAULT_STT_MODEL = "parakeet-tdt-0.6b-v3"
DEFAULT_TTS_URL = "http://127.0.0.1:9000/v1/audio/speech"
DEFAULT_TTS_MODEL = "qwen3-tts-0.6b"
DEFAULT_TTS_VOICE = "Ryan"
DEFAULT_TIMEOUT_SECONDS = 30.0

MAX_SECONDS = 30
#: The text-to-speech model stops at about 60 seconds of audio, so at most this many words are spoken.
MAX_SPOKEN_WORDS = 120

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
#: 30 seconds of compressed audio is well under 2 MB even at a high bit rate; 30 seconds of
#: uncompressed 48 kHz stereo WAV is about 5.8 MB.
MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
MAX_WAV_BYTES = 6 * 1024 * 1024
#: The whole upload, with room for the form's own few lines.
MAX_BODY_BYTES = MAX_WAV_BYTES + 64 * 1024

NO_LENGTH = "Please send the recording with its length."
TOO_BIG = "That recording is too large. Please keep a question under 30 seconds."
TOO_LONG = "That recording is longer than 30 seconds. Please ask a shorter question."
NOT_AUDIO = "That recording is in a format the dashboard can't read. Please type your question instead."
NO_RECORDING = "No recording came with the request. Please try again, or type your question."
CROSS_SITE = "This question came from another website, so it was refused."
VOICE_BUSY = "Another spoken question is being answered just now. Please try again in a moment."
CANT_HEAR = ("Voice isn't available right now, so I couldn't hear the question. "
             "Please type it in the box instead.")
HEARD_NOTHING = "I didn't catch a question in that recording. Please try again, or type it in the box."
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

    @classmethod
    def from_env(cls) -> VoiceSettings:
        """FACTORY_DASH_STT_URL, _STT_MODEL, _TTS_URL, _TTS_MODEL, _TTS_VOICE, _VOICE_TIMEOUT; empty means
        the default."""
        def env(name: str, default: str) -> str:
            return (os.environ.get(name) or "").strip() or default

        try:
            timeout = float(env("FACTORY_DASH_VOICE_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS)))
        except ValueError:
            timeout = DEFAULT_TIMEOUT_SECONDS
        return cls(env("FACTORY_DASH_STT_URL", DEFAULT_STT_URL), env("FACTORY_DASH_STT_MODEL", DEFAULT_STT_MODEL),
                   env("FACTORY_DASH_TTS_URL", DEFAULT_TTS_URL), env("FACTORY_DASH_TTS_MODEL", DEFAULT_TTS_MODEL),
                   env("FACTORY_DASH_TTS_VOICE", DEFAULT_TTS_VOICE), timeout)


class Voice:
    """The two model calls (bytes in, bytes out), and one spoken question at a time.
    `transport` lets the tests stand in for the models."""

    def __init__(self, settings: VoiceSettings | None = None, *,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings or VoiceSettings.from_env()
        self._transport = transport
        self.one_at_a_time = threading.Lock()

    def _client(self) -> httpx.Client:
        t = self.settings.timeout_seconds
        return httpx.Client(transport=self._transport, timeout=httpx.Timeout(t, connect=min(5.0, t)))

    def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        try:
            with self._client() as client:
                r = client.post(self.settings.stt_url, data={"model": self.settings.stt_model},
                                files={"file": (filename, audio, content_type)})
            r.raise_for_status()
            text = r.json()["text"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise VoiceUnavailable(f"speech-to-text: {type(exc).__name__}") from None
        if not isinstance(text, str):
            raise VoiceUnavailable("speech-to-text: no text")
        return text

    def speak(self, text: str) -> bytes:
        body = {"model": self.settings.tts_model, "voice": self.settings.tts_voice, "input": text,
                "response_format": "wav"}
        try:
            with self._client() as client:
                r = client.post(self.settings.tts_url, json=body)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise VoiceUnavailable(f"text-to-speech: {type(exc).__name__}") from None
        if r.content[:4] != b"RIFF":
            raise VoiceUnavailable("text-to-speech: not a WAV")
        return r.content


# --- the recording ---------------------------------------------------------------------------------------


def base_type(content_type: str) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def check_recording(audio: bytes, content_type: str) -> tuple[int, str] | None:
    """(status, sentence) when the recording is refused, else None. The length is checked where the
    file says it (WAV, Ogg); Chrome's webm and Safari's mp4 do not say, so for those the browser's
    30-second stop and the size limit bound it."""
    kind = base_type(content_type)
    if kind not in ACCEPTED_TYPES:
        return 415, NOT_AUDIO
    if not audio:
        return 400, NO_RECORDING
    is_wav = ACCEPTED_TYPES[kind].endswith(".wav")
    if len(audio) > (MAX_WAV_BYTES if is_wav else MAX_COMPRESSED_BYTES):
        return 413, TOO_BIG
    seconds = wav_seconds(audio) if is_wav else ogg_seconds(audio) if kind in ("audio/ogg", "audio/opus") else None
    if seconds is not None and seconds > MAX_SECONDS + 0.5:
        return 413, TOO_LONG
    return None


def wav_seconds(b: bytes) -> float | None:
    if b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        return None
    pos, byte_rate = 12, 0
    while pos + 8 <= len(b):
        chunk, size, body = b[pos:pos + 4], int.from_bytes(b[pos + 4:pos + 8], "little"), pos + 8
        if chunk == b"fmt " and size >= 16:
            byte_rate = int.from_bytes(b[body + 8:body + 12], "little")
        elif chunk == b"data" and byte_rate:
            return min(size, len(b) - body) / byte_rate
        pos = body + size + (size & 1)
    return None


def ogg_seconds(b: bytes) -> float | None:
    """Opus in Ogg: the last page's granule position counts 48 kHz samples."""
    at = b.rfind(b"OggS")
    if at < 0 or len(b) < at + 14 or b[at + 4] != 0:
        return None
    return int.from_bytes(b[at + 6:at + 14], "little") / 48000


# --- the spoken version ----------------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
#: "(FEAT-0001)" after a feature's title: shown on screen, but slow and unhelpful read aloud (the live
#: check on 18 October took about 52 seconds of audio for a 56-word answer full of codes and dates).
_RECORD_CODE = re.compile(r"\s*\([A-Z]+-[0-9A-Z]+\)")


def spoken_version(text: str, max_words: int = MAX_SPOKEN_WORDS) -> str:
    """The first piece of the answer, split on sentences the way the study tutor splits for its speech
    model: whole sentences up to `max_words`, or the first `max_words` words of one long sentence.
    Record codes in brackets are left out of the spoken version only."""
    words: list[str] = []
    for sentence in _SENTENCE_END.split(" ".join(_RECORD_CODE.sub("", text).split())):
        more = sentence.split()
        if words and len(words) + len(more) > max_words:
            break
        words.extend(more)
        if len(words) >= max_words:
            break
    return " ".join(words[:max_words])


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


def ask_by_voice(chat: Chat, voice: Voice, audio: bytes, content_type: str, user: ResolvedUser,
                 follow_up: FollowUp | None = None) -> VoiceReply:
    """Speech to text, the chat, then text to speech. Never raises for a model failure."""
    refused = check_recording(audio, content_type)
    if refused:
        return VoiceReply(refused[0], refused[1])
    if not chat.model.configured:  # say so before spending a speech-to-text call on it
        return VoiceReply(503, NOT_SET_UP)
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
        reply = VoiceReply(transcript=heard, answer=answer, spoken=spoken_version(answer.text))
        try:
            reply.audio = voice.speak(reply.spoken)
        except VoiceUnavailable as exc:
            log.warning("voice: %s", exc)
            reply.spoken, reply.voice_note = "", CANT_SPEAK
        log.info("voice: %s, %d bytes spoken", answer.kind, len(reply.audio))
        return reply
    finally:
        voice.one_at_a_time.release()
