"""Redesign stage 2b: Reports, Fleet, sign-in and the client home keep their words and fences."""

from __future__ import annotations

import re
from pathlib import Path

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
