"""Routes 200; every top-nav item resolves; the operator-only guard; the client dark layout."""

from __future__ import annotations

import re

from backend.app import NAV
from fastapi.routing import APIRoute
from starlette.testclient import TestClient

OPERATOR_PAGES = ["/", "/features", "/features/FEAT-3ED2", "/projects/study-tutor",
                  "/delivered", "/reports", "/issues", "/fleet"]


def test_every_operator_page_200(operator_client: TestClient) -> None:
    for path in OPERATOR_PAGES:
        r = operator_client.get(path)
        assert r.status_code == 200, path


def test_every_nav_item_resolves_to_a_live_route(operator_client: TestClient) -> None:
    """Acceptance item 14: no dead links in the constant nav (incl. /features and /reports)."""
    for _label, href, _key in NAV:
        r = operator_client.get(href)
        assert r.status_code == 200, href


def test_reports_renders_the_weekly_report(operator_client: TestClient) -> None:
    """ux §4.7 (S4): /reports renders the weekly delivery report (headline + plan-vs-actual +
    the client-safe export toggle), no longer the D0 'arrives at S4' placeholder."""
    body = operator_client.get("/reports").text
    assert "Weekly delivery report" in body
    assert "PLAN vs ACTUAL" in body
    assert "client-safe export" in body
    assert "arrives at S4" not in body


def test_unauthenticated_redirects_to_login(client: TestClient) -> None:
    for path in ["/", "/features", "/delivered"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"


def test_fragment_api_requires_login(client: TestClient) -> None:
    r = client.get("/fragments/p1", follow_redirects=False)
    assert r.status_code == 401


CLIENT_REDIRECTED = ["/features", "/features/FEAT-3ED2", "/features/anything",
                     "/projects/study-tutor", "/projects/anything", "/delivered", "/reports",
                     "/reports?view=export", "/issues", "/fleet"]
CLIENT_FORBIDDEN = ["/fragments/p1", "/events?once=1"]
OPEN_ROUTES = {"/", "/login", "/logout"}  # "/" is tenant-aware: the client layout for a client


def test_client_session_redirected_from_operator_pages(clientco_client: TestClient) -> None:
    """ux §2: operator-only routes return a clean redirect-to-client-layout for a client session."""
    for path in CLIENT_REDIRECTED:
        r = clientco_client.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/", path


def test_every_route_is_guarded_for_a_client(app, clientco_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    """Every registered route other than the open ones is covered by the lists above, so a new
    route (or a route that loses its guard) cannot slip past the client scoping unnoticed."""
    covered = {p.split("?")[0] for p in CLIENT_REDIRECTED + CLIENT_FORBIDDEN}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = re.sub(r"\{[^}]+\}", "anything", route.path)
        if route.path in OPEN_ROUTES:
            continue
        assert any(re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", route.path), c) for c in covered), (
            f"{route.path} is not in the client-guard lists"
        )
        r = clientco_client.get(path + ("?once=1" if route.path == "/events" else ""),
                                follow_redirects=False)
        assert r.status_code in (303, 403, 404), (route.path, r.status_code)
        assert r.status_code != 200


def test_client_session_gets_403_on_fragment_api(clientco_client: TestClient) -> None:
    r = clientco_client.get("/fragments/p1", follow_redirects=False)
    assert r.status_code == 403


def test_client_home_is_dark_client_layout(clientco_client: TestClient) -> None:
    """Acceptance item 9: client layout, plain-language dark state, no operator nav."""
    body = clientco_client.get("/").text
    assert "being provisioned" in body               # plain-language honesty (§7.2)
    assert "Fleet" not in body and "Issues" not in body  # no operator nav
    # never cites an internal ask id on a client surface
    assert "A-8" not in body and "IN-4" not in body


def test_operator_home_is_the_operator_layout(operator_client: TestClient) -> None:
    r = operator_client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "being provisioned" not in r.text
    assert 'href="/fleet"' in r.text and 'href="/issues"' in r.text  # the operator nav
    assert operator_client.get("/fragments/p1").status_code == 200


def test_operator_reaches_the_pages_a_client_is_redirected_from(operator_client: TestClient) -> None:
    for path in CLIENT_REDIRECTED:
        assert operator_client.get(path, follow_redirects=False).status_code in (200, 404), path


def test_unknown_fragment_is_404(operator_client: TestClient) -> None:
    assert operator_client.get("/fragments/p9").status_code == 404
