"""A real browser on an open page: content and its read time stay together (Codex review R2).

The board runs on loopback (uvicorn, in a thread) on a store filled from a fake Forge record, and
a headless Chromium driven by Playwright keeps the Features page and the weekly report open while
this test changes the store underneath them: an idle pass, a finished build, an outage, recovery.
Nothing is reloaded by hand; the pages must follow on their own.

It uses what is already on this machine: Node's playwright-core (FACTORY_DASH_PLAYWRIGHT_CORE, or
the npx cache) and a Chromium build (FACTORY_DASH_BROWSER, or Playwright's browser folder, or
Google Chrome). Without them the test fails, unless FACTORY_DASH_ALLOW_NO_BROWSER=1 lets it skip.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from backend.app import create_app
from backend.projector.forge_mirror import mirror_once
from backend.projector.store import iso, open_rw, write_heartbeat

from tests import forge_fake
from tests.conftest import TEST_PASSWORD, TEST_USER
from tests.forge_fake import ForgeFake, delivered_feature, store_from

TENANTS_TEST = Path(__file__).resolve().parent / "fixtures" / "tenants_test.yaml"
ALLOW_NO_BROWSER = "FACTORY_DASH_ALLOW_NO_BROWSER"
HOME = Path.home()


def _first(*patterns: str) -> str | None:
    for pattern in patterns:
        found = sorted(glob.glob(pattern), reverse=True)
        if found:
            return found[0]
    return None


def _tools() -> tuple[str, str, str]:
    node = shutil.which("node") or _first(str(HOME / ".local" / "bin" / "node"))
    core = os.environ.get("FACTORY_DASH_PLAYWRIGHT_CORE") or _first(
        str(HOME / ".npm" / "_npx" / "*" / "node_modules" / "playwright-core"))
    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(HOME / ".cache" / "ms-playwright")
    browser = os.environ.get("FACTORY_DASH_BROWSER") or _first(
        f"{browsers}/chromium_headless_shell-*/chrome-linux/headless_shell",
        f"{browsers}/chromium-*/chrome-linux/chrome", "/usr/bin/google-chrome")
    if node and core and browser:
        return node, core, browser
    missing = ", ".join(n for n, v in (("node", node), ("playwright-core", core), ("a Chromium", browser)) if not v)
    if os.environ.get(ALLOW_NO_BROWSER) == "1":
        pytest.skip(f"{missing} not found and {ALLOW_NO_BROWSER}=1 allows skipping")
    pytest.fail(f"{missing} not found: needed for the real-browser test (or set {ALLOW_NO_BROWSER}=1)")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Path, ForgeFake]]:
    monkeypatch.setattr(forge_fake, "T0", datetime.now(UTC) - timedelta(hours=3))
    fake = ForgeFake(tmp_path / "forge.db")
    delivered_feature(fake, 1, text="Search box that filters notes by title.")
    fake.planning("c3", "Count the notes.")
    fake.build("b3", "FEAT-0003", "c3", status="RUNNING", queued=100, started=100, completed=None)
    store = tmp_path / "readmodel.db"
    store_from(fake, store).close()
    app = create_app(db_path=store, tenants_path=TENANTS_TEST, freshness_poll_secs=1)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "the board did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}", store, fake
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# The browser side: sign in, open Features and the weekly report, then report what each page shows
# after each change this test makes (one JSON line per step; the test answers "go" on stdin).
SCRIPT = r"""
const [core, exe, base, user, password] = process.argv.slice(1);
const { chromium } = require(core);
const lines = []; let waiter = null;
require('readline').createInterface({ input: process.stdin }).on('line', l => {
  if (waiter) { const w = waiter; waiter = null; w(l); } else lines.push(l);
});
const next = () => new Promise(r => lines.length ? r(lines.shift()) : (waiter = r));
const say = o => console.log(JSON.stringify(o));
const T = { timeout: 20000 };
(async () => {
  const browser = await chromium.launch({ executablePath: exe });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(base + '/login');
    await page.fill('input[name=username]', user);
    await page.fill('input[name=password]', password);
    await Promise.all([page.waitForURL(base + '/'), page.click('button[type=submit]')]);
    await page.goto(base + '/features');
    const report = await context.newPage();
    await report.goto(base + '/reports');
    const look = p => p.evaluate(() => {
      const row = [...document.querySelectorAll('#main tr')].find(t => t.textContent.includes('Count the notes'));
      const chip = document.querySelector('#freshness .asof-chip');
      return {
        row: row ? row.textContent.replace(/\s+/g, ' ').trim() : '',
        asof: chip ? parseFloat(chip.dataset.asof) : null,
        version: document.getElementById('freshness').dataset.version,
        banner: document.querySelectorAll('.banner--stale').length,
        lagging: document.querySelectorAll('#main .state-lagging').length,
        withheld: document.querySelectorAll('#main .report-withheld').length,
        mainMark: document.getElementById('main').dataset.mark || '',
        drawerMark: (document.querySelector('.chat-drawer') || {dataset: {}}).dataset.mark || '',
      };
    });
    const asofAbove = (p, a) => p.waitForFunction(
      a => parseFloat(document.querySelector('#freshness .asof-chip').dataset.asof) > a, a, T);
    for (const p of [page, report]) {
      await p.evaluate(() => {
        document.getElementById('main').dataset.mark = 'first';
        document.querySelector('.chat-drawer').dataset.mark = 'kept';
      });
    }
    const opened = await look(page);
    say({ step: 'opened', features: opened, report: await look(report) }); await next();

    await asofAbove(page, opened.asof);                        // an idle pass: only the time moves on
    say({ step: 'idle', features: await look(page) }); await next();

    await page.waitForFunction(() => [...document.querySelectorAll('#main tr')]
      .some(t => t.textContent.includes('Count the notes') && t.textContent.includes('Waiting for the merge decision')),
      null, T);                                                // the build finished
    say({ step: 'changed', features: await look(page) }); await next();

    await page.waitForSelector('.banner--stale', T);           // an outage
    await report.waitForSelector('#main .report-withheld', T);
    await page.waitForSelector('#main .state-lagging', T);
    say({ step: 'outage', features: await look(page), report: await look(report) }); await next();

    await page.waitForSelector('.banner--stale', { state: 'detached', ...T });   // recovery
    await page.waitForSelector('#main .state-lagging', { state: 'detached', ...T });
    await report.waitForSelector('#main .report-withheld', { state: 'detached', ...T });
    say({ step: 'recovered', features: await look(page), report: await look(report) });
  } catch (e) {
    say({ step: 'error', error: String(e) });
  } finally {
    await browser.close();
  }
})();
"""


def test_an_open_page_follows_the_record_through_change_outage_and_recovery(  # noqa: PLR0915 (one story)
    board: tuple[str, Path, ForgeFake],
) -> None:
    node, core, browser = _tools()
    base, store, fake = board
    conn = open_rw(store)
    proc = subprocess.Popen([node, "-e", SCRIPT, core, browser, base, TEST_USER, TEST_PASSWORD],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    watchdog = threading.Timer(180, proc.kill)
    watchdog.start()
    assert proc.stdin is not None and proc.stdout is not None

    def step(name: str) -> dict[str, dict[str, object]]:
        line = proc.stdout.readline()
        assert line, f"the browser stopped before '{name}': {proc.stderr.read() if proc.stderr else ''}"
        got = json.loads(line)
        assert got["step"] == name, got
        return got

    def go() -> None:
        proc.stdin.write("go\n")
        proc.stdin.flush()

    try:
        opened = step("opened")
        features, report = opened["features"], opened["report"]
        assert "Building" in str(features["row"]) and features["banner"] == 0 and features["lagging"] == 0
        assert report["withheld"] == 0

        # Healthy and idle: a pass that finds nothing new. The read time moves on; the content is
        # not fetched again (same data version), so it is the same element as before.
        write_heartbeat(conn, datetime.now(UTC))
        assert mirror_once(conn, fake.path) is False
        go()
        idle = step("idle")["features"]
        assert float(str(idle["asof"])) > float(str(features["asof"])) and idle["banner"] == 0
        if idle["version"] == features["version"]:   # (unless the five-minute tick fell in between)
            assert idle["mainMark"] == "first"

        # The build finishes while the page is open: the row and the read time change together.
        fake.conn.execute("UPDATE builds SET status='COMPLETE', completed_at=? WHERE build_id='b3'",
                          (forge_fake.at(150),))
        fake.merge_offer("b3", 151)
        write_heartbeat(conn, datetime.now(UTC))
        assert mirror_once(conn, fake.path) is True
        go()
        changed = step("changed")["features"]
        assert "Waiting for the merge decision" in str(changed["row"])
        assert float(str(changed["asof"])) > float(str(idle["asof"]))
        assert changed["version"] != idle["version"] and changed["mainMark"] == ""   # content fetched again
        assert changed["drawerMark"] == "kept"                                       # the chat drawer was not

        # An outage: the feed runs but cannot read the record. Banner, lagging cards and the
        # withheld report figures all appear on the open pages.
        write_heartbeat(conn, datetime.now(UTC))
        conn.execute("UPDATE mirror_status SET last_success_at=? WHERE source='forge'",
                     (iso(datetime.now(UTC) - timedelta(minutes=10)),))
        go()
        outage = step("outage")
        assert outage["features"]["banner"] == 1 and int(str(outage["features"]["lagging"])) >= 1
        assert int(str(outage["report"]["withheld"])) >= 1

        # Recovery: the next good pass. Banner gone, cards live, report figures back, no reload.
        write_heartbeat(conn, datetime.now(UTC))
        mirror_once(conn, fake.path)
        go()
        recovered = step("recovered")
        features, report = recovered["features"], recovered["report"]
        assert features["banner"] == 0 and features["lagging"] == 0 and report["withheld"] == 0
        assert "Waiting for the merge decision" in str(features["row"])
        assert float(str(features["asof"])) > float(str(outage["features"]["asof"]))
        assert features["drawerMark"] == "kept" and report["drawerMark"] == "kept"
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        conn.close()
