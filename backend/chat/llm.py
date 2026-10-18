"""The chat's one model call: LiteLLM on loopback, model name `dashboard-chat` (design 2026-10-17, req. 9).

The pages make no model calls; only the chat does, through its own LiteLLM name and its own key
(`FACTORY_DASH_CHAT_KEY`, limited to that name), so its use shows in the cost figures under the
tag `x-feature-id: dashboard-chat`. Each call asks for a JSON object, at most 300 output tokens,
and is bounded by whatever is left of the question's 60-second limit. The request runs in a
worker thread; the asking thread waits for it at most that long and, when the time is up, shuts
the connection's socket so the worker's read fails at once, and answers with the timeout sentence.
So a model that is slow, silent or trickling bytes cannot hold the chat's one-at-a-time slot past
the limit. The reply is also read as it arrives and stopped past 64 KiB. The key is sent only as the
bearer header to LiteLLM, and never logged or shown.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import httpx

MODEL = "dashboard-chat"
FEATURE_TAG = "dashboard-chat"
MAX_OUTPUT_TOKENS = 300
DEFAULT_BASE_URL = "http://127.0.0.1:4000"


class ModelError(Exception):
    """The model could not give a reply. `sentence` says why, in plain words, for the user."""

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence


# Each failure is one plain sentence for the user.
TIMED_OUT = "The factory's model took too long to answer, so I stopped and you can try again in a minute."
UNREACHABLE = "I couldn't reach the factory's model just now, but the pages still show everything."
REFUSED = "The factory's model turned down the chat's request, but the pages still show everything."
UNREADABLE = "The factory's model sent a reply too large to read, but the pages still show everything."

#: The most of a reply the chat reads: a 300-token answer and LiteLLM's wrapper are a few kilobytes.
MAX_REPLY_BYTES = 64 * 1024


@dataclass
class _Call:
    """One model call shared between the asking thread and its worker. The lock decides who goes
    last: once the asker has given up (`abandoned`), the worker stores nothing."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    done: bool = False
    abandoned: bool = False
    text: str = ""
    error: ModelError | None = None
    sockets: list[socket.socket] = field(default_factory=list)

    def finish(self, text: str = "", error: ModelError | None = None) -> None:
        with self.lock:
            if self.abandoned:
                return
            self.done, self.text, self.error = True, text, error

    def trace(self, event: str, info: dict[str, object]) -> None:
        """httpcore's trace hook: keep each new connection's socket, so the asker can shut it."""
        stream = info.get("return_value")
        get = getattr(stream, "get_extra_info", None)
        sock = get("socket") if callable(get) and event.endswith(("connect_tcp.complete", "start_tls.complete")) \
            else None
        if isinstance(sock, socket.socket):
            with self.lock:
                self.sockets.append(sock)
                late = self.abandoned
            if late:
                _shut(sock)

    def give_up(self) -> bool:
        """Called by the asker when its time is up. False when the worker had already finished."""
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
    with contextlib.suppress(OSError):   # already closed
        sock.shutdown(socket.SHUT_RDWR)


class ChatModel(Protocol):
    @property
    def configured(self) -> bool: ...

    def complete(self, messages: Sequence[dict[str, str]], timeout: float) -> str: ...


class LiteLLMChat:
    """POST /v1/chat/completions on LiteLLM. `transport` lets the tests stand in for LiteLLM."""

    def __init__(self, base_url: str | None = None, key: str | None = None, *,
                 transport: httpx.BaseTransport | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._base = (base_url or os.environ.get("FACTORY_LITELLM_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._key = key if key is not None else os.environ.get("FACTORY_DASH_CHAT_KEY", "")
        self._transport = transport
        self._clock = clock

    @property
    def configured(self) -> bool:
        return bool(self._key.strip())

    def complete(self, messages: Sequence[dict[str, str]], timeout: float) -> str:
        """The reply's text, within `timeout` seconds of wall-clock time. Raises ModelError with a
        plain sentence on any failure, and with the timeout sentence when the time runs out (the
        request is then cut off, and its worker never reports a result)."""
        budget = max(0.1, timeout)
        call = _Call()
        worker = threading.Thread(target=self._run, args=(call, messages, budget), name="chat-model-call",
                                  daemon=True)
        worker.start()
        worker.join(budget)
        if call.give_up():
            raise ModelError(TIMED_OUT)
        if call.error is not None:
            raise call.error
        return call.text

    def _run(self, call: _Call, messages: Sequence[dict[str, str]], budget: float) -> None:
        try:
            call.finish(text=self._request(call, messages, budget))
        except ModelError as exc:
            call.finish(error=exc)
        except Exception:   # anything else is the model being unreachable, never a crash
            call.finish(error=ModelError(UNREACHABLE))

    def _request(self, call: _Call, messages: Sequence[dict[str, str]], budget: float) -> str:
        body = {
            "model": MODEL,
            "messages": list(messages),
            "max_tokens": MAX_OUTPUT_TOKENS,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self._key.strip()}", "x-feature-id": FEATURE_TAG}
        deadline = self._clock() + budget
        limit = httpx.Timeout(budget, connect=min(5.0, budget))
        raw = bytearray()
        try:
            with (httpx.Client(transport=self._transport, timeout=limit) as client,
                  client.stream("POST", f"{self._base}/v1/chat/completions", json=body, headers=headers,
                                extensions={"trace": call.trace}) as r):
                if r.status_code in (401, 403):
                    raise ModelError(REFUSED)
                if r.status_code >= 400:
                    raise ModelError(UNREACHABLE)
                for chunk in r.iter_bytes():  # as it arrives, so both limits are checked often
                    raw += chunk
                    if len(raw) > MAX_REPLY_BYTES:
                        raise ModelError(UNREADABLE)
                    if self._clock() > deadline:
                        raise ModelError(TIMED_OUT)
        except httpx.TimeoutException:
            raise ModelError(TIMED_OUT) from None
        except httpx.HTTPError:
            raise ModelError(UNREACHABLE) from None
        if self._clock() > deadline:
            raise ModelError(TIMED_OUT)
        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            return ""
        return content if isinstance(content, str) else ""
