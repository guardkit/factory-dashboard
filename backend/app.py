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

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import select_autoescape
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from backend import auth, db, sse
from backend.config_loader import (
    PLANS_PATH,
    TENANTS_PATH,
    Tenant,
    load_plans,
    load_tenants,
)
from backend.readmodel import dbread, queries

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


async def _sse_stream(
    request: Request, db_path: Path, wanted: set[str], last_id: int, once: bool
) -> AsyncIterator[bytes]:
    """The /events body (design §6): replay/refetch-all on connect, then tail change_log. Kept at
    module level so the route stays thin. Notification-only — no row data on the channel (§6.1)."""
    conn = db.connect_ro(db_path)
    try:
        for upd in sse.replay_or_refetch(conn, last_id, wanted):
            last_id = max(last_id, upd.seq)
            yield upd.to_sse().encode()
        if once:
            return
        while not await request.is_disconnected():  # pragma: no cover (streaming loop)
            for upd in sse.events_since(conn, last_id, wanted):
                last_id = upd.seq
                yield upd.to_sse().encode()
            yield b": keepalive\n\n"
            await asyncio.sleep(1)
    finally:
        conn.close()


def _last_event_id(request: Request) -> int:
    """Reconnect resume point (design §6): the EventSource `Last-Event-ID` header, or a
    `last_event_id` query fallback. Non-numeric or absent -> 0 (a fresh subscription)."""
    raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id") or ""
    return int(raw) if raw.isdigit() else 0


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


def _reports_response(
    request: Request,
    user: auth.ResolvedUser,
    from_: str | None,
    to: str | None,
    view: str | None,
    tenant: str | None,
) -> HTMLResponse:
    """ux §4.7 (S4): the weekly delivery report for ONE tenant engagement + window. The tenant is
    an OPERATOR report filter over the operator store (the session stays operator — this is not the
    §2 session-tenant rule), validated against the configured registry, defaulting to the fleet-wide
    `operator` view. The internal|export toggle is two server-rendered GET variants (no JS tabs,
    §1.6); the export is a SEPARATE, standalone template — the DF-008 outbound firewall, mechanized."""
    tenants: dict[str, Tenant] = request.app.state.tenants
    report_tenant = tenant if tenant in tenants else "operator"
    window = (from_, to) if from_ and to else None
    db_path = request.app.state.db_path
    if view == "export":
        export = dbread.export_report_view(db_path, report_tenant, window)
        return _render(
            request,
            "reports_export.html",
            {"report": export, "report_tenant": report_tenant, "nav_active": "reports",
             "tenant": _tenant(request, user)},
        )
    report = dbread.weekly_report_view(db_path, report_tenant, window)
    return _render(
        request,
        "reports.html",
        {"report": report, "chrome": report.chrome, "report_tenant": report_tenant,
         "nav_active": "reports", "tenant": _tenant(request, user)},
    )


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
            {"view": dbread.home_view(request.app.state.db_path), "nav_active": "now", "tenant": tenant},
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
    def delivered(
        request: Request,
        user: OperatorPage,
        from_: Annotated[str | None, Query(alias="from")] = None,
        to: str | None = None,
    ) -> HTMLResponse:
        # ux §4.4: plain form GET window (two date inputs). A partial/absent window falls back to
        # the default this-week window (dbread.default_window) — never an unbounded scan.
        window = (from_, to) if from_ and to else None
        view = dbread.delivered_view(request.app.state.db_path, window)
        return _render(
            request,
            "delivered.html",
            {
                "panel": view.panel,
                "chrome": view.chrome,
                "nav_active": "delivered",
                "tenant": _tenant(request, user),
            },
        )

    @app.get("/reports", response_class=HTMLResponse)
    def reports(
        request: Request,
        user: OperatorPage,
        from_: Annotated[str | None, Query(alias="from")] = None,
        to: str | None = None,
        view: str | None = None,
        tenant: str | None = None,
    ) -> HTMLResponse:
        return _reports_response(request, user, from_, to, view, tenant)

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
            {"view": dbread.fleet_view(request.app.state.db_path), "nav_active": "fleet",
             "tenant": _tenant(request, user)},
        )

    # --- fragment API (HTMX refetch target for the SSE panel_update, ux §6) --

    @app.get("/fragments/{panel}", response_class=HTMLResponse)
    def fragment(request: Request, panel: str, user: OperatorApi, state: str | None = None) -> HTMLResponse:
        if panel not in _FRAGMENT_TEMPLATES:
            raise HTTPException(status_code=404, detail="unknown panel")
        if state is not None:
            # The §4.8 states-pack demo (+ the hostile fixture): force a specific §5.2 state.
            try:
                view: object = queries.panel_view(panel, state)
            except queries.UnknownPanel as exc:
                raise HTTPException(status_code=404, detail="unknown panel") from exc
        elif panel in dbread._PANEL_BUILDERS:
            # LIVE from the read model (p7 delivered uses the default this-week window on refetch).
            view = dbread.panel_view(request.app.state.db_path, panel)
        else:  # defensive: a fragment template with no live builder falls back to fixtures
            view = queries.panel_view(panel, None)
        template = f"fragments/{_FRAGMENT_TEMPLATES[panel]}"
        return _render(request, template, {"panel": view, "standalone": True})

    # --- SSE push channel (design §6): notification-only panel_update stream --

    @app.get("/events")
    async def events(request: Request, panels: str | None = None, once: bool = False) -> Response:
        user = _resolve(request)
        if user is None:
            raise HTTPException(status_code=401, detail="login required")
        if not user.is_operator:
            raise HTTPException(status_code=403, detail="operator only")
        gen = _sse_stream(request, request.app.state.db_path, sse.parse_panels(panels),
                          _last_event_id(request), once)
        return StreamingResponse(gen, media_type="text/event-stream")


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
