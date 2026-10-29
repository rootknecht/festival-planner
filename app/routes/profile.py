"""User profile: name, language, theme, password, two-factor."""

from __future__ import annotations

import io

import pyotp
import qrcode
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from qrcode.image.svg import SvgPathImage
from sqlalchemy.orm import Session as DbSession

from app.deps import get_db, require_user
from app.models.festival import THEMES
from app.models.user import User
from app.services.passwords import (
    PasswordTooShort,
    hash_password,
    validate_password,
    verify_password,
)
from app.services.sessions import revoke_all_sessions
from app.templating import render
from app.translations import SUPPORTED_LANGUAGES

router = APIRouter(tags=["profile"])

# One source of truth with the festival themes: a theme added to the model's
# THEMES is immediately offerable as a personal override too, and the two
# lists can no longer drift (this one was already missing nothing less than
# the renamed default).


def _page(request: Request, user: User, **extra):
    return render(
        request,
        "profile/index.html",
        {
            "themes": THEMES,
            "supported_languages": SUPPORTED_LANGUAGES,
            "totp_enabled": user.has_totp,
            **extra,
        },
    )


@router.get("/profile")
async def profile(request: Request, user: User = Depends(require_user)) -> Response:
    return _page(request, user, notice=None, error=None)


@router.post("/profile")
async def save_profile(
    request: Request,
    display_name: str = Form(...),
    language: str = Form(default="en"),
    theme_override: str = Form(default=""),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    name = display_name.strip()[:120]
    if name:
        user.display_name = name
    if language in SUPPORTED_LANGUAGES:
        user.language = language
    user.theme_override = theme_override if theme_override in THEMES else None
    db.commit()
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/language")
async def switch_language(
    request: Request,
    language: str = Form(...),
    next: str = Form(default="/"),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    """The title-bar language toggle: switch and come back to the same page."""
    if language in SUPPORTED_LANGUAGES:
        user.language = language
        db.commit()
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(target, status_code=303)


@router.post("/profile/password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    password: str = Form(...),
    password_repeat: str = Form(...),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    if not verify_password(user.password_hash, current_password):
        return _page(request, user, notice=None, error="Your current password is not correct.")
    if password != password_repeat:
        return _page(request, user, notice=None, error="The two new passwords do not match.")
    try:
        validate_password(password)
    except PasswordTooShort as exc:
        return _page(request, user, notice=None, error=str(exc).capitalize() + ".")

    user.password_hash = hash_password(password)
    db.commit()
    revoke_all_sessions(db, user)
    return RedirectResponse("/login", status_code=303)


@router.get("/profile/totp")
async def totp_setup(
    request: Request, user: User = Depends(require_user), db: DbSession = Depends(get_db)
) -> Response:
    if user.has_totp:
        return RedirectResponse("/profile", status_code=303)
    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="Festival Planner")
    return render(
        request,
        "profile/totp.html",
        {"secret": secret, "uri": uri, "error": None},
    )


@router.get("/profile/totp/qr")
async def totp_qr(uri: str, user: User = Depends(require_user)) -> Response:
    """Render the provisioning URI as a QR code.

    Drawn locally as SVG — the secret never leaves the server to a chart
    service, and SVG avoids pulling an image library into the container.
    """
    if not uri.startswith("otpauth://"):
        raise HTTPException(status_code=400, detail="not a provisioning uri")
    buffer = io.BytesIO()
    qrcode.make(uri, image_factory=SvgPathImage).save(buffer)
    return Response(
        content=buffer.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/profile/totp")
async def totp_enable(
    request: Request,
    secret: str = Form(...),
    code: str = Form(...),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="Festival Planner")
        return render(
            request,
            "profile/totp.html",
            {"secret": secret, "uri": uri, "error": "That code is not valid. Try the next one."},
            status_code=400,
        )
    user.totp_secret = secret
    db.commit()
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/totp/disable")
async def totp_disable(
    request: Request,
    password: str = Form(...),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    if not verify_password(user.password_hash, password):
        return _page(request, user, notice=None, error="Your password is not correct.")
    user.totp_secret = None
    db.commit()
    return RedirectResponse("/profile", status_code=303)
