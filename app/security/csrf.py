"""CSRF protection.

Double-submit with an HttpOnly cookie: the server renders the token into every
form and into the HTMX header set, then compares it against the cookie on every
unsafe request. Because the cookie is HttpOnly, injected third-party script
cannot read it, and because the value is compared server-side, JavaScript is
not required for forms to work.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

CSRF_COOKIE = "fp_csrf"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
EXEMPT_PATHS = frozenset({"/healthz"})


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


async def _submitted_token(request: Request) -> str | None:
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        # Read the body once and put it back, otherwise the route handler
        # downstream finds an already-consumed stream and fails with 422.
        body = await request.body()

        async def replay() -> dict[str, object]:
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = replay  # noqa: SLF001
        form = await request.form()
        value = form.get(CSRF_FIELD)
        request._form = None  # noqa: SLF001
        if isinstance(value, str):
            return value
    return None


class CsrfMiddleware:
    """Starlette-style middleware callable for use with app.middleware('http')."""

    async def __call__(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        cookie_token = request.cookies.get(CSRF_COOKIE)
        issue_token = cookie_token or new_csrf_token()
        request.state.csrf_token = issue_token

        if request.method not in SAFE_METHODS and request.url.path not in EXEMPT_PATHS:
            submitted = await _submitted_token(request)
            if not cookie_token or not submitted or not hmac.compare_digest(
                cookie_token, submitted
            ):
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)

        response = await call_next(request)
        if not cookie_token:
            response.set_cookie(
                CSRF_COOKIE,
                issue_token,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
                path="/",
                max_age=60 * 60 * 24,
            )
        return response
