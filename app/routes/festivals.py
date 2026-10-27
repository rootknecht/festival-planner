"""Festival lifecycle: create, view, edit, participants, invitations, archive."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.deps import get_db, require_user
from app.models.festival import (
    Festival,
    FestivalStatus,
    Invite,
    Participant,
    ParticipantRole,
)
from app.models.user import User, utcnow
from app.services import notifications
from app.services.access import (
    count_admins,
    festivals_for,
    require_admin,
    require_membership,
    require_writable,
)
from app.services.geocode import geocode
from app.services.mail import Message, get_mailer
from app.services.notification_text import describe
from app.services.tokens import generate_token, hash_token
from app.templating import language_for, render
from app.translations import gettext_for

router = APIRouter(tags=["festivals"])


def _settings_for(request: Request):
    return getattr(request.app.state, "settings", None) or get_settings()


def _date(value: str) -> dt.date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid date") from exc


def _clean(value: str | None, limit: int = 2000) -> str | None:
    value = (value or "").strip()
    return value[:limit] or None


@router.get("/")
async def festival_list(
    request: Request, user: User = Depends(require_user), db: DbSession = Depends(get_db)
) -> Response:
    rows = festivals_for(db, user)
    return render(request, "festivals/list.html", {"rows": rows})


@router.get("/festivals/new")
async def new_festival_form(request: Request, user: User = Depends(require_user)) -> Response:
    return render(request, "festivals/new.html", {"error": None})


@router.post("/festivals")
async def create_festival(
    request: Request,
    name: str = Form(...),
    website: str = Form(default=""),
    address_text: str = Form(default=""),
    start_date: str = Form(default=""),
    end_date: str = Form(default=""),
    camp_arrival: str = Form(default=""),
    camp_departure: str = Form(default=""),
    timezone: str = Form(default="Europe/Berlin"),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    clean_name = name.strip()
    if not clean_name:
        return render(
            request, "festivals/new.html", {"error": "A festival needs a name."}, status_code=400
        )

    festival = Festival(
        name=clean_name[:160],
        website=_clean(website),
        address_text=_clean(address_text),
        start_date=_date(start_date),
        end_date=_date(end_date),
        camp_arrival=_date(camp_arrival),
        camp_departure=_date(camp_departure),
        timezone=(timezone or "Europe/Berlin")[:64],
        created_by=user.id,
    )
    db.add(festival)
    db.flush()
    db.add(
        Participant(
            festival_id=festival.id,
            user_id=user.id,
            display_name=user.display_name,
            role=ParticipantRole.ADMIN.value,
        )
    )
    db.commit()
    return RedirectResponse(f"/festivals/{festival.id}", status_code=303)


@router.get("/festivals/{festival_id}")
async def festival_dashboard(
    request: Request,
    festival_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_membership(db, user, festival_id)
    participants = db.scalars(
        select(Participant)
        .where(Participant.festival_id == festival_id)
        .order_by(func.lower(Participant.display_name))
    ).all()
    translate = gettext_for(language_for(request))
    blocks = [
        describe(item, translate, festival_id)
        for item in notifications.open_for(db, user.id, festival_id)
    ]
    return render(
        request,
        "festivals/dashboard.html",
        {
            "festival": membership.festival,
            "membership": membership,
            "participants": participants,
            "notifications": blocks,
            "badges": notifications.badges(db, user.id, festival_id),
            "active_tool": "dashboard",
        },
    )


@router.post("/festivals/{festival_id}/notifications/{notification_id}/clear")
async def clear_notification(
    request: Request,
    festival_id: int,
    notification_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_membership(db, user, festival_id)
    if not notifications.clear(db, notification_id, user.id):
        raise HTTPException(status_code=404, detail="not found")
    return RedirectResponse(f"/festivals/{festival_id}", status_code=303)


@router.get("/festivals/{festival_id}/settings")
async def settings_form(
    request: Request,
    festival_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_admin(db, user, festival_id)
    participants = db.scalars(
        select(Participant)
        .where(Participant.festival_id == festival_id)
        .order_by(func.lower(Participant.display_name))
    ).all()
    invites = db.scalars(
        select(Invite)
        .where(Invite.festival_id == festival_id, Invite.accepted_at.is_(None))
        .order_by(Invite.created_at.desc())
    ).all()
    return render(
        request,
        "festivals/settings.html",
        {
            "festival": membership.festival,
            "membership": membership,
            "participants": participants,
            "invites": invites,
            "invite_link": request.query_params.get("link"),
            "error": None,
        },
    )


@router.post("/festivals/{festival_id}/settings")
async def save_settings(
    request: Request,
    festival_id: int,
    name: str = Form(...),
    website: str = Form(default=""),
    address_text: str = Form(default=""),
    start_date: str = Form(default=""),
    end_date: str = Form(default=""),
    camp_arrival: str = Form(default=""),
    camp_departure: str = Form(default=""),
    timezone: str = Form(default="Europe/Berlin"),
    theme: str = Form(default="metal"),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_writable(require_admin(db, user, festival_id))
    festival = membership.festival
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="a festival needs a name")

    festival.name = clean_name[:160]
    festival.website = _clean(website)
    new_address = _clean(address_text)
    if new_address != festival.address_text:
        # Only on an actual change: the venue does not move, and the public
        # geocoder should not be asked again on every unrelated settings save.
        festival.lat, festival.lon = (
            geocode(db, new_address, _settings_for(request)) if new_address else (None, None)
        )
    festival.address_text = new_address
    festival.start_date = _date(start_date)
    festival.end_date = _date(end_date)
    festival.camp_arrival = _date(camp_arrival)
    festival.camp_departure = _date(camp_departure)
    festival.timezone = (timezone or "Europe/Berlin")[:64]
    if theme in {"metal", "matrix", "light"}:
        festival.theme = theme
    db.commit()
    return RedirectResponse(f"/festivals/{festival_id}/settings", status_code=303)


@router.post("/festivals/{festival_id}/participants")
async def add_placeholder(
    request: Request,
    festival_id: int,
    display_name: str = Form(...),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_writable(require_admin(db, user, festival_id))
    clean = display_name.strip()[:120]
    if clean:
        db.add(
            Participant(
                festival_id=membership.festival.id,
                user_id=None,
                display_name=clean,
                role=ParticipantRole.MEMBER.value,
            )
        )
        db.commit()
    return RedirectResponse(f"/festivals/{festival_id}/settings", status_code=303)


@router.post("/festivals/{festival_id}/participants/{participant_id}/role")
async def change_role(
    request: Request,
    festival_id: int,
    participant_id: int,
    role: str = Form(...),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_writable(require_admin(db, user, festival_id))
    participant = db.get(Participant, participant_id)
    if participant is None or participant.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    if participant.is_placeholder:
        raise HTTPException(status_code=400, detail="a placeholder cannot be an admin")
    if role not in {ParticipantRole.ADMIN.value, ParticipantRole.MEMBER.value}:
        raise HTTPException(status_code=400, detail="unknown role")
    if (
        role == ParticipantRole.MEMBER.value
        and participant.is_admin
        and count_admins(db, festival_id) <= 1
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a festival must keep at least one admin",
        )
    participant.role = role
    db.commit()
    return RedirectResponse(f"/festivals/{festival_id}/settings", status_code=303)


@router.post("/festivals/{festival_id}/participants/{participant_id}/delete")
async def remove_participant(
    request: Request,
    festival_id: int,
    participant_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_writable(require_admin(db, user, festival_id))
    participant = db.get(Participant, participant_id)
    if participant is None or participant.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    if participant.is_admin and count_admins(db, festival_id) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a festival must keep at least one admin",
        )
    db.delete(participant)
    db.commit()
    return RedirectResponse(f"/festivals/{festival_id}/settings", status_code=303)


@router.post("/festivals/{festival_id}/invites")
async def create_invite(
    request: Request,
    festival_id: int,
    email: str = Form(...),
    claims_participant_id: str = Form(default=""),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    settings = _settings_for(request)
    membership = require_writable(require_admin(db, user, festival_id))
    address = email.strip().lower()
    if "@" not in address:
        raise HTTPException(status_code=400, detail="that is not an email address")

    claims_id: int | None = None
    if claims_participant_id.strip():
        candidate = db.get(Participant, int(claims_participant_id))
        if candidate is None or candidate.festival_id != festival_id:
            raise HTTPException(status_code=404, detail="not found")
        if not candidate.is_placeholder:
            raise HTTPException(status_code=400, detail="that participant already has an account")
        claims_id = candidate.id

    token = generate_token()
    db.add(
        Invite(
            festival_id=festival_id,
            email=address,
            token_hash=hash_token(token),
            created_by=user.id,
            expires_at=utcnow() + dt.timedelta(seconds=settings.invite_lifetime),
            claims_participant_id=claims_id,
        )
    )
    db.commit()

    link = f"{settings.base_url.rstrip('/')}/invite/{token}"
    get_mailer(settings).send(
        Message(
            to=address,
            subject=f"You are invited to {membership.festival.name}",
            body=(
                f"{user.display_name} invited you to plan "
                f"{membership.festival.name} together.\n\n"
                f"Open this link to join:\n{link}\n\n"
                "The link works once and expires after a few days.\n"
            ),
        )
    )
    # The link is always shown to the admin as well, so an invitation is never
    # lost to a mail problem.
    return RedirectResponse(f"/festivals/{festival_id}/settings?link={token}", status_code=303)


@router.post("/festivals/{festival_id}/invites/{invite_id}/revoke")
async def revoke_invite(
    request: Request,
    festival_id: int,
    invite_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_admin(db, user, festival_id)
    invite = db.get(Invite, invite_id)
    if invite is None or invite.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    invite.revoked_at = utcnow()
    db.commit()
    return RedirectResponse(f"/festivals/{festival_id}/settings", status_code=303)


@router.post("/festivals/{festival_id}/archive")
async def archive(
    request: Request,
    festival_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_admin(db, user, festival_id)
    membership.festival.status = FestivalStatus.ARCHIVED.value
    db.commit()
    return RedirectResponse(f"/festivals/{festival_id}", status_code=303)


@router.post("/festivals/{festival_id}/unarchive")
async def unarchive(
    request: Request,
    festival_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_admin(db, user, festival_id)
    if membership.festival.status == FestivalStatus.ARCHIVED.value:
        membership.festival.status = FestivalStatus.ACTIVE.value
        db.commit()
    return RedirectResponse(f"/festivals/{festival_id}", status_code=303)
