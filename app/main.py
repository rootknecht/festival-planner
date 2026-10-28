"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app import __version__
from app.config import Settings, get_settings
from app.routes import auth as auth_routes
from app.routes import bringlist as bringlist_routes
from app.routes import chat as chat_routes
from app.routes import expenses as expense_routes
from app.routes import festivals as festival_routes
from app.routes import forum as forum_routes
from app.routes import invites as invite_routes
from app.routes import password_reset as reset_routes
from app.routes import polls as poll_routes
from app.routes import profile as profile_routes
from app.routes import pwa as pwa_routes
from app.routes import transport as transport_routes
from app.security.csrf import CsrfMiddleware
from app.templating import render

STATIC_DIR = Path(__file__).parent / "static"

# Strict by default. Map tiles and the service worker are added explicitly
# when those features land; nothing here permits inline script or style.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        # OpenStreetMap tiles are the single external origin this app talks to.
        # Both forms are needed: a wildcard does NOT match the bare host, and
        # the bare host is what the tile URL uses.
        "img-src 'self' data: https://tile.openstreetmap.org https://*.tile.openstreetmap.org",
        "style-src 'self'",
        "script-src 'self'",
        "connect-src 'self'",
    ]
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="Festival Planner",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app.add_middleware(BaseHTTPMiddleware, dispatch=CsrfMiddleware())
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(auth_routes.router)
    app.include_router(reset_routes.router)
    app.include_router(invite_routes.router)
    app.include_router(profile_routes.router)
    app.include_router(pwa_routes.router)
    app.include_router(bringlist_routes.router)
    app.include_router(expense_routes.router)
    app.include_router(transport_routes.router)
    app.include_router(poll_routes.router)
    app.include_router(chat_routes.router)
    app.include_router(forum_routes.router)
    app.include_router(festival_routes.router)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
        # A browser asking for a page should land on the login form, not on a
        # JSON error body.
        accept = request.headers.get("accept", "")
        # Treat anything that is not an explicit API/JSON request as a browser.
        wants_html = "text/html" in accept or accept.strip() in ("", "*/*")
        if exc.status_code == 401 and wants_html:
            return RedirectResponse("/login", status_code=303)
        if wants_html:
            return render(
                request,
                "error.html",
                {"status_code": exc.status_code, "detail": exc.detail},
                status_code=exc.status_code,
            )
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()
