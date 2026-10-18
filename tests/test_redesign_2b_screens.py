"""Redesign stage 2b: Reports, Fleet, sign-in and the client home keep their words and fences."""

from __future__ import annotations

import re
from pathlib import Path

from backend.readmodel.viewmodels import AsOf, FeedPanel, PanelState, ServiceRow, ServingPanel
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.testclient import TestClient

from tests.conftest import login

TEMPLATES = Path(__file__).resolve().parent.parent / "frontend" / "templates"


def test_the_sign_in_form_is_unchanged_apart_from_classes(client: TestClient) -> None:
    body = client.get("/login").text
    assert '<form method="post" action="/login" class="login-form">' in body
    assert ('<input type="text" name="username" autocomplete="username" autocapitalize="none" '
            'spellcheck="false" required>') in body
    assert '<input type="password" name="password" autocomplete="current-password" required>' in body
    assert '<button type="submit" class="button--primary button--block">Sign in</button>' in body
    # The brand panel is dark on its own, and the page still promises it only reads.
    assert '<section class="login-brand" data-theme="dark">' in body
    assert "it changes nothing" in body


def test_a_refused_sign_in_still_alerts(client: TestClient) -> None:
    r = login(client, username="nobody", password="wrong")
    assert 'class="login-card__error" role="alert"' in r.text


def test_every_service_shows_a_word_not_only_a_colour(operator_client: TestClient) -> None:
    body = operator_client.get("/fleet").text
    rows = re.findall(r'<li class="service-row service-row--(\w+) signal-row">(.*?)</li>', body, re.S)
    assert rows
    words = {"ok": "Up", "fail": "Not answering", "unknown": "Unknown"}
    for dot, row in rows:
        assert 'class="status-pill' in row and words[dot] in row, dot
    # The fragments keep the attributes the live updates swap them by.
    assert 'id="panel-p6" data-panel="p6"' in body and 'id="panel-proj" data-panel="proj"' in body


def test_the_feed_card_says_running_or_stopped_in_words(operator_client: TestClient) -> None:
    body = operator_client.get("/fragments/proj").text
    assert ">Running</span>" in body or ">Stopped</span>" in body


def test_the_client_home_shows_no_costs_or_machinery(clientco_client: TestClient) -> None:
    body = clientco_client.get("/").text
    for word in ("cost", "Cost", "spend", "Spend", "£", "Machinery", "model", "Fleet", "Issues"):
        assert word not in re.sub(r"<[^>]+>", " ", body), word
    assert "Your project" in body and "being provisioned" in body
    text = (TEMPLATES / "client_home.html").read_text(encoding="utf-8")
    assert not re.search(r"coach_score|coach-score|turns|agent_id|evidence", text)


# --- coach round 1 (stage 2b): every status keeps its word, not just its icon ---------------------

_ENV = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(default=True))
_AS_OF = AsOf(prefix="checked", label="12:00:00", title_iso="", dot_state="live")


def _pill_words(body: str) -> list[str]:
    """The words inside each status pill or honesty badge, icons and times left out."""
    pieces = re.findall(r'<span class="(?:status-pill|honest-badge)[^"]*">(.*?)</span>', body, re.S)
    return [re.sub(r"<svg.*?</svg>", "", p, flags=re.S).split("<span")[0].strip() for p in pieces]


def test_each_service_state_says_its_word() -> None:
    panel = ServingPanel(panel_id="p6", title="Services", state=PanelState.LIVE, as_of=_AS_OF, services=(
        ServiceRow(name="One", endpoint="a", dot="ok", detail="answering"),
        ServiceRow(name="Two", endpoint="b", dot="fail", detail="no answer"),
        ServiceRow(name="Three", endpoint="c", dot="unknown", detail="not checked lately"),
    ))
    body = _ENV.get_template("fragments/services.html").render(panel=panel)
    assert _pill_words(body) == ["Up", "Not answering", "Unknown"]


def test_the_feed_says_running_or_stopped_in_words() -> None:
    template = _ENV.get_template("fragments/feed.html")
    for ok, word in ((True, "Running"), (False, "Stopped")):
        panel = FeedPanel(panel_id="proj", title="Dashboard feed", state=PanelState.LIVE, as_of=_AS_OF,
                          heartbeat_ok=ok, heartbeat_label="running" if ok else "stopped", read_label="Read")
        assert _pill_words(template.render(panel=panel)) == [word], word
