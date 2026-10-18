"""frontend/static/mic.js under node, with a stand-in microphone, MediaRecorder, fetch and Audio
(tests/js/mic_harness.js holds the checks): the states it shows, and hiding where it cannot record."""

from __future__ import annotations

import json
import os
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
    assert results["question.mp4"] == "audio/mp4"           # Safari; Ogg is never chosen
    assert results["ogg_only"] == results["no_format"] == "hidden"
    assert results["signed_out"] == results["forbidden"] == results["bad_gateway"] == "error"
    assert results["overlapping_prompts"] == "one recording, both microphones closed"
    assert results["recorder_throws"] == results["start_throws"] == "error, microphone released"
    assert len(results) == 23
