"""The Appmilla Factory brand assets are self-hosted: fonts, logos and the icon sprite.

The no-CDN fence (test_honesty_and_fences.py) scans templates and the top level of static/.
These tests extend it to the new sub-folders and check that what is shipped is what it claims to be.
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
STATIC = FRONTEND / "static"
FONTS = STATIC / "fonts"
LOGOS = STATIC / "logos"
VENDORED = {"htmx.min.js", "htmx-ext-sse.js"}

EXPECTED_FONTS = {
    "urbanist-latin-600-normal.woff2",
    "urbanist-latin-700-normal.woff2",
    *(f"public-sans-latin-{w}-normal.woff2" for w in (300, 400, 500, 600, 700)),
}
LICENCES = {"Urbanist-OFL.txt", "PublicSans-OFL.txt"}


def test_fonts_are_woff2_with_their_licences() -> None:
    names = {f.name for f in FONTS.iterdir()}
    assert names == EXPECTED_FONTS | LICENCES
    for name in EXPECTED_FONTS:
        assert (FONTS / name).read_bytes()[:4] == b"wOF2", name
    for name in LICENCES:
        assert "SIL Open Font License, Version 1.1" in (FONTS / name).read_text(encoding="utf-8")


def test_logos_are_png() -> None:
    names = {f.name for f in LOGOS.iterdir()}
    assert names == {
        "appmilla-icon-A.png",
        "appmilla-wordmark-white-on-dark.png",
        "appmilla-wordmark-deepgreen-on-light.png",
    }
    for f in LOGOS.iterdir():
        assert f.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", f


def test_no_external_references_in_any_static_css_or_js() -> None:
    """Recursive, so a stylesheet or script in a sub-folder cannot fetch from outside."""
    for f in STATIC.rglob("*"):
        if f.suffix in {".css", ".js"} and f.name not in VENDORED:
            text = f.read_text(encoding="utf-8")
            assert "http://" not in text and "https://" not in text, f


def test_icon_sprite_is_inline_and_complete() -> None:
    sprite = (FRONTEND / "templates" / "icon_sprite.html").read_text(encoding="utf-8")
    ids = re.findall(r'<symbol id="i-([a-z]+)" viewBox="0 0 16 16">', sprite)
    assert len(ids) == len(set(ids)) == 27
    assert {"check", "alert", "x", "clock", "mic", "chat", "home", "report"} <= set(ids)
    assert "http" not in sprite and "<script" not in sprite
    assert 'aria-hidden="true"' in sprite
