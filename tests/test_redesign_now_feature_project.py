"""Redesign stage 2a: the Now, Feature and Project pages in the Appmilla Factory look.

The look is checked by eye; these tests pin what the look must never break: the Needs-you band is
part of the page's refreshed content, waits come from the record's own flags, and missing figures
are shown as gaps, never as zeros."""

from __future__ import annotations

import re
from pathlib import Path

from backend.projector.forge_mirror import mirror_once
from backend.projector.store import open_rw
from starlette.testclient import TestClient

from tests.forge_fake import ForgeFake
from tests.test_pages import world  # noqa: F401 (the shared fixture)

World = tuple[TestClient, Path, ForgeFake]


def _main(page: str) -> str:
    return page[page.index('<main id="main"'):page.index("</main>")]


def test_the_needs_you_band_is_first_in_the_refreshed_content(world: World) -> None:  # noqa: F811
    client, _, _ = world
    page = client.get("/").text
    main = _main(page)
    # Inside #main: the skip link lands on it, and a content refresh (which swaps #main) refreshes it.
    assert 'data-panel="p4"' in main
    assert main.index('data-panel="p4"') < main.index('data-panel="p2"') < main.index('data-panel="p3"')
    band = main[main.index('<section class="panel-card needs-you'):main.index('data-panel="p2"')]
    assert 'data-theme="dark"' in band and "panel-card--hero" in band
    assert '<span class="key-figure">1 thing</span> needs you' in band
    assert "needs-you__item panel-card panel-card--bar panel-card--wait" in band
    # Decisions are made in Slack, and there is no address for it: plain words, never a link or button.
    assert "Decide in Slack. This dashboard only shows what is waiting." in band
    assert "<button" not in band and 'href="#"' not in band
    assert page.count("<h1") == 1


def test_the_band_fragment_carries_its_own_look(world: World) -> None:  # noqa: F811
    client, _, _ = world
    fragment = client.get("/fragments/p4").text
    root = fragment[fragment.index("<section"):]
    root = root[:root.index(">")]
    assert 'data-theme="dark"' in root and "panel-card--hero" in root and 'id="panel-p4"' in root


def test_now_lists_work_as_rows_that_link_to_the_feature(world: World) -> None:  # noqa: F811
    client, _, _ = world
    body = client.get("/").text
    in_progress = body[body.index('data-panel="p2"'):body.index('data-panel="p3"')]
    assert 'class="build-list"' in in_progress
    rows = re.findall(r'<a class="build-row" href="([^"]+)">', in_progress)
    assert sorted(rows) == ["/features/FEAT-0003", "/features/FEAT-0004"]
    assert '<span class="feature-id">FEAT-0004</span>' in in_progress   # a code chip, no brackets
    assert "Not measured" in in_progress and "£" not in in_progress


def test_the_rail_shows_spend_as_not_measured(world: World) -> None:  # noqa: F811
    client, _, _ = world
    body = client.get("/").text
    rail = body[body.index('class="home-grid__rail"'):]
    rail = rail[:rail.index("</aside>")]
    assert rail.count("stat-tile stat-tile--empty") == 2 and rail.count("Not measured") == 2
    assert "£" not in rail and 'data-panel="p6"' in rail and 'data-panel="proj"' in rail


def test_the_waiting_tile_follows_the_open_wait(world: World) -> None:  # noqa: F811
    client, _, _ = world
    waiting = client.get("/features/FEAT-0004").text        # waiting for the merge decision
    assert waiting.count("stat-tile--key") == 1 and "Waiting now" in waiting
    assert "wait-row--open" in waiting
    building = client.get("/features/FEAT-0003").text       # nobody waited on
    assert "stat-tile--key" not in building and "Waiting now" not in building
    delivered = client.get("/features/FEAT-0001").text      # every wait closed
    assert "stat-tile--key" not in delivered and "wait-row--open" not in delivered
    assert "Not measured" in delivered and "£" not in delivered


def test_numeric_columns_say_so_in_their_header(world: World) -> None:  # noqa: F811
    client, _, _ = world
    for path, label in (("/", "When"), ("/", "Started"), ("/features/FEAT-0004", "How long"),
                        ("/features/FEAT-0004", "Queued")):
        assert f'<th scope="col" class="num">{label}</th>' in client.get(path).text, (path, label)


def test_the_project_page_shows_counts_and_a_spend_gap(world: World) -> None:  # noqa: F811
    client, _, _ = world
    body = client.get("/projects/acme/notes").text
    tiles = body[body.index('class="stat-grid project-summary"'):]
    tiles = tiles[:tiles.index("</section>")]
    assert tiles.count('<div class="stat-tile') == 4
    assert "Spend to date" in tiles and "Not measured" in tiles and "£" not in tiles
    assert "of 10" not in body and "On plan" not in body


# --- coach round 1 (stage 2a): hostile words stay inert everywhere these pages show them ---------

HOSTILE = "<script>alert(1)</script>"
HOSTILE_ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_hostile_titles_render_inert_on_now_its_sections_and_the_project(world: World) -> None:  # noqa: F811
    client, store, fake = world
    # A waiting feature with a hostile title, so the Needs-you subject is covered too (the building
    # feature FEAT-0003 already carries one, for In progress, activity and planning).
    fake.planning("c9", f"Archive a note {HOSTILE}.")
    fake.build("b9", "FEAT-0009", "c9")
    fake.merge_offer("b9", 95)
    mirror_once(open_rw(store), fake.path)
    for path in ("/", "/fragments/p2", "/fragments/p3", "/fragments/p4", "/fragments/p5", "/projects/acme/notes"):
        body = client.get(path).text
        assert HOSTILE not in body, path
        assert HOSTILE_ESCAPED in body, path
    needs = client.get("/fragments/p4").text
    assert f"Archive a note {HOSTILE_ESCAPED}." in needs


# --- coach round 1 (stage 2a): every colour keeps its word ----------------------------------------


def _blocks(body: str, start: str, end: str) -> list[str]:
    """Each piece of `body` from an occurrence of `start` up to the next `end` after it."""
    out, i = [], body.find(start)
    while i != -1:
        j = body.find(end, i + len(start))
        out.append(body[i:j if j != -1 else len(body)])
        i = body.find(start, i + len(start))
    return out


def test_band_chips_keep_their_words_on_now(world: World) -> None:  # noqa: F811
    client, _, _ = world
    body = client.get("/").text
    items = _blocks(body, '<li class="needs-you__item', "</li>")
    rows = _blocks(body, '<a class="build-row"', "</a>")
    assert items and rows
    for piece in items + rows:
        assert 'class="band-chip__word">' in piece, piece[:120]
        word = re.search(r'class="band-chip__word">([^<]+)<', piece)
        assert word is not None and word.group(1).strip(), piece[:120]


def test_waiting_pills_say_waiting(world: World) -> None:  # noqa: F811
    client, _, _ = world
    body = client.get("/features/FEAT-0004").text           # waiting for the merge decision
    pills = _blocks(body, '<span class="status-pill status-pill--wait">', "</span>")
    words = [re.sub(r"<[^>]+>", "", p).split(" ·")[0].strip() for p in pills]
    assert "Waiting" in words                                # the open issue
    assert "waiting now" in words                            # the open wait's row
    assert "Waiting for the merge decision" in words         # the head


def _not_wrapped(body: str, words: str) -> None:
    i = body.index(words)
    for opening, closing in (("<a ", "</a>"), ("<a>", "</a>"), ("<button", "</button>")):
        assert body.rfind(opening, 0, i) <= body.rfind(closing, 0, i), (opening, words)


def test_decide_in_slack_is_never_a_link_or_a_button(world: World) -> None:  # noqa: F811
    client, _, _ = world
    for path in ("/", "/features/FEAT-0004"):
        body = client.get(path).text
        lines = _blocks(body, '<p class="observes-only">', "</p>")
        assert lines and all("Decide in Slack" in line for line in lines), path
        for line in lines:
            assert "<a" not in line and "<button" not in line, path
        _not_wrapped(body, "Decide in Slack")


def test_waiting_is_purple_in_the_stylesheet() -> None:
    css = (Path(__file__).resolve().parents[1] / "frontend" / "static" / "style.css").read_text(encoding="utf-8")
    for selector in (".journey-station--waiting .journey-station__glyph", ".wait-row--open td"):
        rules = [r for r in re.findall(r"([^{}]+)\{([^}]*)\}", css) if selector in r[0]]
        assert rules, selector
        assert any("--status-wait" in body for _, body in rules), selector
