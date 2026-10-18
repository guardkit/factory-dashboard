"""Redesign stage 3: Features, Projects, Delivered, Issues, the not-found page, and the chat's look.

The look is checked by eye; these tests pin what it must keep: answer-style heads whose counts are
the rows the page shows, the live Delivered section carrying its own look, issue kinds as pills in
the right colour, closed issues dimmed, honest gaps instead of figures, and every chat and
microphone class name the chat's markup and script use present in the stylesheet."""

from __future__ import annotations

import html
import re
from pathlib import Path

from backend.projector.forge_mirror import mirror_once
from backend.projector.store import open_rw
from starlette.testclient import TestClient

from tests.forge_fake import ForgeFake
from tests.test_pages import world  # noqa: F401 (the shared fixture)

World = tuple[TestClient, Path, ForgeFake]
STYLE = (Path(__file__).resolve().parent.parent / "frontend" / "static" / "style.css").read_text(encoding="utf-8")


def _main(page: str) -> str:
    return page[page.index('<main id="main"'):page.index("</main>")]


def _head(page: str) -> str:
    main = _main(page)
    return main[main.index('<header class="page-head'):main.index("</header>")]


def test_features_head_counts_the_rows_it_shows(world: World) -> None:  # noqa: F811
    client, _, _ = world
    for path in ("/features", "/features?days=all"):
        page = client.get(path).text
        rows = page.count('<tr class="feature-list__row')
        head = _head(page)
        assert "All projects · Features" in head
        assert f'<h1 class="page-title">{rows} feature{"" if rows == 1 else "s"}</h1>' in head
        assert '<nav class="window-links" aria-label="Period">' in head  # the period switch sits in the head
        assert 'class="panel-card panel-card--flush features' in page and "asof-chip" in page
        assert page.count(">Not measured</span>") == rows  # cost: an honest gap in every row, never a figure


def test_projects_head_counts_projects_and_keeps_the_figures_plain(world: World) -> None:  # noqa: F811
    client, _, _ = world
    page = client.get("/projects").text
    rows = page.count('<tr class="project-row">')
    assert rows >= 1
    head = _head(page)
    assert "All projects · Projects" in head
    assert f'<h1 class="page-title">{rows} project{"" if rows == 1 else "s"}</h1>' in head
    assert 'class="panel-card panel-card--flush projects"' in page
    assert '<th scope="col" class="num">Open issues</th>' in page


def test_the_delivered_section_carries_its_own_look(world: World) -> None:  # noqa: F811
    """sse.js swaps the section whole, so the flush card must be on the fragment's own root."""
    client, _, _ = world
    fragment = client.get("/fragments/p7").text
    root = fragment[fragment.index("<section"):fragment.index(">", fragment.index("<section"))]
    assert 'class="panel-card panel-card--flush delivered' in root
    assert 'id="panel-p7"' in root and 'data-panel="p7"' in root
    page = client.get("/delivered?from=2000-01-01&to=2100-01-01").text
    head = _head(page)
    assert '<p class="page-eyebrow">Delivered</p>' in head and 'class="window-form"' in head
    assert '<span class="feature-id">1e581663e8</span>' in page  # the commit as a code chip
    assert 'class="panel-card__foot delivered__others"' in page  # the other endings, as the card's foot
    assert "—" not in _main(page)


def test_issue_kinds_are_pills_in_the_right_colour(world: World) -> None:  # noqa: F811
    client, _, _ = world
    page = html.unescape(client.get("/issues").text)
    open_part = page[:page.index("Closed in the last 7 days")]
    open_rows = open_part.count('<tr class="issue-row ">')
    assert open_rows >= 2
    assert f'<h1 class="page-title">{open_rows} open</h1>' in _head(page)
    # Waiting for a person is purple; a failure is red; the severity keeps its own word.
    assert re.search(r'status-pill--wait">[^<]*<svg.*?</svg>Waiting for the merge decision', open_part)
    assert re.search(r'status-pill--fail">[^<]*<svg.*?</svg>Planning failed', open_part)
    assert "band-chip__word" in open_part


def test_closed_issues_are_dimmed_with_a_closed_pill(world: World) -> None:  # noqa: F811
    client, store, fake = world
    fake.planning("c5b", "Sort the notes.", start=30, end=50)  # sent again later: its failure closes
    mirror_once(open_rw(store), fake.path)
    page = html.unescape(client.get("/issues").text)
    closed = page[page.index("Closed in the last 7 days"):]
    row = closed[closed.index('<tr class="issue-row issue-row--closed">'):]
    row = row[:row.index("</tr>")]
    assert "Planning failed" in row
    assert 'status-pill--neutral">' in row and "Closed <span" in row
    assert "status-pill--fail" not in row and "band-chip" not in row  # closed: plain words, dimmed


def test_the_missing_feature_page_is_a_head_and_an_honest_gap(world: World) -> None:  # noqa: F811
    client, _, _ = world
    page = client.get("/features/FEAT-NOPE").text
    assert '<h1 class="page-title">No feature called FEAT-NOPE</h1>' in _head(page)
    assert 'class="true-empty feature-missing"' in page and 'href="/features"' in page


# --- the chat and microphone look ---------------------------------------------------------------

#: The names Part D's chat markup and Part E's microphone markup and script (mic.js) use.
CHAT_NAMES = (
    "chat-toggle", "chat-drawer", "chat", "chat__title", "chat__note", "chat__log", "chat__form", "chat__label",
    "chat__input", "chat__ask", "chat__working", "chat-exchange", "chat-exchange__question",
    "chat-exchange__answer", "chat-exchange__text", "chat-exchange__how", "chat-exchange__voice-note",
    "chat-exchange__reply", "chat-exchange--refused", "chat-exchange--unavailable", "chat-exchange--busy",
    "chat-exchange--limited", "chat-refs", "chat-refs__item",
    "chat-mic", "chat-mic--listening", "chat-mic--thinking", "chat-mic--speaking", "chat-mic--error",
    "chat-voice", "chat-voice__wave", "chat-voice__bar", "chat-voice__dots", "chat-voice__text", "chat-send",
)


def _chat_section() -> str:
    return STYLE[STYLE.index("/* ==== stage 3: chat and microphone ==== */"):]


def test_every_chat_and_microphone_name_is_styled() -> None:
    section = _chat_section()
    for name in CHAT_NAMES:
        assert re.search(r"\." + re.escape(name) + r"(?![\w-])", section), name
        assert name in STYLE[:STYLE.index("*/")], name  # and listed in the contract comment


def test_chat_motion_only_when_the_viewer_allows_it() -> None:
    section = _chat_section()
    # Remove the one allowed block, then no animation may remain.
    start = section.index("@media (prefers-reduced-motion: no-preference) {")
    end = section.index("\n}\n", start)
    rest = section[:start] + section[end:]
    assert "animation" in section[start:end]
    assert not re.search(r"animation\s*:", rest)


def test_the_floating_ask_link_is_hidden_on_phones() -> None:
    """Below 720px the tab bar has Ask, so the floating link would only cover it."""
    section = _chat_section()
    phones = re.findall(r"@media \(max-width: 720px\) \{(.*?)\n\}", section, re.S)
    assert any(re.search(r"\.chat-toggle[^{]*\{\s*display: none;", block) for block in phones)
