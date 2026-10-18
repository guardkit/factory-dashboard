"""FastAPI entrypoint: server-rendered pages, sessions, tenant resolution, the route guard.

Server-rendered FastAPI + Jinja with HTMX for live updates. The app has no bus connection and no
write path: every request opens the store read-only. Jinja autoescape is on, so every string from
Forge's record (request sentences, error text) renders inert.

Sign-in (design 2026-10-17, requirement 2): accounts come from accounts.yaml in the configuration
folder (backend/accounts.py); the signed cookie holds only the username. The account and its
tenant are looked up again on every request (F-2f), so removing an account ends its sessions.
Operator-only routes (the whole operator nav) return a clean redirect-to-client-layout for a
client-tenant session, and 403 for the fragment API — an explicit guard, never an incidental
DB-open failure (ux §2). No client tenant is registered today, so no account can be one.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import select_autoescape
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Receive, Scope, Send

from backend import accounts, auth, db, sse, voice
from backend.chat.service import Chat, FollowUp
from backend.config_loader import (
    PLANS_PATH,
    TENANTS_PATH,
    Tenant,
    load_plans,
    load_tenants,
)
from backend.readmodel import dbread

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


SESSION_MAX_AGE = 14 * 24 * 60 * 60  # 14 days

LOGIN_FAILED = "That username and password do not match. Please try again."
LOGIN_THROTTLED = (
    "There have been too many failed sign-in attempts for this username. "
    "Please wait 15 minutes and try again."
)


FormText = Annotated[str, Form()]
OptionalFormText = Annotated[str, Form(max_length=2000)]


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})


def _request_host(scope: Scope) -> str:
    """The Host header's name, lower-cased, without the port."""
    for name, value in scope.get("headers", ()):
        if name == b"host":
            host = bytes(value).decode("latin-1").strip().lower()
            if host.startswith("["):  # [v6]:port
                return host.split("]", 1)[0] + "]"
            return host.rsplit(":", 1)[0] if ":" in host else host
    return ""


def _is_loopback_request(scope: Scope) -> bool:
    """True only for a browser talking to the board on loopback directly (the SSH tunnel), never
    for anything that came through a proxy that says it forwarded the request."""
    if any(name in (b"x-forwarded-proto", b"x-forwarded-host") for name, _ in scope.get("headers", ())):
        return False
    return _request_host(scope) in LOOPBACK_HOSTS


class SessionsByHost:
    """Signed session cookie: HttpOnly, SameSite=Lax, and Secure for every request except one
    whose Host is localhost or 127.0.0.1 (any port). The board always sees plain HTTP (behind
    Docker and tailscale serve), so the scheme cannot decide this. Safari refuses a Secure cookie
    from http://localhost, so the SSH tunnel needs it plain; the tailnet entry always gets Secure.
    Both halves share one key, so a cookie made by either is accepted by both."""

    def __init__(self, app: ASGIApp, secret_key: str, max_age: int) -> None:
        common = {"secret_key": secret_key, "max_age": max_age, "same_site": "lax"}
        self._secure = SessionMiddleware(app, https_only=True, **common)  # type: ignore[arg-type]
        self._loopback = SessionMiddleware(app, https_only=False, **common)  # type: ignore[arg-type]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        inner = self._loopback if _is_loopback_request(scope) else self._secure
        await inner(scope, receive, send)


CROSS_SITE_LOGIN = (
    "This sign-in came from another website, so it was refused. "
    "Open the dashboard's own address and sign in there."
)
CROSS_SITE_LOGOUT = "This sign-out came from another website, so it was refused."
CROSS_SITE_CHAT = "This question came from another website, so it was refused."
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _strip_default_port(netloc: str, scheme: str | None) -> str:
    netloc = netloc.strip().lower()
    for sch, port in _DEFAULT_PORTS.items():
        if (scheme is None or scheme == sch) and netloc.endswith(":" + port):
            return netloc[: -len(port) - 1]
    return netloc


def same_origin(request: Request) -> bool:
    """Cross-site request check for the sign-in and sign-out forms (CSRF).

    The browser's Origin header (or, when it is missing, the Referer) must name the address the
    request was sent to: X-Forwarded-Host when a proxy such as tailscale serve supplied one,
    otherwise Host. Another site's page cannot set either header on a form post. A request with
    neither Origin nor Referer (not a browser form) is allowed; "Origin: null" is refused.
    The scheme is ignored, because the board always sees plain HTTP behind tailscale serve.
    """
    source = request.headers.get("origin")
    if source is None:
        source = request.headers.get("referer")
        if source is None:
            return True
    if source.strip().lower() == "null":
        return False
    parts = urlsplit(source.strip())
    if not parts.scheme or not parts.netloc:
        return False
    forwarded = request.headers.get("x-forwarded-host")
    target = forwarded.split(",")[0] if forwarded else request.headers.get("host", "")
    if not target.strip():
        return False
    return _strip_default_port(parts.netloc, parts.scheme) == _strip_default_port(target, None)


class _LoginRedirect(Exception):
    """Unauthenticated request to a protected route -> bounce to /login."""


class _ClientRedirect(Exception):
    """Client-tenant session on an operator-only page route -> bounce to the client layout."""


def create_app(
    *,
    db_path: Path | None = None,
    tenants_path: Path = TENANTS_PATH,
    plans_path: Path = PLANS_PATH,
    config_dir: Path | None = None,
) -> FastAPI:
    """Build the board. Refuses to start (accounts.ConfigError) without a session secret of at
    least 32 characters and a usable accounts.yaml in the configuration folder."""
    config_dir = config_dir or accounts.config_dir()
    key = accounts.read_session_secret(config_dir)
    tenants = load_tenants(tenants_path)
    account_store = accounts.AccountStore(config_dir / accounts.ACCOUNTS_FILE, tenants.keys())

    # The same env contract the projector honours (FACTORY_DASH_DB), so the composed container
    # points BOTH processes at the one mounted read-model; bare/dev runs keep the repo-root default.
    env_db = os.environ.get("FACTORY_DASH_DB")
    db_path = db_path or (Path(env_db) if env_db else BASE_DIR / "readmodel.db")
    load_plans(plans_path)  # validated at boot; mirrored by the projector at S4
    db.init_db(db_path, tenants.values())

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # Fence 6: autoescape ON for every event-derived string.
    templates.env.autoescape = select_autoescape(default=True, default_for_string=True)

    app = FastAPI(title="factory-dashboard", docs_url=None, redoc_url=None)
    app.add_middleware(SessionsByHost, secret_key=key, max_age=SESSION_MAX_AGE)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.state.db_path = db_path
    app.state.tenants = tenants
    app.state.accounts = account_store
    app.state.login_throttle = accounts.LoginThrottle()
    app.state.templates = templates
    # The chat (design requirement 9): one shared instance, so its one-at-a-time rule and its
    # hourly limit hold across every request and every account. Part E's voice endpoint calls the
    # same `app.state.chat.ask(question, user)`.
    app.state.chat = Chat(db_path)
    # Voice (requirement 10): speech-to-text, then the same chat, then text-to-speech.
    app.state.voice = voice.Voice()

    _register_exception_handlers(app)
    _register_routes(app)
    _register_chat_routes(app)
    _register_voice_routes(app)
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
    """The signed-in account, looked up again in accounts.yaml (a removed account is signed out)."""
    username = request.session.get("username")
    if not username:
        return None
    store: accounts.AccountStore = request.app.state.accounts
    account = store.get(str(username))
    if account is None:
        return None
    user = auth.ResolvedUser(
        username=account.username, display_name=account.display_name, tenant_slug=account.tenant
    )
    request.state.user = user
    return user


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


def _render(
    request: Request, name: str, context: dict[str, object], status_code: int = 200
) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    ctx: dict[str, object] = {"nav": NAV, "signed_in": getattr(request.state, "user", None), **context}
    # base.html needs the page chrome (the read-time chip and the stale-data banner). Most views
    # carry it; surface it uniformly so every page renders it without repetition.
    if "chrome" not in ctx:
        view = ctx.get("view")
        if view is not None and hasattr(view, "chrome"):
            ctx["chrome"] = view.chrome
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _sign_in(request: Request, username: str, password: str) -> Response:
    """Check the password against accounts.yaml, behind the per-username throttle. The attempt is
    counted (atomically, before the slow hash) so parallel guesses cannot overrun the limit; a
    locked username is refused before its password is checked, even when the password is right."""
    username = username.strip()
    throttle: accounts.LoginThrottle = request.app.state.login_throttle
    if not throttle.try_attempt(username):
        return _render(request, "login.html", {"error": LOGIN_THROTTLED}, status_code=429)
    store: accounts.AccountStore = request.app.state.accounts
    account = store.authenticate(username, password)
    if account is None:
        return _render(request, "login.html", {"error": LOGIN_FAILED}, status_code=401)
    throttle.record_success(username)
    request.session.clear()  # a fresh session on every sign-in
    request.session["username"] = account.username
    return RedirectResponse("/", status_code=303)


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
        username: FormText,
        password: FormText,
    ) -> Response:
        if not same_origin(request):
            return _render(request, "login.html", {"error": CROSS_SITE_LOGIN}, status_code=403)
        return _sign_in(request, username, password)

    @app.post("/logout")
    def logout(request: Request) -> Response:
        # POST only, and same-origin only: a link or another site's form cannot sign anyone out.
        if not same_origin(request):
            return HTMLResponse(CROSS_SITE_LOGOUT, status_code=403)
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
    def features(request: Request, user: OperatorPage, days: str | None = None) -> HTMLResponse:
        # ?days=7 (default), ?days=30, or ?days=all.
        window: int | None = 7
        if days == "all":
            window = None
        elif days and days.isdigit() and int(days) in (7, 30):
            window = int(days)
        return _render(
            request,
            "features.html",
            {"view": dbread.features_view(request.app.state.db_path, window), "nav_active": "features",
             "tenant": _tenant(request, user)},
        )

    @app.get("/features/{feature_key}", response_class=HTMLResponse)
    def feature(request: Request, feature_key: str, user: OperatorPage) -> HTMLResponse:
        found = dbread.feature_page(request.app.state.db_path, feature_key)
        if found is None:
            return _render(
                request,
                "feature_missing.html",
                {"feature_id": feature_key, "nav_active": "features", "tenant": _tenant(request, user),
                 "chrome": dbread.chrome_view(request.app.state.db_path)},
            )
        chrome, page = found
        return _render(
            request,
            "feature.html",
            {"page": page, "chrome": chrome, "nav_active": "features", "tenant": _tenant(request, user)},
        )

    @app.get("/projects/{owner}/{name}", response_class=HTMLResponse)
    def project(request: Request, owner: str, name: str, user: OperatorPage) -> HTMLResponse:
        # Projects are named owner/name (the build's repository), so the route takes both parts.
        return _render(
            request,
            "project.html",
            {"view": dbread.project_view(request.app.state.db_path, f"{owner}/{name}"), "nav_active": "",
             "tenant": _tenant(request, user)},
        )

    @app.get("/projects/{name}", response_class=HTMLResponse)
    def project_single(request: Request, name: str, user: OperatorPage) -> HTMLResponse:
        return _render(
            request,
            "project.html",
            {"view": dbread.project_view(request.app.state.db_path, name), "nav_active": "",
             "tenant": _tenant(request, user)},
        )

    @app.get("/delivered", response_class=HTMLResponse)
    def delivered(
        request: Request,
        user: OperatorPage,
        from_: Annotated[str | None, Query(alias="from")] = None,
        to: str | None = None,
    ) -> HTMLResponse:
        # Two date inputs; a missing or malformed window falls back to this week.
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
            {"view": dbread.issues_view(request.app.state.db_path), "nav_active": "issues",
             "tenant": _tenant(request, user)},
        )

    @app.get("/fleet", response_class=HTMLResponse)
    def fleet(request: Request, user: OperatorPage) -> HTMLResponse:
        return _render(
            request,
            "fleet.html",
            {"view": dbread.fleet_view(request.app.state.db_path), "nav_active": "fleet",
             "tenant": _tenant(request, user)},
        )

    @app.get("/healthz")
    def healthz(request: Request) -> JSONResponse:
        # For the start script and the release runbook's last check. No login: it says only whether
        # the dashboard is reading the factory's record, and when it last did.
        return JSONResponse(dbread.health(request.app.state.db_path))

    # --- live-update fragments (refetched when the feed records a change) ---

    @app.get("/fragments/{panel}", response_class=HTMLResponse)
    def fragment(
        request: Request,
        panel: str,
        user: OperatorApi,
        from_: Annotated[str | None, Query(alias="from")] = None,
        to: str | None = None,
    ) -> HTMLResponse:
        if panel not in _FRAGMENT_TEMPLATES:
            raise HTTPException(status_code=404, detail="unknown panel")
        # The Delivered panel keeps the window of the page it sits on.
        window = (from_, to) if from_ and to else None
        view = dbread.panel_view(request.app.state.db_path, panel, window=window)
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


def _register_chat_routes(app: FastAPI) -> None:
    """The chat (design requirement 9): operator accounts only."""

    @app.get("/chat", response_class=HTMLResponse)
    def chat_page(request: Request, user: OperatorPage) -> HTMLResponse:
        # The full-page chat, for narrow screens; wide screens have the same panel in a side drawer.
        return _render(request, "chat.html", {"exchanges": [], "chat_page": True, "nav_active": "",
                                              "chrome": dbread.chrome_view(request.app.state.db_path),
                                              "tenant": _tenant(request, user)})

    @app.post("/chat", response_class=HTMLResponse)
    def chat_ask(
        request: Request,
        user: OperatorApi,
        question: OptionalFormText = "",
        prior_question: OptionalFormText = "",
        prior_clarify: OptionalFormText = "",
    ) -> HTMLResponse:
        # Same-origin only, like sign-in and sign-out: another site's page cannot spend the chat's
        # model calls in a signed-in browser.
        if not same_origin(request):
            return HTMLResponse(CROSS_SITE_CHAT, status_code=403)
        follow_up = FollowUp(prior_question, prior_clarify) if prior_question and prior_clarify else None
        chat: Chat = request.app.state.chat
        answer = chat.ask(question, user, follow_up=follow_up)
        exchange = {"question": " ".join(question.split()), "answer": answer}
        if request.headers.get("hx-request") == "true":
            return _render(request, "fragments/chat_exchange.html", exchange)
        # Without scripts the form posts here and gets the whole page back, answer included.
        return _render(request, "chat.html", {"exchanges": [exchange], "chat_page": True, "nav_active": "",
                                              "chrome": dbread.chrome_view(request.app.state.db_path),
                                              "tenant": _tenant(request, user)})


def _voice_json(reply: voice.VoiceReply, templates: Jinja2Templates) -> JSONResponse:
    """One JSON shape for the browser and, later, the robot: what was heard, the answer (text and
    links), the WAV read aloud (base64) and, for the browser, the chat's own answer fragment."""
    if reply.answer is None:
        return JSONResponse({"message": reply.message}, status_code=reply.status)
    a = reply.answer
    html = templates.get_template("fragments/chat_exchange.html").render(
        question=reply.transcript, answer=a, how="Spoken", voice_note=reply.voice_note)
    return JSONResponse({
        "transcript": reply.transcript,
        "answer": {
            "text": a.text, "kind": a.kind,
            "refs": [{"kind": r.kind, "label": r.label, "href": r.href} for r in a.refs],
            "follow_up": ({"question": a.follow_up.question, "clarify": a.follow_up.clarify}
                          if a.follow_up else None),
        },
        "spoken": reply.spoken,
        "audio": ({"content_type": "audio/wav", "base64": base64.b64encode(reply.audio).decode("ascii")}
                  if reply.audio else None),
        "voice_note": reply.voice_note,
        "html": html,
    })


def _register_voice_routes(app: FastAPI) -> None:
    """Voice (design requirement 10): the chat's guards, plus limits on the recording."""

    @app.post("/api/voice/ask")
    async def voice_ask(request: Request, user: OperatorApi) -> JSONResponse:
        # The same guards as POST /chat: operators only (401/403 above), same-origin only. The body
        # is read only after the guards and the length check, and never beyond the limit.
        if not same_origin(request):
            return JSONResponse({"message": voice.CROSS_SITE}, status_code=403)
        length = request.headers.get("content-length", "")
        if not length.isdigit():
            return JSONResponse({"message": voice.NO_LENGTH}, status_code=411)
        if int(length) > voice.MAX_BODY_BYTES:
            return JSONResponse({"message": voice.TOO_BIG}, status_code=413)
        try:
            form = await request.form(max_files=1, max_fields=3, max_part_size=4096)
        except StarletteHTTPException:  # a malformed form
            return JSONResponse({"message": voice.NO_RECORDING}, status_code=400)
        try:
            upload = form.get("audio")
            if not isinstance(upload, UploadFile):
                return JSONResponse({"message": voice.NO_RECORDING}, status_code=400)
            audio = await upload.read(voice.MAX_WAV_BYTES + 1)
            content_type = upload.content_type or ""
            prior_q, prior_c = str(form.get("prior_question") or ""), str(form.get("prior_clarify") or "")
        finally:
            await form.close()
        follow_up = FollowUp(prior_q, prior_c) if prior_q and prior_c else None
        reply = await run_in_threadpool(voice.ask_by_voice, request.app.state.chat, request.app.state.voice,
                                        audio, content_type, user, follow_up)
        return _voice_json(reply, request.app.state.templates)


_FRAGMENT_TEMPLATES: dict[str, str] = {
    "p2": "in_progress.html",
    "p3": "activity.html",
    "p4": "needs_you.html",
    "p5": "planning.html",
    "p6": "services.html",
    "p7": "delivered.html",
    "proj": "feed.html",
}


# The ASGI app for `uvicorn backend.app:app` in production, built on first access (PEP 562) so
# importing this module (tests, tools) does not need the configuration folder. A missing or short
# session secret, or a missing accounts file, stops the board with one plain sentence.
@functools.cache
def _production_app() -> FastAPI:
    try:
        return create_app()
    except accounts.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


def __getattr__(name: str) -> FastAPI:
    if name != "app":
        raise AttributeError(name)
    return _production_app()
