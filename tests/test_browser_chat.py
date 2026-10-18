"""A real browser asks the chat (Part D, Codex review): a question that gets a clarifying
question, the reply to it, the final answer with its links, and a failure shown as one sentence.

The board runs on loopback (uvicorn, in a thread) with a stand-in chat model, so no model is
called; a headless Chromium driven by Playwright signs in, opens /chat and types into the page as
a person would, through the page's own htmx forms. It uses the same tools as tests/test_browser.py
and the same rule: without them the test fails, unless FACTORY_DASH_ALLOW_NO_BROWSER=1 lets it skip.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from backend.app import create_app
from backend.chat import llm
from backend.chat.service import Chat

from tests.conftest import TEST_PASSWORD, TEST_USER
from tests.test_browser import TENANTS_TEST, _free_port, _tools
from tests.test_chat import NOW, FakeModel, _ledger


@pytest.fixture
def chat_board(tmp_path: Path) -> Iterator[tuple[str, FakeModel]]:
    store = _ledger(tmp_path)
    model = FakeModel('{"clarify": "which_project"}',
                      '{"query": "project_summary", "args": {"project": "acme/notes"}}',
                      llm.ModelError(llm.UNREACHABLE))
    app = create_app(db_path=store, tenants_path=TENANTS_TEST)
    app.state.chat = Chat(store, model, now=lambda: NOW)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "the board did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}", model
    finally:
        server.should_exit = True
        thread.join(timeout=10)


SCRIPT = r"""
const [core, exe, base, user, password] = process.argv.slice(1);
const { chromium } = require(core);
const T = { timeout: 15000 };
(async () => {
  const out = {};
  const browser = await chromium.launch({ executablePath: exe });
  try {
    const page = await browser.newPage();
    await page.goto(base + '/login');
    await page.fill('input[name=username]', user);
    await page.fill('input[name=password]', password);
    await Promise.all([page.waitForURL(base + '/'), page.click('button[type=submit]')]);
    await page.goto(base + '/chat');

    // A question the model cannot route without knowing the project: it asks which.
    await page.fill('#chat-question', 'How is the project doing?');
    await page.click('.chat__form button[type=submit]');
    const clarify = await page.waitForSelector('#chat-log .chat-exchange--clarify', T);
    out.clarify = await clarify.evaluate(e => e.querySelector('.chat-exchange__text').textContent.trim());
    out.questionBoxCleared = await page.inputValue('#chat-question') === '';

    // The reply goes through the clarifying question's own form.
    await clarify.$eval('input[name=question]', (e, v) => { e.value = v; }, 'acme/notes');
    await clarify.$eval('button[type=submit]', e => e.click());
    const answer = await page.waitForSelector('#chat-log .chat-exchange--answer', T);
    out.answer = await answer.evaluate(e => e.querySelector('.chat-exchange__text').textContent.trim());
    out.links = await answer.$$eval('.chat-refs a', as => as.map(a => ({ href: a.getAttribute('href'),
                                                                      label: a.textContent.trim() })));
    out.replyFormsLeft = await page.$$eval('#chat-log .chat-exchange__reply', fs => fs.length);
    out.exchanges = await page.$$eval('#chat-log .chat-exchange', es => es.length);

    // A failure of the model: one plain sentence in the log.
    await page.fill('#chat-question', 'What was delivered this week?');
    await page.click('.chat__form button[type=submit]');
    const failed = await page.waitForSelector('#chat-log .chat-exchange--unavailable', T);
    out.failure = await failed.evaluate(e => e.querySelector('.chat-exchange__text').textContent.trim());

    // The answer's link leads to the record it rests on.
    await Promise.all([page.waitForURL(u => u.pathname === '/projects/acme/notes', T),
                       answer.$eval('.chat-refs a[href="/projects/acme/notes"]', a => a.click())]);
    out.followed = { path: new URL(page.url()).pathname, heading: await page.evaluate(() =>
      (document.querySelector('h1') || { textContent: '' }).textContent.replace(/\s+/g, ' ').trim()) };
  } catch (e) {
    out.error = String(e);
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(out));
})();
"""


def test_a_person_asks_is_asked_which_replies_and_follows_the_answers_link(
        chat_board: tuple[str, FakeModel]) -> None:
    node, core, browser = _tools()
    base, model = chat_board
    done = subprocess.run([node, "-e", SCRIPT, core, browser, base, TEST_USER, TEST_PASSWORD],
                          capture_output=True, text=True, timeout=120, check=False)
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("{")]
    assert lines, f"the browser gave no result: {done.stderr}"
    got = json.loads(lines[-1])
    assert "error" not in got, (got.get("error"), got)

    assert got["clarify"] == "Which project do you mean?"          # the server's own sentence
    assert got["questionBoxCleared"]
    assert got["answer"].startswith("acme/notes: ")
    assert "merged and checked in the last seven days" in got["answer"]
    assert {"href": "/projects/acme/notes", "label": "acme/notes"} in got["links"]
    assert got["replyFormsLeft"] == 0 and got["exchanges"] == 2     # the reply form went once answered

    assert got["failure"] == llm.UNREACHABLE
    assert len(re.findall(r"[.?!](?:\s|$)", got["failure"])) == 1

    assert got["followed"]["path"] == "/projects/acme/notes"
    assert "acme/notes" in got["followed"]["heading"]

    # Three questions reached the model; the reply carried the first question and the one shown.
    assert len(model.calls) == 3
    second = model.calls[1][0]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
    assert (second[1]["content"], second[2]["content"], second[3]["content"]) == \
        ("How is the project doing?", "Which project do you mean?", "acme/notes")
