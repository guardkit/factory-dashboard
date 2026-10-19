"""Autoescape, client templates clean, the bus only in the answers module, read-only store opens, no CDN."""

from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import nats
import pytest
from backend import answers
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


# --- the bus: only the answers module, only card answers (design of 19 October 2026, ADR-DASH-007) ------
#
# Build, planning, approval and delivery state still come from Forge's record. The board's one use of
# the bus is answering the factory's cards: backend/answers.py holds the only bus client and the only
# publish call, publishes only on a card's response subject, and the feed never imports it.

ANSWERS = BACKEND / "answers.py"
_BUS_CLIENT = re.compile(r"\bimport\s+nats\b|\bfrom\s+nats\b|nats_core|nats\.aio|FACTORY_DASH_NATS_URL|"
                         r"FACTORY_DASH_APPROVE_PASSWORD_FILE|dashboard_approve\b")


def test_no_bus_client_outside_the_answers_module() -> None:
    for src in _py_sources():
        if src == ANSWERS:
            continue
        assert not _BUS_CLIENT.search(src.read_text(encoding="utf-8")), src
    assert _BUS_CLIENT.search(ANSWERS.read_text(encoding="utf-8"))  # the pattern still finds the real one


def test_no_jetstream_api_usage_app_wide() -> None:
    """M-D3 extended: zero JetStream API tokens anywhere, the answers module included (it publishes
    with a plain core publish and flush)."""
    pat = re.compile(
        r"jetstream|JetStreamContext|nats\.js|add_consumer|pull_subscribe|\.ack\(|\$JS\.|\.jetstream\(|js\.publish"
    )
    for src in _py_sources():
        assert not pat.search(src.read_text(encoding="utf-8")), src


def test_no_publish_call_sites_outside_the_answers_module() -> None:
    pat = re.compile(r"\.publish\(")
    for src in _py_sources():
        if src != ANSWERS:
            assert not pat.search(src.read_text(encoding="utf-8")), src


def _publish_calls() -> list[tuple[str, ast.Call]]:
    """Every `<x>.publish(...)` call in the answers module, with the function it is made in."""
    tree = ast.parse(ANSWERS.read_text(encoding="utf-8"))
    found: list[tuple[str, ast.Call]] = []

    def walk(node: ast.AST, where: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = where
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{where}.{child.name}" if where else child.name
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "publish"):
                found.append((where, child))
            walk(child, name)

    walk(tree, "")
    return found


def test_the_answers_module_publishes_only_on_a_cards_response_subject() -> None:
    calls = _publish_calls()
    # Two call sites: `_send` (which checks the subject first) and the bus client's own publish
    # (which checks it again before connecting).
    assert sorted(w for w, _ in calls) == ["NatsBus.publish", "_send"], calls
    send = next(c for w, c in calls if w == "_send")
    first = send.args[0]
    assert isinstance(first, ast.Call) and isinstance(first.func, ast.Name) and first.func.id == "check_subject"
    tree = ast.parse(ANSWERS.read_text(encoding="utf-8"))
    bus = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "NatsBus")
    method = next(n for n in bus.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "publish")
    head = method.body[1] if isinstance(method.body[0], ast.Expr) and isinstance(
        getattr(method.body[0], "value", None), ast.Constant) else method.body[0]
    assert isinstance(head, ast.Expr) and isinstance(head.value, ast.Call)
    assert getattr(head.value.func, "id", "") == "check_subject"
    assert answers.RESPONSE_SUBJECT.pattern == r"agents\.approval\.forge\.[^.*>\s]{1,200}\.response"
    for bad in ("agents.approval.forge.x", "pipeline.build.x", "agents.approval.forge.*.response",
                "agents.approval.forge.a.b.response", "agents.approval.forge.>.response",
                "agents.approval.forge..response", "agents.command.forge.x.response"):
        try:
            answers.check_subject(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert answers.check_subject("agents.approval.forge.merge-FEAT-1.response")


def test_the_bus_client_refuses_any_other_subject_before_connecting(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*_a: object, **_k: object) -> None:
        raise AssertionError("connected")

    monkeypatch.setattr(nats, "connect", never)
    (tmp_path / "pw").write_text("not-a-real-password", encoding="utf-8")
    bus = answers.NatsBus("nats://127.0.0.1:1", tmp_path / "pw")
    for bad in ("pipeline.x", "agents.approval.forge.x"):
        try:
            asyncio.run(bus.publish(bad, b"{}"))
        except ValueError:
            continue
        raise AssertionError(bad)


_IMPORTS_ANSWERS = re.compile(
    r"^\s*(?:from|import)\s+(?:nats|backend\.answers|backend\.claims|backend\.app)\b"
    r"|^\s*from\s+backend\s+import\s+[^\n]*\b(?:answers|claims|app)\b", re.MULTILINE)


def test_the_feed_never_imports_the_answers_module_or_a_bus_client() -> None:
    for src in (BACKEND / "projector").rglob("*.py"):
        text = src.read_text(encoding="utf-8")
        assert not _IMPORTS_ANSWERS.search(text), src
    # And at run time: importing every part of the feed loads neither.
    code = ("import sys, pkgutil, importlib, backend.projector as p\n"
            "import backend.projector.__main__\n"
            "[importlib.import_module('backend.projector.' + m.name) for m in pkgutil.iter_modules(p.__path__)]\n"
            "bad = [m for m in sys.modules if m in ('backend.answers', 'backend.claims', 'backend.app')"
            " or m == 'nats' or m.startswith('nats.')]\n"
            "print(','.join(bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=BACKEND.parent, env={**os.environ, "PYTHONPATH": str(BACKEND.parent)})
    assert out.stdout.strip() == "", out.stdout


def test_the_read_model_never_imports_the_answers_module() -> None:
    for src in (BACKEND / "readmodel").rglob("*.py"):
        assert not _IMPORTS_ANSWERS.search(src.read_text(encoding="utf-8")), src


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
