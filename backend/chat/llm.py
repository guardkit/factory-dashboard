"""The chat's one model call: LiteLLM on loopback, model name `dashboard-chat` (design 2026-10-17, req. 9).

The pages make no model calls; only the chat does, through its own LiteLLM name and its own key
(`FACTORY_DASH_CHAT_KEY`, limited to that name), so its use shows in the cost figures under the
tag `x-feature-id: dashboard-chat`. Each call asks for a JSON object, at most 300 output tokens,
and is bounded by whatever is left of the question's 60-second limit. The key is sent only as the
bearer header to LiteLLM, and never logged or shown.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
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


TIMED_OUT = "The factory's model took too long to answer, so I stopped. Please try again in a minute."
UNREACHABLE = "I couldn't reach the factory's model just now, so I can't answer. The pages still show everything."
REFUSED = "The factory's model refused the chat's request, so I can't answer. The pages still show everything."


class ChatModel(Protocol):
    @property
    def configured(self) -> bool: ...

    def complete(self, messages: Sequence[dict[str, str]], timeout: float) -> str: ...


class LiteLLMChat:
    """POST /v1/chat/completions on LiteLLM. `transport` lets the tests stand in for LiteLLM."""

    def __init__(self, base_url: str | None = None, key: str | None = None, *,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._base = (base_url or os.environ.get("FACTORY_LITELLM_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._key = key if key is not None else os.environ.get("FACTORY_DASH_CHAT_KEY", "")
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._key.strip())

    def complete(self, messages: Sequence[dict[str, str]], timeout: float) -> str:
        """The reply's text. Raises ModelError with a plain sentence on any failure."""
        body = {
            "model": MODEL,
            "messages": list(messages),
            "max_tokens": MAX_OUTPUT_TOKENS,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self._key.strip()}", "x-feature-id": FEATURE_TAG}
        limit = httpx.Timeout(max(0.1, timeout), connect=min(5.0, max(0.1, timeout)))
        try:
            with httpx.Client(transport=self._transport, timeout=limit) as client:
                r = client.post(f"{self._base}/v1/chat/completions", json=body, headers=headers)
        except httpx.TimeoutException:
            raise ModelError(TIMED_OUT) from None
        except httpx.HTTPError:
            raise ModelError(UNREACHABLE) from None
        if r.status_code in (401, 403):
            raise ModelError(REFUSED)
        if r.status_code >= 400:
            raise ModelError(UNREACHABLE)
        try:
            data = r.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            return ""
        return content if isinstance(content, str) else ""
