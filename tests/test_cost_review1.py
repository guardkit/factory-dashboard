"""Codex review round 1 of part B (findings R1–R6), checked on rendered pages and in a real browser.

The world is relative to the real clock (so neither feed is stale and nothing is withheld):
- a delivered feature (c-1) with planning, build and merge-check calls, one of them estimated;
- a building feature (c3) with an estimated call and a call on a model with no price;
- an untagged call;
- a tenant registry with the operator and three clients: one owning the project, one owning
  another project, and one with no projects.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
import yaml
from backend import db
from backend.app import create_app
from backend.prices import load_prices
from backend.projector.cost_import import import_once
from backend.readmodel import dbread
from backend.readmodel.costs import CostBook, CostFreshness, Totals
from backend.readmodel.viewmodels import SpendSlot
from starlette.testclient import TestClient

from tests import forge_fake
from tests.conftest import LOOPBACK_URL, TEST_PASSWORD, TEST_USER, login
from tests.forge_fake import REPO, ForgeFake, delivered_feature, store_from
from tests.spend_fake import FakeSpend
from tests.test_browser import _free_port, _tools

PRICE_LABEL = "Nominal: OpenRouter price for qwen/qwen3.8-flash on 17 Oct 2026"
UNPRICED = "1 of these calls was on a model with no price, so it is not counted."


def _registry(path: Path) -> Path:
    rows: list[dict[str, object]] = [{"tenant_slug": "operator", "display_name": "Factory (operator)",
                                      "nats_account": "APPMILLA", "subject_prefix": None, "projects": [],
                                      "store_path": None}]
    for slug, projects in (("acmeco", [REPO]), ("otherco", ["other/app"]), ("emptyco", [])):
        rows.append({"tenant_slug": slug, "display_name": slug.capitalize(), "nats_account": slug.upper(),
                     "subject_prefix": slug, "projects": projects, "store_path": f"ledger_client_{slug}.db"})
    path.write_text(yaml.safe_dump({"tenants": rows}), encoding="utf-8")
    return path


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path, Path]]:
    now = datetime.now(UTC)
    t0 = now - timedelta(hours=3)
    monkeypatch.setattr(forge_fake, "T0", t0)
    fake = ForgeFake(tmp_path / "forge.db")
    delivered_feature(fake, 1, text="Search box that filters notes by title.")
    fake.planning("c3", "Count the notes.")
    fake.build("b3", "FEAT-0003", "c3", status="RUNNING", queued=100, started=100, completed=None)
    path = tmp_path / "readmodel.db"
    conn = store_from(fake, path, now)
    calls = FakeSpend(t0)
    calls.add("p1", 5, "c-1", prompt=10_000, completion=1_000)                   # planning
    calls.add("p2", 10, "c-1", prompt=2_000, completion=200, spend=0.0)          # planning, estimated
    calls.add("b1", 30, "c-1", prompt=100_000, completion=5_000)                 # building
    calls.add("r1", 85, "c-1", prompt=500, completion=50)                        # merge and check
    calls.add("x1", 110, "c3", prompt=40_000, completion=4_000, spend=0.0)       # estimated
    calls.add("x2", 115, "c3", prompt=900, completion=90, provider_model="openai/coach-ft-v4")   # no price
    calls.add("u1", 30, None, prompt=700, completion=70, model_name="tutor-coach")               # untagged
    import_once(conn, calls, now)
    conn.close()
    tenants = _registry(tmp_path / "tenants.yaml")
    app = create_app(db_path=path, tenants_path=tenants)
    with TestClient(app, base_url=LOOPBACK_URL) as client:
        login(client)
        yield client, path, tenants


def _dates() -> tuple[str, str]:
    today = datetime.now(UTC).date()
    return (today - timedelta(days=2)).isoformat(), today.isoformat()


def _window() -> str:
    lo, hi = _dates()
    return f"from={lo}&to={hi}"


def _spend_tile(page: str) -> str:
    """The report's Spend tile, from its opening div to its closing one."""
    for chunk in page.split('<div class="stat-tile')[1:]:
        if 'stat-tile__label">Spend</span>' in chunk:
            return chunk.split("</div>")[0]
    raise AssertionError("no Spend tile")


def _visible(page: str) -> str:
    return html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)))


# --- R1: a client's report never carries the factory's usage --------------------------------------------


def test_only_the_operators_report_shows_every_model_call(world: tuple[TestClient, Path, Path]) -> None:
    client, path, _ = world
    operator = client.get(f"/reports?{_window()}").text
    assert "All model calls:" in operator
    own = client.get(f"/reports?tenant=acmeco&{_window()}").text
    other = client.get(f"/reports?tenant=otherco&{_window()}").text
    empty = client.get(f"/reports?tenant=emptyco&{_window()}").text
    for page in (own, other, empty):
        assert "All model calls" not in page
    # The client that owns the project sees its own tagged calls only; the others see no figure at all.
    conn = db.connect_ro(path)
    try:
        own_spend = dbread.weekly_report(conn, "acmeco", window=_dates()).headline.spend
    finally:
        conn.close()
    assert own_spend.measured and own_spend.value in own
    assert own_spend.calls == 6                         # c-1's four calls and c3's two; never the untagged one
    assert own_spend.value in _spend_tile(own)
    for page in (other, empty):
        assert "$" not in _spend_tile(page)
    assert "No work on record for this." in other and "This covers no projects." in empty


# --- R2: the project page shows its totals and weeks -------------------------------------------------


def test_the_project_page_renders_totals_and_weeks(world: tuple[TestClient, Path, Path]) -> None:
    client, path, _ = world
    view = dbread.project_view(path, REPO)
    body = client.get(f"/projects/{REPO}").text
    assert "Cost and time by week" in body
    table = body[body.index("Cost and time by week"):]
    table = table[:table.index("</table>")]
    total = _visible(table[table.index("In total"):table.index("</tr>", table.index("In total"))])
    assert view.spend.calls == 6 and f" {view.spend.calls} " in total
    assert view.spend.tokens_in_text in total and view.spend.tokens_out_text in total
    assert view.spend.value in total and view.time.elapsed in total and view.time.machine in total
    week = view.weeks[0]
    row_at = table.index(f'<th scope="row">{week.week_label}</th>')
    row = _visible(table[row_at:table.index("</tr>", row_at)])
    assert week.spend.calls_text in row and week.spend.value in row and week.time.waiting in row
    assert week.untagged is not None and week.untagged.calls_text == "1"
    assert len(re.findall(r'<th scope="row">', table)) == 1 + len(view.weeks)
    notes = _visible(body)
    assert view.untagged_note and view.untagged_note in notes
    assert "Counts calls from 17 October, when calls began to be tagged by feature." in notes
    assert PRICE_LABEL in notes


# --- R3: the feature page shows cost by stage ------------------------------------------------------------


def test_the_feature_page_shows_cost_by_stage(world: tuple[TestClient, Path, Path]) -> None:
    client, _path, _ = world
    body = client.get("/features/FEAT-0001").text
    card = body[body.index('id="cost-title"'):]
    card = card[:card.index("</section>")]
    rows = {}
    for label in ("Planning", "Building", "Merge and check"):
        at = card.index(f'<th scope="row">{label}</th>')
        rows[label] = _visible(card[at:card.index("</tr>", at)])
    assert rows["Planning"].split()[1] == "2" and rows["Building"].split()[1] == "1"
    assert rows["Merge and check"].split()[3] == "1"     # "Merge and check 1 …": one call
    assert '<span class="est">' in card[card.index('<th scope="row">Planning</th>'):]   # the estimated call
    assert "nominal" in rows["Building"]


# --- R4: every spend tile carries the dated price source ---------------------------------------------------


def test_spend_tiles_name_the_price_source_and_date(world: tuple[TestClient, Path, Path]) -> None:
    client, _path, _ = world

    def tile_subs(page: str) -> list[str]:
        return [_visible(m) for m in re.findall(r'<div class="stat-tile[^"]*"[^>]*>(.*?)</div>', page, re.S)
                if "stat-tile__sub" in m and ("$" in m or "Spend" in m or "all projects" in m)]

    # Now's "today" tile may hold no calls just after midnight, so one measured tile there is enough.
    for url, needs in (("/", 1), (f"/projects/{REPO}", 1), (f"/reports?{_window()}", 1)):
        subs = [s for s in tile_subs(client.get(url).text) if "$" in s]
        assert len(subs) >= needs, (url, subs)
        assert all(PRICE_LABEL in s for s in subs), (url, subs)


# --- R5: on a phone, the cost line keeps its notes (a real browser at 390 px) --------------------------------

PHONE_SCRIPT = r"""
const [core, exe, base, user, password] = process.argv.slice(1);
const { chromium } = require(core);
(async () => {
  const browser = await chromium.launch({ executablePath: exe });
  try {
    const page = await (await browser.newContext({ viewport: { width: 390, height: 844 } })).newPage();
    await page.goto(base + '/login');
    await page.fill('input[name=username]', user);
    await page.fill('input[name=password]', password);
    await Promise.all([page.waitForURL(base + '/'), page.click('button[type=submit]')]);
    const out = await page.evaluate(() => {
      const row = [...document.querySelectorAll('.build-row')].find(r => r.textContent.includes('Count the notes'));
      const shown = el => !!el && getComputedStyle(el).display !== 'none' && el.getClientRects().length > 0;
      const line = row.querySelector('.build-row__phone-line');
      const cell = row.querySelector('.build-row__cost');
      return { lineShown: shown(line), cellShown: shown(cell), text: line.innerText.replace(/\s+/g, ' ').trim(),
               est: !!line.querySelector('.est') && shown(line.querySelector('.est')),
               estMark: getComputedStyle(line.querySelector('.est'), '::before').content };
    });
    console.log(JSON.stringify(out));
  } catch (e) { console.log(JSON.stringify({ error: String(e) })); } finally { await browser.close(); }
})();
"""


def test_the_phone_cost_line_keeps_the_estimate_and_the_unpriced_note(world: tuple[TestClient, Path, Path]) -> None:
    node, core, browser = _tools()
    _client, path, tenants = world
    app = create_app(db_path=path, tenants_path=tenants)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "the board did not start"
        time.sleep(0.05)
    try:
        out = subprocess.run([node, "-e", PHONE_SCRIPT, core, browser, f"http://127.0.0.1:{port}", TEST_USER,
                              TEST_PASSWORD], capture_output=True, text=True, timeout=120, check=True)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert "error" not in got, got
    assert got["lineShown"] and not got["cellShown"], got        # the phone line replaces the cost column
    assert got["est"] and "≈" in got["estMark"], got             # part estimated, marked as such
    assert "nominal" in got["text"] and UNPRICED in got["text"], got


# --- R6: availability follows pricing coverage, whatever failed ---------------------------------------------


def _slot(t: Totals) -> SpendSlot:
    now = datetime.now(UTC)
    book = CostBook(freshness=CostFreshness(read_at=now, attempted_at=now, error="", earliest=None),
                    prices=load_prices(), now=now)
    return book.slot(t)


def test_a_failed_call_with_tokens_and_no_price_is_not_measured() -> None:
    s = _slot(Totals(calls=1, failed=1, tokens_in=900, tokens_out=90, unpriced=1))
    assert s.state == "not_measured" and s.empty and s.amount_usd is None
    assert UNPRICED in s.reason


def test_a_priced_failure_with_an_unpriced_success_keeps_the_priced_amount() -> None:
    s = _slot(Totals(calls=2, failed=1, tokens_in=1900, tokens_out=190, cost=0.0042, unpriced=1, priced=1))
    assert s.state == "measured" and s.amount_usd == pytest.approx(0.0042) and not s.empty
    assert UNPRICED in s.reason


def test_a_mix_of_priced_and_unpriced_calls_is_measured_and_discloses_the_rest() -> None:
    s = _slot(Totals(calls=5, tokens_in=5000, tokens_out=500, cost=0.0031, unpriced=2, priced=3))
    assert s.state == "measured" and s.amount_usd == pytest.approx(0.0031)
    assert s.reason == "2 of these calls were on a model with no price, so they are not counted."


def test_calls_that_used_no_tokens_cost_nothing_and_say_so() -> None:
    s = _slot(Totals(calls=2, failed=2))
    assert s.state == "measured" and s.value == "$0.00" and s.reason == "None of these calls used any tokens."
    assert load_prices().label == PRICE_LABEL


# --- R7 (round 2): usage is shown apart from price ---------------------------------------------------------

UNPRICED_PROJECT = "acme/unpriced"
NO_PRICE = "openai/coach-ft-v4"


@pytest.fixture
def unpriced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """An entirely unpriced feature in its own project (u1), and a feature whose planning calls are
    priced and whose build calls are not (m1)."""
    now = datetime.now(UTC)
    t0 = now - timedelta(hours=3)
    monkeypatch.setattr(forge_fake, "T0", t0)
    fake = ForgeFake(tmp_path / "forge.db")
    fake.planning("u1", "Nothing here has a price.", state="RUNNING", end=None, repo=UNPRICED_PROJECT)
    fake.planning("m1", "Half priced.")
    fake.build("bm", "FEAT-00M1", "m1", status="RUNNING", completed=None)
    path = tmp_path / "readmodel.db"
    conn = store_from(fake, path, now)
    calls = FakeSpend(t0)
    calls.add("u-a", 5, "u1", prompt=3_000, completion=300, provider_model=NO_PRICE)
    calls.add("u-b", 8, "u1", prompt=2_000, completion=200, provider_model=NO_PRICE)
    calls.add("m-plan", 5, "m1", prompt=10_000, completion=1_000)                       # planning, priced
    calls.add("m-build", 40, "m1", prompt=50_000, completion=5_000, provider_model=NO_PRICE)   # building, no price
    import_once(conn, calls, now)
    conn.close()
    with TestClient(create_app(db_path=path, tenants_path=_registry(tmp_path / "t.yaml")),
                    base_url=LOOPBACK_URL) as client:
        login(client)
        yield client


def test_an_unpriced_feature_still_shows_its_calls_and_tokens(unpriced: TestClient) -> None:
    body = unpriced.get("/features/u1").text
    card = body[body.index('id="cost-title"'):]
    card = _visible(card[:card.index("</section>")])
    assert "2 model calls, 5 k tokens in, 500 tokens out" in card     # the usage is known
    assert "Not measured" in card and "None of these calls has a price." in card   # only the cost is not
    assert "$" not in card
    planning = card[card.index("Planning"):]
    assert planning.split()[1:4] == ["2", "5", "k"]                   # the stage row carries the counts too


def test_an_unpriced_project_still_shows_its_calls_and_tokens(unpriced: TestClient) -> None:
    body = unpriced.get(f"/projects/{UNPRICED_PROJECT}").text
    table = body[body.index("Cost and time by week"):]
    table = table[:table.index("</table>")]
    total = _visible(table[table.index("In total"):table.index("</tr>", table.index("In total"))])
    assert total.split()[2:7] == ["1", "0", "2", "5", "k"]            # 1 feature, 0 merged, 2 calls, 5 k in
    assert "Not measured" in total and "$" not in total


def test_an_unpriced_stage_beside_a_priced_stage(unpriced: TestClient) -> None:
    body = unpriced.get("/features/FEAT-00M1").text
    card = body[body.index('id="cost-title"'):]
    card = card[:card.index("</section>")]

    def row(label: str) -> str:
        at = card.index(f'<th scope="row">{label}</th>')
        return _visible(card[at:card.index("</tr>", at)])

    planning, building = row("Planning"), row("Building")
    assert planning.split()[1] == "1" and "$" in planning and "nominal" in planning
    assert building.split()[1:4] == ["1", "50", "k"] and "Not measured" in building and "$" not in building
    # The feature's own figure is the priced part, and it says the rest is not counted.
    assert UNPRICED in _visible(card)
