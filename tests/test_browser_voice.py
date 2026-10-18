"""A real browser asks by voice (Part E): headless Chromium with its fake microphone records a
question on /chat at phone-to-laptop width (720-1599 px, where the floating link opens /chat) and in
the side drawer from 1600 px, sends it to /api/voice/ask, and shows and plays the answer.

The board runs on loopback (uvicorn, in a thread) with a stand-in chat model and stand-in speech
models, so no model is called. Chromium's fake device stands in for the microphone, and its fake
permission prompt says yes. It uses the same tools and rule as tests/test_browser.py: without them the
test fails, unless FACTORY_DASH_ALLOW_NO_BROWSER=1 lets it skip.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from backend import voice
from backend.app import create_app
from backend.chat.service import Chat

from tests.conftest import TEST_PASSWORD, TEST_USER
from tests.test_browser import TENANTS_TEST, _free_port, _tools
from tests.test_chat import NOW, FakeModel, _ledger
from tests.test_voice import FakeSpeech, wav


@pytest.fixture
def voice_board(tmp_path: Path) -> Iterator[tuple[str, FakeModel, FakeSpeech]]:
    store = _ledger(tmp_path)
    model = FakeModel('{"query": "open_issues"}', '{"query": "open_issues"}')
    speech = FakeSpeech()
    speech.stt = (200, {"text": "Any issues?"})
    speech.tts = (200, wav(1))                       # a real (silent) WAV the browser can play
    app = create_app(db_path=store, tenants_path=TENANTS_TEST)
    app.state.chat = Chat(store, model, now=lambda: NOW)
    app.state.voice = voice.Voice(voice.VoiceSettings(), transport=httpx.MockTransport(speech.handler))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "the board did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}", model, speech
    finally:
        server.should_exit = True
        thread.join(timeout=10)


SCRIPT = r"""
const [core, exe, base, user, password] = process.argv.slice(1);
const { chromium } = require(core);
const T = { timeout: 15000 };
async function ask(page, panel, how) {
  const mic = panel + ' .chat-mic';
  const out = { visible: await page.isVisible(mic) };
  await page.locator(mic).scrollIntoViewIfNeeded();
  const box = await page.locator(mic).boundingBox();
  out.hit = await page.evaluate(([x, y]) => { const e = document.elementFromPoint(x, y);
    return e ? (e.closest('.chat-mic') ? 'mic' : e.tagName + '.' + e.className) : 'none'; },
    [box.x + box.width / 2, box.y + box.height / 2]);
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  if (how === 'hold') {
    await page.mouse.down();
    await page.waitForSelector(mic + '.chat-mic--listening', T);
    out.listeningWords = (await page.textContent(panel + ' .chat-voice__text')).trim();
    await page.waitForTimeout(1200);
    await page.mouse.up();
  } else {
    await page.mouse.down(); await page.mouse.up();          // a tap starts
    await page.waitForSelector(mic + '.chat-mic--listening', T);
    await page.waitForTimeout(1200);
    await page.mouse.down(); await page.mouse.up();          // a second tap sends
  }
  const ex = await page.waitForSelector(panel + ' .chat-exchange', T);
  out.question = (await ex.$eval('.chat-exchange__question', e => e.textContent)).replace(/\s+/g, ' ').trim();
  out.answer = (await ex.$eval('.chat-exchange__text', e => e.textContent)).trim();
  await page.waitForFunction(p => {
    const h = document.querySelector(p + ' .chat-exchange__how');
    return h && h.textContent.trim() === 'Read aloud';
  }, panel, T);
  await page.waitForFunction(m => !document.querySelector(m).className.includes('--'), mic, T);
  out.how = (await ex.$eval('.chat-exchange__how', e => e.textContent)).trim();
  out.endWords = (await page.textContent(panel + ' .chat-voice__text')).trim();
  return out;
}
(async () => {
  const out = {};
  const browser = await chromium.launch({ executablePath: exe, args: [
    '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
    '--autoplay-policy=no-user-gesture-required'] });
  try {
    for (const [name, width, path, panel, how] of [['chatPage', 1000, '/chat', 'main .chat', 'hold'],
                                                   ['drawer', 1700, '/', '.chat-drawer .chat', 'tap']]) {
      const context = await browser.newContext({ viewport: { width, height: 1000 } });
      await context.grantPermissions(['microphone'], { origin: base });
      const page = await context.newPage();
      await page.goto(base + '/login');
      await page.fill('input[name=username]', user);
      await page.fill('input[name=password]', password);
      await Promise.all([page.waitForURL(base + '/'), page.click('button[type=submit]')]);
      await page.goto(base + path);
      out[name] = { drawerShown: await page.isVisible('.chat-drawer'),
                    panels: await page.$$eval('.chat', p => p.length) };
      Object.assign(out[name], await ask(page, panel, how));
      await context.close();
    }
  } catch (e) {
    out.error = String(e);
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(out));
})();
"""


def test_a_person_asks_by_voice_on_the_chat_page_and_in_the_drawer(
        voice_board: tuple[str, FakeModel, FakeSpeech]) -> None:
    node, core, browser = _tools()
    base, model, speech = voice_board
    done = subprocess.run([node, "-e", SCRIPT, core, browser, base, TEST_USER, TEST_PASSWORD],
                          capture_output=True, text=True, timeout=180, check=False)
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("{")]
    assert lines, f"the browser gave no result: {done.stderr}"
    got = json.loads(lines[-1])
    assert "error" not in got, (got.get("error"), got)

    page, drawer = got["chatPage"], got["drawer"]
    assert page["drawerShown"] is False and drawer["drawerShown"] is True
    for where in (page, drawer):
        assert where["visible"] and where["hit"] == "mic", where  # shown, and nothing covers it
        assert "Any issues?" in where["question"]                # the transcript, in the question bubble
        assert "open issue" in where["answer"]
        assert where["how"] == "Read aloud"
        assert where["endWords"].startswith("Hold the microphone")
    assert page["listeningWords"].startswith("Listening")

    # Each recording reached speech-to-text as WebM/Opus, then the chat, then text-to-speech.
    stt = [r for r in speech.requests if r.url.path.endswith("/transcriptions")]
    assert len(stt) == 2 and all(b'filename="question.webm"' in r.content for r in stt)
    assert all(b"Content-Type: audio/webm" in r.content for r in stt)
    assert len(model.calls) == 2
