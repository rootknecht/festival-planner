"""Password reset: request a link, then set a new password."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.deps import get_db
from app.models.password_reset import PasswordReset
from app.models.user import User, utcnow
from app.services import branding
from app.services.mail import Message, get_mailer
from app.services.passwords import PasswordTooShort, hash_password, validate_password
from app.services.sessions import revoke_all_sessions
from app.services.tokens import generate_token, hash_token
from app.templating import render

router = APIRouter(tags=["auth"])

# Always the same answer, so the form cannot be used to find out which email
# addresses have an account here.
GENERIC_SENT = (
    "If that address belongs to an account, a reset link is on its way. "
    "The link is valid for a short time only."
)


def _settings_for(request: Request):
    return getattr(request.app.state, "settings", None) or get_settings()


@router.get("/forgot")
async def forgot_form(request: Request) -> Response:
    return render(request, "auth/forgot.html", {"notice": None})


@router.post("/forgot")
async def forgot(
    request: Request,
    email: str = Form(...),
    db: DbSession = Depends(get_db),
) -> Response:
    settings = _settings_for(request)
    identity = email.strip().lower()
    user = db.scalar(select(User).where(func.lower(User.email) == identity))

    if user is not None and user.is_active:
        token = generate_token()
        db.add(
            PasswordReset(
                user_id=user.id,
                token_hash=hash_token(token),
                expires_at=utcnow() + dt.timedelta(seconds=settings.reset_lifetime),
            )
        )
        db.commit()
        # No festival context here by design — the address may belong to
        # someone in festivals on several domains, or none, and this route
        # must not reveal which. The domain they are actually looking at is
        # the honest brand, and it is allowlisted inside brand_for_host so a
        # forged Host cannot redirect the link or forge its sender.
        brand = branding.brand_for_host(settings, request.headers.get("host"))
        link = f"{brand.base_url}/reset/{token}"
        get_mailer(settings).send(
            Message(
                from_=brand.sender,
                to=user.email,
                subject="Festival Planner — reset your password",
                body=(
                    f"Hello {user.display_name},\n\n"
                    f"Open this link to choose a new password:\n{link}\n\n"
                    "If you did not ask for this, ignore this message — "
                    "your password stays unchanged.\n"
                ),
            )
        )

    return render(request, "auth/forgot.html", {"notice": GENERIC_SENT})


def _load_reset(db: DbSession, token: str) -> PasswordReset | None:
    record = db.scalar(select(PasswordReset).where(PasswordReset.token_hash == hash_token(token)))
    if record is None or record.used_at is not None:
        return None
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.UTC)
    if expires_at <= utcnow():
        return None
    return record


@router.get("/reset/{token}")
async def reset_form(request: Request, token: str, db: DbSession = Depends(get_db)) -> Response:
    if _load_reset(db, token) is None:
        return render(request, "auth/reset_invalid.html", {}, status_code=410)
    return render(request, "auth/reset.html", {"token": token, "error": None})


@router.post("/reset/{token}")
async def reset(
    request: Request,
    token: str,
    password: str = Form(...),
    password_repeat: str = Form(...),
    db: DbSession = Depends(get_db),
) -> Response:
    record = _load_reset(db, token)
    if record is None:
        return render(request, "auth/reset_invalid.html", {}, status_code=410)

    if password != password_repeat:
        return render(
            request,
            "auth/reset.html",
            {"token": token, "error": "The two passwords do not match."},
            status_code=400,
        )
    try:
        validate_password(password)
    except PasswordTooShort as exc:
        return render(
            request,
            "auth/reset.html",
            {"token": token, "error": str(exc).capitalize() + "."},
            status_code=400,
        )

    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        return render(request, "auth/reset_invalid.html", {}, status_code=410)

    user.password_hash = hash_password(password)
    record.used_at = utcnow()
    db.commit()
    # A password change logs every device out — that is the point of being
    # able to reset it after a compromise.
    revoke_all_sessions(db, user)
    return RedirectResponse("/login", status_code=303)
