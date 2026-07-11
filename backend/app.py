"""FastAPI entrypoint — server-rendered panels, sessions, tenant resolution, route guard.

ADR-DASH-003: HTMX-first, server-rendered FastAPI + Jinja. ADR-DASH-007 / fence 1-2: this app
imports NO NATS anything at S1 (verified by grep at stage close) and holds no write path. Fence 6:
Jinja autoescape is ON; every event-derived string renders inert.

Tenancy (design §7): the session holds only the username; the tenant is re-read from the DB per
request (F-2f). Operator-only routes (the whole operator nav) return a clean redirect-to-client-
layout for a client-tenant session, and 403 for the fragment API — an explicit guard, never an
incidental DB-open failure (ux §2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import select_autoescape
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from backend import auth, db
from backend.config_loader import (
    PLANS_PATH,
    TENANTS_PATH,
    Tenant,
    load_plans,
    load_tenants,
)
from backend.readmodel import queries

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "frontend" / "templates"
STATIC_DIR = BASE_DIR / "frontend" / "static"

# Top nav (constant) — ux §2. Every item resolves to a live route (acceptance item 14).
NAV: tuple[tuple[str, str, str], ...] = (
    ("Now", "/", "now"),
    ("Features", "/features", "features"),
    ("Delivered", "/delivered", "delivered"),
    ("Reports", "/reports", "reports"),
    ("Issues", "/issues", "issues"),
    ("Fleet", "/fleet", "fleet"),
)


class _LoginRedirect(Exception):
    """Unauthenticated request to a protected route -> bounce to /login."""


class _ClientRedirect(Exception):
    """Client-tenant session on an operator-only page route -> bounce to the client layout."""


def create_app(
    *,
    db_path: Path | None = None,
    tenants_path: Path = TENANTS_PATH,
    plans_path: Path = PLANS_PATH,
    secret_key: str = "dev-insecure-key-change-me",
) -> FastAPI:
    db_path = db_path or (BASE_DIR / "readmodel.db")
    tenants = load_tenants(tenants_path)
    load_plans(plans_path)  # validated at boot; mirrored by the projector at S4
    db.init_db(db_path, tenants.values())

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # Fence 6: autoescape ON for every event-derived string.
    templates.env.autoescape = select_autoescape(default=True, default_for_string=True)

    app = FastAPI(title="factory-dashboard", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret_key, same_site="lax", https_only=False)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.state.db_path = db_path
    app.state.tenants = tenants
    app.state.templates = templates

    _register_exception_handlers(app)
    _register_routes(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    async def on_login_redirect(request: Request, exc: Exception) -> Response:
        return RedirectResponse("/login", status_code=303)

    async def on_client_redirect(request: Request, exc: Exception) -> Response:
        return RedirectResponse("/", status_code=303)

    app.add_exception_handler(_LoginRedirect, on_login_redirect)
    app.add_exception_handler(_ClientRedirect, on_client_redirect)


# --- session -> tenant resolution -------------------------------------------


def _resolve(request: Request) -> auth.ResolvedUser | None:
    username = request.session.get("username")
    if not username:
        return None
    conn = db.connect_ro(request.app.state.db_path)
    try:
        return auth.resolve_user(conn, str(username))
    finally:
        conn.close()


def operator_page(request: Request) -> auth.ResolvedUser:
    """Guard for operator-only PAGE routes: redirect unauth -> /login, client -> / (client layout)."""
    user = _resolve(request)
    if user is None:
        raise _LoginRedirect
    if not user.is_operator:
        raise _ClientRedirect
    return user


def operator_api(request: Request) -> auth.ResolvedUser:
    """Guard for the fragment API: 401 unauth, 403 for a client tenant (explicit, not a DB failure)."""
    user = _resolve(request)
    if user is None:
        raise HTTPException(status_code=401, detail="login required")
    if not user.is_operator:
        raise HTTPException(status_code=403, detail="operator only")
    return user


OperatorPage = Annotated[auth.ResolvedUser, Depends(operator_page)]
OperatorApi = Annotated[auth.ResolvedUser, Depends(operator_api)]


def _tenant(request: Request, user: auth.ResolvedUser) -> Tenant:
    tenants: dict[str, Tenant] = request.app.state.tenants
    return tenants.get(
        user.tenant_slug, Tenant(user.tenant_slug, user.tenant_slug, None, None, (), None)
    )


def _render(request: Request, name: str, context: dict[str, object]) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    ctx: dict[str, object] = {"nav": NAV, **context}
    # base.html needs the page chrome (top-bar projector chip + banner slot, ux §3). Most views
    # carry it; surface it uniformly so every operator page renders the chrome without repetition.
    if "chrome" not in ctx:
        view = ctx.get("view")
        if view is not None and hasattr(view, "chrome"):
            ctx["chrome"] = view.chrome
    return templates.TemplateResponse(request, name, ctx)


def _register_routes(app: FastAPI) -> None:
    # --- auth ---------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> HTMLResponse:
        return _render(request, "login.html", {"error": None})

    @app.post("/login")
    def login_submit(
        request: Request,
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
    ) -> Response:
        conn = db.connect_ro(request.app.state.db_path)
        try:
            user = auth.authenticate(conn, username, password)
        finally:
            conn.close()
        if user is None:
            return _render(request, "login.html", {"error": "Invalid credentials."})
        request.session["username"] = user.username
        return RedirectResponse("/", status_code=303)

    @app.get("/logout")
    def logout(request: Request) -> Response:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # --- home (tenant-aware) ------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> Response:
        user = _resolve(request)
        if user is None:
            raise _LoginRedirect
        tenant = _tenant(request, user)
        if not user.is_operator:
            # Client layout, honestly dark at S1 (D3 lights the client feed).
            return _render(request, "client_home.html", {"tenant": tenant})
        return _render(
            request,
            "home.html",
            {"view": queries.home_view(), "nav_active": "now", "tenant": tenant},
        )

    # --- operator pages -----------------------------------------------------

    @app.get("/features", response_class=HTMLResponse)
    def features(request: Request, user: OperatorPage) -> HTMLResponse:
        return _render(
            request,
            "features.html",
            {"view": queries.features_view(), "nav_active": "features", "tenant": _tenant(request, user)},
        )

    @app.get("/features/{feature_id}", response_class=HTMLResponse)
    def feature(request: Request, feature_id: str, user: OperatorPage) -> HTMLResponse:
        page = queries.feature_page(feature_id)
        if page is None:
            return _render(
                request,
                "feature_missing.html",
                {"feature_id": feature_id, "nav_active": "features", "tenant": _tenant(request, user)},
            )
        return _render(
            request,
            "feature.html",
            {
                "page": page,
                "chrome": queries.chrome(),
                "nav_active": "features",
                "tenant": _tenant(request, user),
            },
        )

    @app.get("/projects/{project}", response_class=HTMLResponse)
    def project(request: Request, project: str, user: OperatorPage) -> HTMLResponse:
        return _render(
            request,
            "project.html",
            {"view": queries.project_view(project), "nav_active": "", "tenant": _tenant(request, user)},
        )

    @app.get("/delivered", response_class=HTMLResponse)
    def delivered(request: Request, user: OperatorPage) -> HTMLResponse:
        return _render(
            request,
            "delivered.html",
            {
                "panel": queries.delivered_view(),
                "chrome": queries.chrome(),
                "nav_active": "delivered",
                "tenant": _tenant(request, user),
            },
        )

    @app.get("/reports", response_class=HTMLResponse)
    def reports(request: Request, user: OperatorPage) -> HTMLResponse:
        return _render(
            request,
            "reports.html",
            {"view": queries.reports_placeholder(), "nav_active": "reports", "tenant": _tenant(request, user)},
        )

    @app.get("/issues", response_class=HTMLResponse)
    def issues(request: Request, user: OperatorPage) -> HTMLResponse:
        return _render(
            request,
            "issues.html",
            {"view": queries.issues_view(), "nav_active": "issues", "tenant": _tenant(request, user)},
        )

    @app.get("/fleet", response_class=HTMLResponse)
    def fleet(request: Request, user: OperatorPage) -> HTMLResponse:
        return _render(
            request,
            "fleet.html",
            {"view": queries.fleet_view(), "nav_active": "fleet", "tenant": _tenant(request, user)},
        )

    # --- fragment API (HTMX refetch target; SSE wiring lands at S2) ----------

    @app.get("/fragments/{panel}", response_class=HTMLResponse)
    def fragment(request: Request, panel: str, user: OperatorApi, state: str | None = None) -> HTMLResponse:
        try:
            view = queries.panel_view(panel, state)
        except queries.UnknownPanel as exc:
            raise HTTPException(status_code=404, detail="unknown panel") from exc
        template = f"fragments/{_FRAGMENT_TEMPLATES[panel]}"
        return _render(request, template, {"panel": view, "standalone": True})


_FRAGMENT_TEMPLATES: dict[str, str] = {
    "p1": "p1_agents.html",
    "p2": "p2_build_board.html",
    "p3": "p3_gate_events.html",
    "p4": "p4_needs_you.html",
    "p5": "p5_planning.html",
    "p6": "p6_serving.html",
    "p7": "p7_delivered.html",
    "proj": "projector.html",
}


# The module-level ASGI app for `uvicorn backend.app:app` in production.
app = create_app()
