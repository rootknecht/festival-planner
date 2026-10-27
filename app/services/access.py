"""The one place where "who may see or change this festival" is decided.

Every festival-scoped route goes through these helpers. Authorisation is not
re-implemented per feature: a new tool added later inherits the rule by using
``require_membership``.

Conventions:
- A non-participant gets 404, never 403 — a 403 would confirm that the
  festival exists.
- A participant who is not an admin gets 403 on admin-only actions: they
  already know the festival exists.
- Any write to an archived or suspended festival gets 409.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models.festival import Festival, FestivalStatus, Participant, ParticipantRole
from app.models.user import User


@dataclass(frozen=True)
class Membership:
    festival: Festival
    participant: Participant

    @property
    def is_admin(self) -> bool:
        return self.participant.is_admin


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


def find_membership(db: DbSession, user: User, festival_id: int) -> Membership | None:
    festival = db.get(Festival, festival_id)
    if festival is None:
        return None
    participant = db.scalar(
        select(Participant).where(
            Participant.festival_id == festival_id,
            Participant.user_id == user.id,
        )
    )
    if participant is None:
        return None
    return Membership(festival=festival, participant=participant)


def require_membership(db: DbSession, user: User, festival_id: int) -> Membership:
    membership = find_membership(db, user, festival_id)
    if membership is None:
        raise _not_found()
    if membership.festival.status == FestivalStatus.SUSPENDED.value:
        raise _not_found()
    return membership


def require_admin(db: DbSession, user: User, festival_id: int) -> Membership:
    membership = require_membership(db, user, festival_id)
    if not membership.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only a festival admin may do this",
        )
    return membership


def require_writable(membership: Membership) -> Membership:
    if membership.festival.is_archived:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this festival is archived and cannot be changed",
        )
    return membership


def festivals_for(db: DbSession, user: User) -> list[tuple[Festival, Participant]]:
    """Every festival this user belongs to. Nothing else is ever listed."""
    rows = db.execute(
        select(Festival, Participant)
        .join(Participant, Participant.festival_id == Festival.id)
        .where(
            Participant.user_id == user.id,
            Festival.status != FestivalStatus.SUSPENDED.value,
        )
        .order_by(Festival.start_date.is_(None), Festival.start_date, Festival.name)
    ).all()
    return [(festival, participant) for festival, participant in rows]


def count_admins(db: DbSession, festival_id: int) -> int:
    return len(
        list(
            db.scalars(
                select(Participant).where(
                    Participant.festival_id == festival_id,
                    Participant.role == ParticipantRole.ADMIN.value,
                )
            )
        )
    )
