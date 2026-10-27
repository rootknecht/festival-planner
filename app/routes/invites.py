"""Accepting an invitation.

Two cases:
- the address has no account yet: the invitee sets a display name and a
  password, and the new account joins the festival;
- the address already has an account: accepting only adds that account to the
  festival, and the invitee signs in as usual.

If the invitation claims a placeholder, the placeholder row is upgraded in
place — car seats and bring-list cells keep pointing at the same participant.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.deps import get_db
from app.models.festival import Invite, Participant, ParticipantRole
from app.models.user import User, utcnow
from app.routes.auth import set_session_cookie
from app.services.passwords import PasswordTooShort, hash_password, validate_password
from app.services.sessions import create_session
from app.services.tokens import hash_token
from app.templating import render

router = APIRouter(tags=["invites"])


def _settings_for(request: Request):
    return getattr(request.app.state, "settings", None) or get_settings()


def _load_invite(db: DbSession, token: str) -> Invite | None:
    invite = db.scalar(select(Invite).where(Invite.token_hash == hash_token(token)))
    if invite is None or not invite.is_open:
        return None
    expires_at = invite.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.UTC)
    if expires_at <= utcnow():
        return None
    return invite


def _existing_user(db: DbSession, email: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))


@router.get("/invite/{token}")
async def invite_form(request: Request, token: str, db: DbSession = Depends(get_db)) -> Response:
    invite = _load_invite(db, token)
    if invite is None:
        return render(request, "invites/invalid.html", {}, status_code=410)
    return render(
        request,
        "invites/accept.html",
        {
            "token": token,
            "festival": invite.festival,
            "email": invite.email,
            "has_account": _existing_user(db, invite.email) is not None,
            "error": None,
        },
    )


@router.post("/invite/{token}")
async def accept_invite(
    request: Request,
    token: str,
    display_name: str = Form(default=""),
    password: str = Form(default=""),
    password_repeat: str = Form(default=""),
    db: DbSession = Depends(get_db),
) -> Response:
    settings = _settings_for(request)
    invite = _load_invite(db, token)
    if invite is None:
        return render(request, "invites/invalid.html", {}, status_code=410)

    user = _existing_user(db, invite.email)
    created = False

    if user is None:
        if password != password_repeat:
            return render(
                request,
                "invites/accept.html",
                {
                    "token": token,
                    "festival": invite.festival,
                    "email": invite.email,
                    "has_account": False,
                    "error": "The two passwords do not match.",
                },
                status_code=400,
            )
        try:
            validate_password(password)
        except PasswordTooShort as exc:
            return render(
                request,
                "invites/accept.html",
                {
                    "token": token,
                    "festival": invite.festival,
                    "email": invite.email,
                    "has_account": False,
                    "error": str(exc).capitalize() + ".",
                },
                status_code=400,
            )
        user = User(
            email=invite.email,
            password_hash=hash_password(password),
            display_name=(display_name.strip() or invite.email.split("@")[0])[:120],
        )
        db.add(user)
        db.flush()
        created = True

    # Claim a placeholder if the invitation points at one; otherwise add a
    # fresh participant unless this account is already in the festival.
    participant = db.scalar(
        select(Participant).where(
            Participant.festival_id == invite.festival_id,
            Participant.user_id == user.id,
        )
    )
    if participant is None and invite.claims_participant_id:
        placeholder = db.get(Participant, invite.claims_participant_id)
        if placeholder is not None and placeholder.is_placeholder:
            placeholder.user_id = user.id
            if display_name.strip():
                placeholder.display_name = display_name.strip()[:120]
            participant = placeholder
    if participant is None:
        participant = Participant(
            festival_id=invite.festival_id,
            user_id=user.id,
            display_name=user.display_name,
            role=ParticipantRole.MEMBER.value,
        )
        db.add(participant)

    invite.accepted_at = utcnow()
    db.commit()

    if not created:
        # Existing account: no silent sign-in from a mailed link.
        return RedirectResponse("/login", status_code=303)

    session_token, _ = create_session(
        db,
        user,
        lifetime=settings.session_lifetime,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    response = RedirectResponse(f"/festivals/{invite.festival_id}", status_code=303)
    set_session_cookie(
        response,
        session_token,
        secure=request.url.scheme == "https",
        lifetime=settings.session_lifetime,
    )
    return response
