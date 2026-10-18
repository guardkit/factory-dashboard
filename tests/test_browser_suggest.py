"""A real browser suggests a change (Part F): from the Features page it follows "Suggest a change",
records a voice note that fills the box (Chromium's fake microphone, a stand-in speech-to-text model),
adds to it by typing, sends it, finds it on the Suggestions page with the page it came from, and marks
it done. The words typed include markup, which must arrive as plain text. At phone width the pages
must not scroll sideways.

The board runs on loopback (uvicorn, in a thread). It uses the same tools and rule as
tests/test_browser.py: without them the test fails, unless FACTORY_DASH_ALLOW_NO_BROWSER=1 lets it skip.
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
from backend import suggestions, voice
from backend.app import create_app
from backend.chat.service import Chat

from tests.conftest import TEST_DISPLAY_NAME, TEST_PASSWORD, TEST_USER
from tests.test_browser import TENANTS_TEST, _free_port, _tools
from tests.test_chat import NOW, FakeModel, _ledger
from tests.test_voice import FakeSpeech

TYPED = 'Also <b>bold</b> & "quoted".'


@pytest.fixture
def suggest_board(tmp_path: Path) -> Iterator[tuple[str, Path, FakeModel, FakeSpeech]]:
    store = _ledger(tmp_path)
    model, speech = FakeModel(), FakeSpeech()
    speech.stt = (200, {"text": "Please show the cost on the Now page.", "duration": 1.2})
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
        yield f"http://127.0.0.1:{port}", app.state.suggestions_path, model, speech
    finally:
        server.should_exit = True
        thread.join(timeout=10)


SCRIPT = r"""
const [core, exe, base, user, password, typed] = process.argv.slice(1);
const { chromium } = require(core);
const T = { timeout: 15000 };
const sideways = (page) => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
(async () => {
  const out = {};
  const browser = await chromium.launch({ executablePath: exe, args: [
    '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.grantPermissions(['microphone'], { origin: base });
    const page = await context.newPage();
    await page.goto(base + '/login');
    await page.fill('input[name=username]', user);
    await page.fill('input[name=password]', password);
    await Promise.all([page.waitForURL(base + '/'), page.click('button[type=submit]')]);

    await page.goto(base + '/features?days=30');
    await Promise.all([page.waitForURL(u => u.pathname === '/suggest', T),
                       page.click('.suggest-link a[href^="/suggest"]')]);
    out.title = await page.title();
    out.from = await page.inputValue('input[name=from_page]');
    out.recent = await page.$$eval('.recent-changes .changes__list li', ls => ls.length);

    // A voice note: tap to start, tap again to stop; the words heard go into the box.
    const mic = '.suggest .chat-mic';
    out.micVisible = await page.isVisible(mic);
    const box = await page.locator(mic).boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down(); await page.mouse.up();
    await page.waitForSelector(mic + '.chat-mic--listening', T);
    await page.waitForTimeout(1200);
    await page.mouse.down(); await page.mouse.up();
    await page.waitForFunction(() => document.querySelector('#suggest-text').value.length > 0, null, T);
    out.heard = await page.inputValue('#suggest-text');
    out.noteWords = (await page.textContent('.suggest .chat-voice__text')).trim();
    out.chatLogEntries = await page.$$eval('.chat__log .chat-exchange', es => es.length);

    // Add to it by typing, then send.
    await page.focus('#suggest-text');
    await page.keyboard.press('End');
    await page.keyboard.type(' ' + typed);
    await Promise.all([page.waitForURL(u => u.pathname === '/suggestions', T), page.click('.suggest .chat__ask')]);
    out.sent = (await page.textContent('.suggest__sent')).trim();
    const item = await page.waitForSelector('.suggestion', T);
    out.text = await item.$eval('.suggestion__text', e => e.textContent);
    out.boldElements = await item.$$eval('.suggestion__text b', bs => bs.length);
    out.who = (await item.$eval('.suggestion__who', e => e.textContent)).trim();
    out.fromLink = await item.$eval('.suggestion__from a', a => a.getAttribute('href'));
    out.statusBefore = (await item.$eval('.status-pill', e => e.textContent)).trim();

    // Mark it done.
    await item.$eval('select[name=status]', s => { s.value = 'done'; });
    await Promise.all([page.waitForURL(u => u.pathname === '/suggestions', T),
                       item.$eval('.suggestion__status button', b => b.click())]);
    out.statusAfter = (await page.textContent('.suggestion .status-pill')).trim();

    // Phone width: no sideways scroll on the three new pages.
    await page.setViewportSize({ width: 375, height: 800 });
    out.sideways = {};
    for (const path of ['/suggest', '/suggestions', '/changes']) {
      await page.goto(base + path);
      out.sideways[path] = await sideways(page);
    }
    await context.close();
  } catch (e) {
    out.error = String(e);
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(out));
})();
"""


def test_a_person_suggests_a_change_by_voice_and_text_and_marks_it_done(
        suggest_board: tuple[str, Path, FakeModel, FakeSpeech]) -> None:
    node, core, browser = _tools()
    base, path, model, speech = suggest_board
    done = subprocess.run([node, "-e", SCRIPT, core, browser, base, TEST_USER, TEST_PASSWORD, TYPED],
                          capture_output=True, text=True, timeout=180, check=False)
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("{")]
    assert lines, f"the browser gave no result: {done.stderr}"
    got = json.loads(lines[-1])
    assert "error" not in got, (got.get("error"), got)

    assert got["title"] == "Suggest a change · Appmilla Factory"
    assert got["from"] == "/features?days=30"
    assert got["recent"] == 3
    assert got["micVisible"]
    assert got["heard"] == "Please show the cost on the Now page."
    assert got["noteWords"] == "Your words are in the box. Check them, then press Send."
    assert got["chatLogEntries"] == 0                       # the chat was not asked
    assert speech.paths() == ["/v1/audio/transcriptions"]   # speech to text only, nothing spoken
    assert model.calls == []

    expected = f"Please show the cost on the Now page. {TYPED}"
    assert got["sent"] == "Thank you. Your suggestion is saved."
    assert got["text"] == expected and got["boldElements"] == 0  # markup arrives as plain text
    assert got["who"] == TEST_DISPLAY_NAME
    assert got["fromLink"] == "/features?days=30"
    assert (got["statusBefore"], got["statusAfter"]) == ("New", "Done")
    assert got["sideways"] == {"/suggest": 0, "/suggestions": 0, "/changes": 0}

    (s,) = suggestions.list_all(path)
    assert (s.text, s.from_page, s.status, s.username) == (expected, "/features?days=30", "done", TEST_USER)
