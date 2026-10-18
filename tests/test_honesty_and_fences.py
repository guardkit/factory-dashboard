"""Autoescape, client templates clean, no bus client, no writes, read-only store opens, no CDN."""

from __future__ import annotations

import re
from pathlib import Path

from backend.app import create_app

BACKEND = Path(__file__).resolve().parent.parent / "backend"
TEMPLATES = Path(__file__).resolve().parent.parent / "frontend" / "templates"


def _py_sources() -> list[Path]:
    return list(BACKEND.rglob("*.py"))


# --- autoescape: hostile strings from Forge's record render inert (tests/test_pages.py renders a
# request sentence carrying a <script> tag through the real feed and pages; this checks the setting).


def test_autoescape_is_on(tmp_path: Path) -> None:
    app = create_app(db_path=tmp_path / "rm.db")
    env = app.state.templates.env
    assert env.from_string("{{ x }}").render(x="<script>") == "&lt;script&gt;"


# --- acceptance item 11: coach_score in operator templates only ------------


def test_client_templates_reference_no_operator_only_fields() -> None:
    """ux §7.2 belt-and-braces: client templates never reference operator-only fields."""
    deny = re.compile(r"coach_score|coach-score|turns|agent_id|evidence")
    for name in ("client_base.html", "client_home.html"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert not deny.search(text), name


# --- no bus connection anywhere (design of 17 October 2026) and nothing written to the bus ------


def test_no_nats_client_anywhere() -> None:
    """Build, planning, approval and delivery state come from Forge's record; the dashboard has no
    bus connection at all (the NATS monitor's HTTP health page is polled, which needs no client)."""
    pat = re.compile(r"\bimport\s+nats\b|\bfrom\s+nats\b|nats_core|nats\.aio|FACTORY_DASH_NATS_URL")
    for src in _py_sources():
        assert not pat.search(src.read_text(encoding="utf-8")), src


def test_no_jetstream_api_usage_app_wide() -> None:
    """M-D3 extended: zero JetStream API tokens (grep the exact set the coach uses)."""
    pat = re.compile(
        r"jetstream|JetStreamContext|nats\.js|add_consumer|pull_subscribe|\.ack\(|\$JS\."
    )
    for src in _py_sources():
        assert not pat.search(src.read_text(encoding="utf-8")), src


def test_no_publish_call_sites_app_wide() -> None:
    """ADR-DASH-007 / M-D3: zero publish call sites (the app writes nothing to the bus, ever)."""
    pat = re.compile(r"\.publish\(")
    for src in _py_sources():
        assert not pat.search(src.read_text(encoding="utf-8")), src


# --- fence 4: all web-layer DB opens are mode=ro ---------------------------


def test_web_layer_db_helper_opens_read_only() -> None:
    text = (BACKEND / "db.py").read_text(encoding="utf-8")
    assert "mode=ro" in text  # connect_ro is the only request-path opener


# --- fence: no CDN references in any static/template -----------------------


def test_no_cdn_references() -> None:
    """First-party templates + first-party JS/CSS reference no CDN (vendored libs are excluded —
    the fence is about not FETCHING from a CDN at runtime, which vendoring satisfies)."""
    vendored = {"htmx.min.js", "htmx-ext-sse.js"}
    files = list((BACKEND.parent / "frontend").rglob("*.html"))
    files += [
        f for f in (BACKEND.parent / "frontend" / "static").glob("*")
        if f.suffix in {".js", ".css"} and f.name not in vendored
    ]
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text, f
    # and the vendored files ARE actually present (referenced locally, not from a CDN)
    for name in vendored:
        assert (BACKEND.parent / "frontend" / "static" / name).exists()
