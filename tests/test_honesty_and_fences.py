"""Autoescape (hostile_strings inert), DF-008 client-clean, and the S1 fences (no NATS / M-D3)."""

from __future__ import annotations

import re
from pathlib import Path

from starlette.testclient import TestClient

BACKEND = Path(__file__).resolve().parent.parent / "backend"
TEMPLATES = Path(__file__).resolve().parent.parent / "frontend" / "templates"


def _py_sources() -> list[Path]:
    return list(BACKEND.rglob("*.py"))


# --- fence 6: Jinja autoescape ON; hostile strings render inert ------------


def test_hostile_strings_render_inert(operator_client: TestClient) -> None:
    body = operator_client.get("/fragments/p2?state=hostile").text
    # NO raw tag opener survives — the XSS vector is neutralised at the '<' (autoescape ON).
    # (onerror=… / hx-get=… surviving as inert TEXT is harmless once the '<' is escaped.)
    assert "<script" not in body
    assert "<img" not in body
    assert "<a hx-get" not in body
    assert 'hx-get="/steal"' not in body   # real attribute quotes are escaped to &#34;
    # the payloads appear ESCAPED instead
    assert "&lt;script&gt;" in body
    assert "&lt;img" in body or "&lt;a" in body


# --- acceptance item 11: coach_score in operator templates only ------------


def test_client_templates_reference_no_operator_only_fields() -> None:
    """ux §7.2 belt-and-braces: client templates never reference operator-only fields."""
    deny = re.compile(r"coach_score|coach-score|turns|agent_id|evidence")
    for name in ("client_base.html", "client_home.html"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert not deny.search(text), name


# --- fence 1-2 / M-D3: NATS confined to the projector; zero JetStream / publish (grep) ------


def test_nats_import_confined_to_the_projector() -> None:
    """S2 (D1): NATS is imported ONLY under backend/projector/ (connection A). No web/read-model/
    chat layer touches NATS — credentials live in the projector's process only (design §7 / fence 3)."""
    pat = re.compile(r"\bimport\s+nats\b|\bfrom\s+nats\b|nats_core")
    projector = BACKEND / "projector"
    for src in _py_sources():
        if pat.search(src.read_text(encoding="utf-8")):
            assert projector in src.parents, f"NATS imported outside the projector: {src}"


def test_no_nats_import_outside_projector() -> None:
    """The web/read-model/chat layers import no NATS anything (belt-and-braces on the above)."""
    pat = re.compile(r"\bimport\s+nats\b|\bfrom\s+nats\b|nats_core|nats\.aio")
    for src in _py_sources():
        if (BACKEND / "projector") in src.parents:
            continue
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
