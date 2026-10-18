"""frontend/static/mic.js under node, with a stand-in microphone, MediaRecorder, fetch and Audio
(tests/js/mic_harness.js holds the checks): the states it shows, and hiding where it cannot record."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MIC_JS = ROOT / "frontend" / "static" / "mic.js"
HARNESS = Path(__file__).resolve().parent / "js" / "mic_harness.js"
# Set to 1 only where node genuinely cannot be installed; otherwise a missing node fails the test.
ALLOW_NO_NODE = "FACTORY_DASH_ALLOW_NO_NODE"


def _node() -> str:
    found = shutil.which("node") or next(
        (str(p) for p in (Path.home() / ".local" / "bin" / "node",) if p.exists()), None)
    if found:
        return found
    if os.environ.get(ALLOW_NO_NODE) == "1":
        pytest.skip(f"node is not installed and {ALLOW_NO_NODE}=1 allows skipping")
    pytest.fail(f"node is needed to test frontend/static/mic.js; install it or set {ALLOW_NO_NODE}=1")


def test_the_microphone_states_and_where_it_hides() -> None:
    out = subprocess.run([_node(), str(HARNESS), str(MIC_JS)], capture_output=True, text=True, timeout=60,
                         check=False)
    assert out.returncode == 0, out.stderr
    results = json.loads(out.stdout)
    assert results["insecure"] == results["no_recorder"] == results["no_microphone"] == "hidden"
    assert results["hold"] == "listening, thinking, speaking, idle"
    assert results["question.ogg"] == "audio/ogg;codecs=opus" and results["question.mp4"] == "audio/mp4"
    assert results["signed_out"] == results["forbidden"] == results["bad_gateway"] == "error"
    assert results["overlapping_prompts"] == "one recording, both microphones closed"
    assert len(results) == 20



CHAT_JS = ROOT / "frontend" / "static" / "chat.js"


def test_the_chat_clears_its_box_and_answered_reply_forms_from_a_listener() -> None:
    """chat.js replaces the inline after-request attributes: on a successful answer the question box is
    reset and an answered reply form removed; a failed request leaves both as they are."""
    script = (
        f"const m = require({json.dumps(str(CHAT_JS))});"
        "const form = (cls) => ({classList: {contains: (c) => c === cls}, resets: 0, removed: 0,"
        "  reset() { this.resets++; }, remove() { this.removed++; }});"
        "const box = form('chat__form'), reply = form('chat-exchange__reply'), other = form('elsewhere');"
        "m.afterAnswer({target: box, detail: {successful: false}});"
        "m.afterAnswer({target: reply, detail: {successful: false}});"
        "const before = [box.resets, reply.removed];"
        "for (const f of [box, reply, other]) m.afterAnswer({target: f, detail: {successful: true}});"
        "console.log(JSON.stringify({before, box: box.resets, reply: reply.removed,"
        "  other: [other.resets, other.removed]}));"
    )
    out = subprocess.run([_node(), "-e", script], capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(out.stdout) == {"before": [0, 0], "box": 1, "reply": 1, "other": [0, 0]}


def test_no_template_has_an_inline_event_handler() -> None:
    """The redesign's rule: behaviour lives in static scripts, never in on…= or hx-on attributes."""
    for template in (ROOT / "frontend" / "templates").rglob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert not re.search(r"\s(on[a-z]+|hx-on[:\w-]*)\s*=", text), template
