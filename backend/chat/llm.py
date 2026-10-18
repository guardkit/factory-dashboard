"""The chat's one model call: LiteLLM on loopback, model name `dashboard-chat` (design 2026-10-17, req. 9).

The pages make no model calls; only the chat does, through its own LiteLLM name and its own key
(`FACTORY_DASH_CHAT_KEY`, limited to that name), so its use shows in the cost figures under the
tag `x-feature-id: dashboard-chat`. Each call asks for a JSON object, at most 300 output tokens,
and is bounded by whatever is left of the question's 60-second limit: the reply is read as it
arrives, and the call stops when that time runs out or the reply passes 64 KiB, so a model that
trickles bytes cannot hold the chat's one-at-a-time slot for longer. The key is sent only as the
bearer header to LiteLLM, and never logged or shown.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
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
        """The reply's text. Raises ModelError with a plain sentence on any failure, and when the
        reply is still arriving `timeout` seconds after the call began (checked between chunks;
        each wait for a chunk is itself bounded by `timeout`)."""
        body = {
            "model": MODEL,
            "messages": list(messages),
            "max_tokens": MAX_OUTPUT_TOKENS,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self._key.strip()}", "x-feature-id": FEATURE_TAG}
        budget = max(0.1, timeout)
        deadline = self._clock() + budget
        limit = httpx.Timeout(budget, connect=min(5.0, budget))
        raw = bytearray()
        try:
            with (httpx.Client(transport=self._transport, timeout=limit) as client,
                  client.stream("POST", f"{self._base}/v1/chat/completions", json=body, headers=headers) as r):
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
