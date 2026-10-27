"""Authentication routes: login, logout."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.deps import current_user, get_db
from app.models.user import User, utcnow
from app.services import login_throttle
from app.services.passwords import hash_password, needs_rehash, verify_password
from app.services.sessions import COOKIE_NAME, create_session, revoke_session
from app.templating import render

router = APIRouter(tags=["auth"])

GENERIC_LOGIN_ERROR = "Email address or password is not correct."
LOCKED_ERROR = "Too many failed attempts. Please try again in a few minutes."


def set_session_cookie(response: Response, token: str, secure: bool, lifetime: int) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
        max_age=lifetime,
    )


def _settings_for(request: Request):
    return getattr(request.app.state, "settings", None) or get_settings()


@router.get("/login")
async def login_form(request: Request, user: User | None = Depends(current_user)) -> Response:
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return render(request, "auth/login.html", {"error": None})


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    totp: str = Form(default=""),
    db: DbSession = Depends(get_db),
) -> Response:
    settings = _settings_for(request)
    identity = email.strip().lower()

    if login_throttle.is_locked(db, identity):
        return render(request, "auth/login.html", {"error": LOCKED_ERROR}, status_code=429)

    user = db.scalar(select(User).where(func.lower(User.email) == identity))
    authenticated = user is not None and user.is_active and verify_password(
        user.password_hash, password
    )

    if authenticated and user is not None and user.has_totp:
        import pyotp

        authenticated = bool(totp) and pyotp.TOTP(user.totp_secret).verify(totp, valid_window=1)

    if not authenticated or user is None:
        login_throttle.record_failure(db, identity)
        if settings.environment != "test":
            await asyncio.sleep(
                login_throttle.delay_for(login_throttle.recent_failures(db, identity))
            )
        return render(request, "auth/login.html", {"error": GENERIC_LOGIN_ERROR}, status_code=401)

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    login_throttle.record_success(db, identity)
    user.last_login_at = utcnow()
    db.commit()

    token, _ = create_session(
        db,
        user,
        lifetime=settings.session_lifetime,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(
        response, token, secure=request.url.scheme == "https", lifetime=settings.session_lifetime
    )
    return response


@router.post("/logout")
async def logout(request: Request, db: DbSession = Depends(get_db)) -> Response:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        revoke_session(db, token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
