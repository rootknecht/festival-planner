"""Notifications — the dashboard blocks and the red dots.

Three kinds, exactly as specified:

- ``mention``  you were named in chat or in the forum;
- ``poll``     a poll is open and you have not answered it;
- ``change``   something changed that names you: placed in or removed from a
               car, an item assigned to you, your cell amount changed.

A notification belongs to one user inside one festival and points at the tool
that produced it, which is what drives the red dot on that toolbar icon. There
is no task system: the transportation and bring-list tables are the assignment.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.user import utcnow


class NotificationKind(enum.StrEnum):
    MENTION = "mention"
    POLL = "poll"
    CHANGE = "change"


class Tool(enum.StrEnum):
    """Toolbar icons that can carry a red dot."""

    TRANSPORT = "transport"
    BRINGLIST = "bringlist"
    EXPENSES = "expenses"
    CHAT = "chat"
    POLLS = "polls"
    FORUM = "forum"


class Notification(Base):
    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    festival_id: Mapped[int] = mapped_column(
        ForeignKey("festival.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    tool: Mapped[str] = mapped_column(String(16), nullable=False)
    # Message key plus JSON payload instead of a baked sentence: the block is
    # rendered in the reader's language, not the writer's.
    message_key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    cleared_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


Index(
    "ix_notification_open",
    Notification.user_id,
    Notification.festival_id,
    Notification.cleared_at,
)
