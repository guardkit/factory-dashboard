"""Routes 200; every top-nav item resolves; the operator-only guard; the client dark layout."""

from __future__ import annotations

from backend.app import NAV
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


def test_reports_is_plain_placeholder_not_feed_pending(operator_client: TestClient) -> None:
    """ux §2: /reports renders a plain 'arrives at S4' placeholder, NOT the FEED-PENDING treatment."""
    body = operator_client.get("/reports").text
    assert "arrives at S4" in body
    assert "FEED PENDING" not in body


def test_unauthenticated_redirects_to_login(client: TestClient) -> None:
    for path in ["/", "/features", "/delivered"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"


def test_fragment_api_requires_login(client: TestClient) -> None:
    r = client.get("/fragments/p1", follow_redirects=False)
    assert r.status_code == 401


def test_client_session_redirected_from_operator_pages(clientco_client: TestClient) -> None:
    """ux §2: operator-only routes return a clean redirect-to-client-layout for a client session."""
    for path in ["/features", "/delivered", "/reports", "/issues", "/fleet"]:
        r = clientco_client.get(path, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"


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


def test_unknown_fragment_is_404(operator_client: TestClient) -> None:
    assert operator_client.get("/fragments/p9").status_code == 404
