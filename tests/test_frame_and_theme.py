"""The Appmilla Factory frame (redesign stage 1): the theme script, the icon sprite, the client
frame's fences, the self-hosted fonts and the dark tokens."""

from __future__ import annotations

import re
from pathlib import Path

from backend.app import create_app
from backend.readmodel.viewmodels import AsOf
from fastapi.testclient import TestClient

from tests.test_pages import world  # noqa: F401  (the page-test factory, reused)

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
TEMPLATES = FRONTEND / "templates"
STATIC = FRONTEND / "static"
STYLE = (STATIC / "style.css").read_text(encoding="utf-8")
THEME_JS = (STATIC / "theme.js").read_text(encoding="utf-8")
THEME_TAG = '<script src="/static/theme.js"></script>'


def _head(name: str) -> str:
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    return text[: text.index("</head>")]


# --- the theme script ---------------------------------------------------------------------------


def test_theme_script_is_in_every_head_without_defer() -> None:
    """It must run before the first paint, so it is a plain, non-deferred script in <head>, after
    the stylesheet and before anything deferred."""
    for name in ("base.html", "client_base.html", "login.html"):
        head = _head(name)
        assert head.count(THEME_TAG) == 1, name
        assert head.index('<link rel="stylesheet" href="/static/style.css">') < head.index(THEME_TAG), name
        deferred = [m.start() for m in re.finditer(r"<script[^>]*\bdefer\b", head)]
        assert all(head.index(THEME_TAG) < at for at in deferred), name


def test_theme_script_guards_storage_and_media_queries() -> None:
    """localStorage and matchMedia can throw (private windows, blocked site data): each use sits
    inside a try, and the page falls back to light."""
    for api in ("localStorage.getItem", "localStorage.setItem", "matchMedia("):
        at = THEME_JS.index(api)
        before = THEME_JS[:at]
        assert before.rfind("try {") > before.rfind("}"), api
    assert 'return "light"' in THEME_JS
    assert "http://" not in THEME_JS and "https://" not in THEME_JS


def test_no_inline_script_in_any_template() -> None:
    """Every script is a first-party or vendored file; an inline one would also count as visible
    text to the plain-English check."""
    for f in TEMPLATES.rglob("*.html"):
        for tag in re.findall(r"<script\b[^>]*>", f.read_text(encoding="utf-8")):
            assert "src=" in tag, (f.name, tag)
        assert not re.search(r"\son[a-z]+=", f.read_text(encoding="utf-8")), f.name


def test_the_html_starts_light_so_a_page_without_the_script_still_renders() -> None:
    for name in ("base.html", "client_base.html"):
        assert '<html lang="en-GB" data-theme="light">' in (TEMPLATES / name).read_text(encoding="utf-8")


# --- the frame --------------------------------------------------------------------------------


def test_operator_frame_has_the_sprite_once_and_the_brand(operator_client: TestClient) -> None:
    body = operator_client.get("/").text
    assert body.count('id="am-sprite"') == 1
    assert '<symbol id="i-check"' in body
    assert 'src="/static/logos/appmilla-wordmark-white-on-dark.png"' in body
    assert '<span class="topbar__product">Factory</span>' in body
    assert "<title>Now · Appmilla Factory</title>" in body
    assert '<nav class="tabbar" aria-label="Main">' in body
    assert "site-footer" not in body


def test_client_frame_has_no_operator_places_chat_or_live_updates(clientco_client: TestClient) -> None:
    body = clientco_client.get("/").text
    assert body.count('id="am-sprite"') == 1
    for word in ("Fleet", "Issues", "chat-toggle", "chat-drawer", "sse.js", "sse-dot", "tabbar",
                 "read-asof", "A-8", "IN-4"):
        assert word not in body, word
    assert '<form method="post" action="/logout"' in body and 'href="/logout"' not in body
    assert "· Appmilla Factory</title>" in body


def test_no_page_title_still_says_factory_dashboard() -> None:
    for f in TEMPLATES.rglob("*.html"):
        assert "Factory dashboard" not in f.read_text(encoding="utf-8"), f.name


# --- fonts and tokens ---------------------------------------------------------------------------


def test_every_font_face_names_a_shipped_woff2() -> None:
    faces = re.findall(r"@font-face\s*\{[^}]*\}", STYLE)
    assert len(faces) == 7
    for face in faces:
        urls = re.findall(r'url\("([^"]+)"\)', face)
        assert len(urls) == 1, face
        url = urls[0]
        assert url.startswith("/static/fonts/") and url.endswith(".woff2"), url
        assert (STATIC / "fonts" / url.removeprefix("/static/fonts/")).is_file(), url
        assert "font-display: swap" in face


def _block(selector: str) -> str:
    at = STYLE.index(selector + " {")
    return STYLE[at: STYLE.index("}", at)]


def test_dark_tokens_live_on_the_attribute_selector() -> None:
    """data-theme="dark" on one section (the Needs-you band, the sign-in brand panel) re-resolves
    every token inside it, which needs attribute selectors, not :root alone."""
    light = _block(':root, [data-theme="light"]')
    dark = _block('[data-theme="dark"]')
    for token in ("--bg", "--surface", "--text", "--text-dim", "--heading", "--accent-text", "--focus",
                  "--line", "--line-strong", "--status-wait", "--status-fail-text"):
        assert f"{token}:" in light, token
        assert f"{token}:" in dark, token
    assert "--bg: #1C2D24;" in dark and "--bg: #F5F5F5;" in light
    # The old names are aliases declared on the theme selectors too, so they re-resolve as well.
    aliases = _block(':root, [data-theme="light"], [data-theme="dark"]')
    assert "--card: var(--surface);" in aliases and "--muted: var(--text-dim);" in aliases


def test_later_stages_have_their_own_sections() -> None:
    for label in ("stage 2a: Now, Feature, Project", "stage 2b: Reports, export, Fleet, Login, Client",
                  "stage 3: Features, Delivered, Issues", "stage 3: chat and microphone"):
        assert f"/* ==== {label} ==== */" in STYLE, label


# --- honesty surfaces: emptying any of these must fail a test -------------------------------------


def _macros(tmp_path: Path):  # type: ignore[no-untyped-def]
    env = create_app(db_path=tmp_path / "rm.db").state.templates.env
    return env.get_template("macros.html").module


def test_lagging_note_shows_its_clock_and_its_words(tmp_path: Path) -> None:
    note = str(_macros(tmp_path).lagging_note(
        AsOf(prefix="factory record read", label="12:00:00", title_iso="", dot_state="lagging")))
    assert '<p class="lagging-note">' in note
    assert '<use href="#i-clock"/>' in note
    assert "Showing the last known data (factory record read 12:00:00)." in note


def test_asof_chip_carries_all_three_state_words(tmp_path: Path) -> None:
    """freshness.js swaps the state classes on the open page; the stylesheet shows the matching
    word, so all three must be in the HTML."""
    chip = str(_macros(tmp_path).asof_chip(AsOf(prefix="read", label="12:00:00", title_iso="", dot_state="live",
                                        asof_epoch=1.0, threshold_secs=60)))
    for state, word in (("live", "up to date"), ("lagging", "a little old"), ("stale", "out of date")):
        assert f'asof-chip__word asof-chip__word--{state}">{word}</span>' in chip, state
    assert 'class="asof-dot asof-dot--live"' in chip and 'data-threshold="60"' in chip


def test_a_features_row_says_its_cost_is_not_measured(world: tuple[TestClient, Path, object]) -> None:  # noqa: F811
    client, _, _ = world
    body = client.get("/features").text
    start = body.index('<tr class="feature-list__row')
    row = body[start: body.index("</tr>", start)]
    assert '<span class="spend-slot" data-state="not-measured">Not measured</span>' in row


def test_waiting_for_a_person_is_purple_in_the_journey() -> None:
    purple = re.search(
        r"([^{}]*)\{[^}]*background: var\(--status-wait-tint\); border-color: var\(--status-wait\)", STYLE)
    assert purple is not None
    assert ".journey-station--waiting .journey-station__glyph" in purple.group(1)


# --- Codex round 1, R2: old data is marked, never faded below readable contrast -----------------


def test_stale_cards_keep_their_text_at_full_contrast() -> None:
    """Opacity on a stale card took dim text to about 2.5:1. Staleness is the lagging note and an
    amber bar; text is never faded. The only opacity below 1 left is on disabled buttons (exempt
    from contrast rules) and the chat's animated dots (decoration, not text)."""
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", STYLE)
    for selector, body in rules:
        found = re.search(r"opacity:\s*([\d.]+)", body)
        if not found or float(found.group(1)) >= 1:
            continue
        sel = selector.strip()
        assert "state-lagging" not in sel and "--closed" not in sel, sel
        assert re.fullmatch(r"[\d%,\s]+|from|to", sel) or "[disabled]" in sel, sel
    assert any("state-lagging" in sel and "var(--status-risk)" in body for sel, body in rules)
