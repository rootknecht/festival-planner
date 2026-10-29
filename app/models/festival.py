"""Festival, participant and invitation models."""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.user import User, utcnow


class FestivalStatus(enum.StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    SUSPENDED = "suspended"  # set by the instance operator, not by its admins


class ParticipantRole(enum.StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


#: Every selectable theme: the key is the CSS filename in
#: app/static/css/themes/ and the value is the label shown in settings.
#: Adding a theme is a CSS file plus one entry here — no migration, because
#: the column is a plain string.
THEMES = {
    "dark": "Dark",
    "metal": "Metal",
    "punk": "Punk",
    "matrix": "Matrix",
    "light": "Light",
}


class Festival(Base):
    __tablename__ = "festival"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    address_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    camp_arrival: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    camp_departure: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Europe/Berlin")
    #: One of THEMES. Stored as a plain string rather than an enum so adding a
    #: theme is a CSS file plus one list entry, with no migration.
    theme: Mapped[str] = mapped_column(String(32), nullable=False, default="dark")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FestivalStatus.ACTIVE.value
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    participants: Mapped[list[Participant]] = relationship(
        back_populates="festival", cascade="all, delete-orphan"
    )

    @property
    def is_archived(self) -> bool:
        return self.status in (FestivalStatus.ARCHIVED.value, FestivalStatus.SUSPENDED.value)

    @property
    def date_range(self) -> str:
        if self.start_date and self.end_date:
            return f"{self.start_date.isoformat()} – {self.end_date.isoformat()}"
        if self.start_date:
            return self.start_date.isoformat()
        return ""

    @property
    def countdown_days(self) -> int | None:
        """Whole days until camping opens (or the festival starts).

        Camping arrival is the date that actually matters to this group: it is
        when they leave, and it is usually a day or two before the first act.
        Falls back to the festival start when no camping date is set, and
        returns None when neither is known rather than inventing a number.
        """
        target = self.camp_arrival or self.start_date
        if target is None:
            return None
        return (target - dt.date.today()).days


class Participant(Base):
    """A person in a festival.

    ``user_id`` is NULL for a placeholder: somebody who takes a car seat and
    brings a tent but has no account. A placeholder can later be claimed
    through an invitation without losing any of its data.
    """

    __tablename__ = "participant"
    __table_args__ = (UniqueConstraint("festival_id", "user_id", name="uq_participant_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    festival_id: Mapped[int] = mapped_column(
        ForeignKey("festival.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True
    )
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ParticipantRole.MEMBER.value
    )
    joined_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    festival: Mapped[Festival] = relationship(back_populates="participants")
    user: Mapped[User | None] = relationship()

    @property
    def is_placeholder(self) -> bool:
        return self.user_id is None

    @property
    def is_admin(self) -> bool:
        return self.role == ParticipantRole.ADMIN.value


class Invite(Base):
    __tablename__ = "invite"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    festival_id: Mapped[int] = mapped_column(
        ForeignKey("festival.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # When set, accepting the invitation claims this placeholder instead of
    # creating a second participant row for the same person.
    claims_participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("participant.id", ondelete="SET NULL"), nullable=True
    )

    festival: Mapped[Festival] = relationship()

    @property
    def is_open(self) -> bool:
        return self.accepted_at is None and self.revoked_at is None
