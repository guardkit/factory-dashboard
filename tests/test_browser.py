"""A real browser on an open page: content and its read times stay together (Codex review R2).

The board runs on loopback (uvicorn, in a thread) on a store that a stand-in feed keeps reading
from a fake Forge record, and a headless Chromium driven by Playwright keeps the Features page and
the weekly report open while this test changes things underneath them: more than two minutes of
healthy, unchanged reads, a finished build, an outage, and recovery. Nothing is reloaded by hand;
the pages must follow on their own, with the read time at the top and on every card agreeing.

Time is scaled to a tenth by default (poll 3 s, reads every 1.5 s, cards amber after 6 s and red
after 12 s, a 16 s idle spell, a 12 s outage), so the test takes about a minute. Every interval
and threshold the pages use is scaled by the same factor, so the order of events is the same as
at full scale; the only fixed time is freshness.js's own 5-second tick, which is coarser relative
to the scaled thresholds, so the scaled run is the harder one. FACTORY_DASH_BROWSER_REAL_TIME=1
runs it at the real thresholds (about six minutes). The five-minute version tick is switched off
here, so it cannot rescue the cards: they must be kept level by the unchanged polls alone.

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
from backend.projector.store import open_rw, write_heartbeat
from backend.readmodel import dbread
from backend.readmodel import viewmodels as vm

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


REAL_TIME = os.environ.get("FACTORY_DASH_BROWSER_REAL_TIME") == "1"
SCALE = 1.0 if REAL_TIME else 0.1


def secs(real: float) -> float:
    return real * SCALE


class Feed:
    """A stand-in for the dashboard's feed: a heartbeat and a read of Forge's record every 15 s
    (scaled). `path` can be pointed at a missing file to make the reads fail."""

    def __init__(self, store: Path, path: Path) -> None:
        self.store, self.path = store, path
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        conn = open_rw(self.store)
        try:
            while not self.stop.is_set():
                now = datetime.now(UTC)
                write_heartbeat(conn, now)
                mirror_once(conn, self.path, now)
                self.stop.wait(secs(15))
        finally:
            conn.close()


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Feed, ForgeFake]]:
    monkeypatch.setattr(forge_fake, "T0", datetime.now(UTC) - timedelta(hours=3))
    monkeypatch.setattr(vm, "MIRROR_FRESH_SECS", round(secs(60)))
    monkeypatch.setattr(vm, "STALE_AFTER_SECS", round(secs(120)))
    monkeypatch.setattr(vm, "PROJECTOR_STALE_SECS", round(secs(60)))
    monkeypatch.setattr(dbread, "VERSION_TICK_SECS", 10**9)
    fake = ForgeFake(tmp_path / "forge.db")
    delivered_feature(fake, 1, text="Search box that filters notes by title.")
    fake.planning("c3", "Count the notes.")
    fake.build("b3", "FEAT-0003", "c3", status="RUNNING", queued=100, started=100, completed=None)
    store = tmp_path / "readmodel.db"
    store_from(fake, store).close()
    feed = Feed(store, fake.path)
    feed.thread.start()
    app = create_app(db_path=store, tenants_path=TENANTS_TEST, freshness_poll_secs=round(secs(30)))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "the board did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}", feed, fake
    finally:
        server.should_exit = True
        feed.stop.set()
        thread.join(timeout=10)
        feed.thread.join(timeout=10)


# The browser side: sign in, open Features and the weekly report, then report what each page shows
# after each change this test makes (one JSON line per step; the test answers "go" on stdin).
SCRIPT = r"""
const [core, exe, base, user, password, idleMs, timeoutMs] = process.argv.slice(1);
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
      const top = document.querySelector('#freshness .asof-chip');
      const state = c => ['live', 'lagging', 'stale'].find(s => c.classList.contains('asof-chip--' + s)) || '';
      const cards = [...document.querySelectorAll('#main .asof-chip[data-source="factory"]')];
      return {
        row: row ? row.textContent.replace(/\s+/g, ' ').trim() : '',
        asof: top ? parseFloat(top.dataset.asof) : null,
        top: top ? state(top) : '',
        cards: cards.map(state),
        cardAsof: cards.map(c => parseFloat(c.dataset.asof)),
        cardLabels: cards.map(c => c.querySelector('.asof-chip__label').textContent.trim()),
        topLabel: top ? top.querySelector('.asof-chip__label').textContent.trim() : '',
        version: document.getElementById('freshness').dataset.version,
        banner: document.querySelectorAll('.banner--stale').length,
        lagging: document.querySelectorAll('#main .state-lagging').length,
        withheld: document.querySelectorAll('#main .report-withheld').length,
        mainMark: document.getElementById('main').dataset.mark || '',
        drawerMark: (document.querySelector('.chat-drawer') || {dataset: {}}).dataset.mark || '',
      };
    });
    for (const p of [page, report]) {
      await p.evaluate(() => {
        document.getElementById('main').dataset.mark = 'first';
        document.querySelector('.chat-drawer').dataset.mark = 'kept';
      });
    }
    say({ step: 'opened', features: await look(page), report: await look(report) }); await next();

    // More than two minutes (scaled) of healthy, unchanged reads.
    await page.waitForTimeout(Number(idleMs));
    say({ step: 'idle', features: await look(page), report: await look(report) }); await next();

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


def _level(view: dict[str, object]) -> None:
    """The cards show the factory record's read time of the top: live, and the same time."""
    cards = view["cards"]
    assert isinstance(cards, list) and cards, view
    assert view["top"] == "live" and set(cards) == {"live"}, view
    assert set(view["cardLabels"]) == {view["topLabel"]}, view  # type: ignore[arg-type]


def test_an_open_page_follows_the_record_through_change_outage_and_recovery(  # noqa: PLR0915 (one story)
    board: tuple[str, Feed, ForgeFake],
) -> None:
    node, core, browser = _tools()
    base, feed, fake = board
    idle_ms, timeout_ms = round(secs(160) * 1000), round((secs(200) + 10) * 1000)
    proc = subprocess.Popen(
        [node, "-e", SCRIPT, core, browser, base, TEST_USER, TEST_PASSWORD, str(idle_ms), str(timeout_ms)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    watchdog = threading.Timer(secs(900) + 120, proc.kill)
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
        go()

        # Healthy and idle for more than twice the card threshold: nothing changed, so the content
        # is not fetched again (same element), yet the top time and every card time moved on together
        # and every card is still live.
        idle = step("idle")
        for name in ("features", "report"):
            view = idle[name]
            _level(view)                     # top and cards live, showing the same read time
            assert view["mainMark"] == "first" and view["version"] == opened[name]["version"], name
            assert float(str(view["asof"])) > float(str(opened[name]["asof"])) + secs(60), name
            assert min(view["cardAsof"]) > float(str(opened[name]["asof"])) + secs(60), name  # type: ignore[call-overload]
            assert view["banner"] == 0

        # The build finishes while the page is open: the row and the read times change together.
        fake.conn.execute("UPDATE builds SET status='COMPLETE', completed_at=? WHERE build_id='b3'",
                          (forge_fake.at(150),))
        fake.merge_offer("b3", 151)
        go()
        changed = step("changed")["features"]
        assert "Waiting for the merge decision" in str(changed["row"])
        assert float(str(changed["asof"])) > float(str(idle["features"]["asof"]))
        assert changed["version"] != idle["features"]["version"] and changed["mainMark"] == ""  # fetched again
        assert changed["drawerMark"] == "kept"                                                # the chat drawer not
        _level(changed)

        # An outage: the feed runs but cannot read the record. Banner, lagging cards, honest card
        # times (they stop moving and age) and the withheld report figures appear on the open pages.
        feed.path = fake.path.parent / "gone.db"
        go()
        outage = step("outage")
        assert outage["features"]["banner"] == 1 and int(str(outage["features"]["lagging"])) >= 1
        assert int(str(outage["report"]["withheld"])) >= 1
        assert "live" not in set(outage["features"]["cards"])  # type: ignore[arg-type]

        # Recovery: reads work again. Banner gone, cards live, report figures back, no reload.
        feed.path = fake.path
        go()
        recovered = step("recovered")
        features, report = recovered["features"], recovered["report"]
        assert features["banner"] == 0 and features["lagging"] == 0 and report["withheld"] == 0
        assert "Waiting for the merge decision" in str(features["row"])
        assert float(str(features["asof"])) > float(str(outage["features"]["asof"]))
        assert features["drawerMark"] == "kept" and report["drawerMark"] == "kept"
        _level(features)
        _level(report)
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
