"""A real browser answers a merge card (design of 19 October 2026, "Tests").

The board runs on loopback on a store a stand-in feed keeps reading from a fake Forge record, with a
fake bus that records what it is sent. A headless Chromium signs in as an operator with an approver
identity, opens the feature page and presses "Merge and deploy". The page must say "Sent. Waiting
for the factory to record it", the fake bus must hold exactly the merge card's answer, and when the
test then writes the decision into the fake record (as Forge would), the open page must show the
recorded answer on its own, through the existing freshness poll, without a reload.

The browser tools are found as in tests/test_browser.py (FACTORY_DASH_ALLOW_NO_BROWSER=1 lets the
test skip where there are none).
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import uvicorn
from backend.app import create_app

from tests.conftest import TEST_PASSWORD, TEST_USER, account_row, write_accounts
from tests.forge_fake import ForgeFake, store_from
from tests.test_approvals import APPROVER, OFFER_SUBJECT, FakeBus, offer
from tests.test_browser import TENANTS_TEST, Feed, _free_port, _tools, secs


@pytest.fixture
def board(tmp_path: Path, config_dir: Path) -> Iterator[tuple[str, FakeBus, ForgeFake]]:
    write_accounts(config_dir, [{**account_row(), "approver_identity": APPROVER}])
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("c1", "Search box that filters notes by title.")
    fake.build("b1", "FEAT-0001", "c1")
    fake.stage("b1", "the merge word", "merge_deploy_offer", "GATED", 81, {"merge_offer": offer()})
    store = tmp_path / "readmodel.db"
    store_from(fake, store).close()
    feed = Feed(store, fake.path)
    feed.thread.start()
    bus = FakeBus()
    app = create_app(db_path=store, tenants_path=TENANTS_TEST, freshness_poll_secs=round(secs(30)), answer_bus=bus)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=_free_port(), log_level="warning"))
    port = server.config.port
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "the board did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}", bus, fake
    finally:
        server.should_exit = True
        feed.stop.set()
        thread.join(timeout=10)
        feed.thread.join(timeout=10)


SCRIPT = r"""
const [core, exe, base, user, password, timeoutMs] = process.argv.slice(1);
const { chromium } = require(core);
const lines = []; let waiter = null;
require('readline').createInterface({ input: process.stdin }).on('line', l => {
  if (waiter) { const w = waiter; waiter = null; w(l); } else lines.push(l);
});
const next = () => new Promise(r => lines.length ? r(lines.shift()) : (waiter = r));
const say = o => console.log(JSON.stringify(o));
const T = { timeout: Number(timeoutMs) };
(async () => {
  const browser = await chromium.launch({ executablePath: exe });
  try {
    const page = await (await browser.newContext()).newPage();
    await page.goto(base + '/login');
    await page.fill('input[name=username]', user);
    await page.fill('input[name=password]', password);
    await Promise.all([page.waitForURL(base + '/'), page.click('button[type=submit]')]);
    await page.goto(base + '/features/FEAT-0001');
    const card = '#card-merge-b1';
    const look = () => page.evaluate(sel => {
      const c = document.querySelector(sel);
      return {
        text: c ? c.textContent.replace(/\s+/g, ' ').trim() : '',
        buttons: c ? [...c.querySelectorAll('button')].map(b => b.textContent.trim()) : [],
        mark: document.getElementById('main').dataset.mark || '',
        url: location.pathname,
      };
    }, card);
    say({ step: 'opened', card: await look() }); await next();
    await Promise.all([page.waitForNavigation(), page.click(card + ' button[value=merge]')]);
    await page.waitForSelector(card + ' .answer-card__state', T);
    await page.evaluate(() => { document.getElementById('main').dataset.mark = 'sent'; });
    say({ step: 'sent', card: await look() }); await next();
    await page.waitForFunction(sel => {
      const c = document.querySelector(sel);
      return c && c.textContent.includes('approved the merge');
    }, card, T);
    say({ step: 'recorded', card: await look() });
  } catch (e) {
    say({ step: 'error', error: String(e) });
  } finally {
    await browser.close();
  }
})();
"""


def test_a_browser_answers_a_merge_card_and_sees_the_recorded_answer(
    board: tuple[str, FakeBus, ForgeFake],
) -> None:
    node, core, browser = _tools()
    base, bus, fake = board
    proc = subprocess.Popen([node, "-e", SCRIPT, core, browser, base, TEST_USER, TEST_PASSWORD,
                             str(round((secs(200) + 20) * 1000))],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    watchdog = threading.Timer(180, proc.kill)
    watchdog.start()
    assert proc.stdin is not None and proc.stdout is not None
    stdin, stdout = proc.stdin, proc.stdout

    def step(name: str) -> dict[str, Any]:
        line = stdout.readline()
        assert line, f"the browser stopped before '{name}': {proc.stderr.read() if proc.stderr else ''}"
        got = json.loads(line)
        assert got["step"] == name, got
        return cast("dict[str, Any]", got["card"])

    def go() -> None:
        stdin.write("go\n")
        stdin.flush()

    try:
        opened = step("opened")
        assert opened["buttons"] == ["Merge and deploy", "Decline", "Rebuild the preview"], opened
        assert bus.sent == []
        go()

        sent = step("sent")
        assert "Sent. Waiting for the factory to record it." in sent["text"], sent
        assert sent["buttons"] == [] and sent["url"] == "/features/FEAT-0001"
        assert len(bus.sent) == 1
        subject, env = bus.sent[0]
        assert subject == OFFER_SUBJECT + ".response"
        assert env["correlation_id"] == "c1" and env["source_id"] == "dashboard"
        assert env["payload"]["request_id"] == "merge-b1" and env["payload"]["decision"] == "approve"
        assert env["payload"]["decided_by"] == APPROVER

        # Forge records the decision; the feed copies it; the open page follows on its own.
        fake.stage("b1", "the merge word", "merge_deploy_decision", "PASSED", 85,
                   {"merge_decision": {"decision": "approve", "decided_by": APPROVER, "request_id": "merge-b1"}})
        go()
        recorded = step("recorded")
        assert "Test Person approved the merge" in recorded["text"], recorded
        assert "Sent. Waiting" not in recorded["text"] and recorded["buttons"] == []
        assert recorded["mark"] == ""                       # the content was fetched again, not reloaded by hand
        assert len(bus.sent) == 1
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
