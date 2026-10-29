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


def _set_token_cookie(response: Response, token: str, *, secure: bool) -> None:
    from app.config import get_settings

    response.set_cookie(
        CSRF_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
        # Matched to the session lifetime. These used to disagree — the cookie
        # lasted a day and the session a month — so a tab left open overnight
        # came back with a form whose token had no cookie behind it, and the
        # first thing the user pressed failed.
        max_age=get_settings().session_lifetime,
    )


def _rejection(request: Request, token: str) -> Response:
    """Explain the failure in the language of whoever is asking.

    A stale token is a routine event: a tab left open, a browser that cleared
    its data, a session resumed days later. The response has to be recoverable
    rather than correct-and-useless, and it carries a FRESH cookie so the
    retry actually succeeds instead of looping.
    """
    from app.templating import templates
    from app.translations import gettext_for, normalise

    secure = request.url.scheme == "https"

    # HTMX swaps fragments into a live page, so an error page would be
    # inserted into the layout. Telling the client to reload gets it a fresh
    # token and puts the user back where they were.
    if request.headers.get("hx-request", "").lower() == "true":
        response = Response(status_code=403, headers={"HX-Refresh": "true"})
        _set_token_cookie(response, token, secure=secure)
        return response

    accept = request.headers.get("accept", "")
    if "text/html" not in accept:
        response = JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
        _set_token_cookie(response, token, secure=secure)
        return response

    language = normalise(
        request.cookies.get("fp_lang") or request.headers.get("accept-language", "")
    )
    _ = gettext_for(language)
    response = templates.TemplateResponse(
        request,
        "csrf.html",
        {
            "language": language,
            "heading": _("Please try that again"),
            "explanation": _(
                "The security token on that form had expired, so the app refused it. "
                "This happens when a page has been open for a long time, or after "
                "the browser has cleared its data."
            ),
            "retry_url": request.headers.get("referer") or "/",
            "retry_label": _("Reload the page"),
            "reassurance": _("Nothing was changed and nothing was lost."),
        },
        status_code=403,
    )
    _set_token_cookie(response, token, secure=secure)
    return response


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
                # Issue a NEW token with the rejection: the old one is either
                # gone or untrusted, and without this the retry fails too.
                return _rejection(request, new_csrf_token())

        response = await call_next(request)
        if not cookie_token:
            _set_token_cookie(response, issue_token, secure=request.url.scheme == "https")
        return response
