"""Polls, chat messages, forum topics and posts."""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.user import User, utcnow

#: How long an author may still edit or delete their own message.
EDIT_WINDOW_SECONDS = 15 * 60


class PollMode(enum.StrEnum):
    SINGLE = "single"
    MULTI = "multi"


class Poll(Base):
    __tablename__ = "poll"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    festival_id: Mapped[int] = mapped_column(
        ForeignKey("festival.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default=PollMode.SINGLE.value)
    closes_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    options: Mapped[list[PollOption]] = relationship(
        back_populates="poll", cascade="all, delete-orphan", order_by="PollOption.position"
    )

    @property
    def is_multi(self) -> bool:
        return self.mode == PollMode.MULTI.value

    def is_closed(self, now: dt.datetime | None = None) -> bool:
        if self.closed_at is not None:
            return True
        if self.closes_at is None:
            return False
        deadline = self.closes_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=dt.UTC)
        return deadline <= (now or utcnow())


class PollOption(Base):
    __tablename__ = "poll_option"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    poll_id: Mapped[int] = mapped_column(
        ForeignKey("poll.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    poll: Mapped[Poll] = relationship(back_populates="options")
    votes: Mapped[list[PollVote]] = relationship(
        back_populates="option", cascade="all, delete-orphan"
    )


class PollVote(Base):
    """Votes are not anonymous: seeing who has not voted yet is the point."""

    __tablename__ = "poll_vote"
    __table_args__ = (UniqueConstraint("option_id", "participant_id", name="uq_poll_vote"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    option_id: Mapped[int] = mapped_column(
        ForeignKey("poll_option.id", ondelete="CASCADE"), nullable=False, index=True
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participant.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    option: Mapped[PollOption] = relationship(back_populates="votes")


class Message(Base):
    """One chat message in a festival's single room."""

    __tablename__ = "message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    festival_id: Mapped[int] = mapped_column(
        ForeignKey("festival.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    author_name: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    edited_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    author: Mapped[User | None] = relationship()

    def editable_by(self, user: User, is_admin: bool, now: dt.datetime | None = None) -> bool:
        if self.deleted_at is not None:
            return False
        if is_admin:
            return True
        if self.author_id != user.id:
            return False
        created = self.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.UTC)
        return ((now or utcnow()) - created).total_seconds() <= EDIT_WINDOW_SECONDS


class Topic(Base):
    __tablename__ = "topic"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    festival_id: Mapped[int] = mapped_column(
        ForeignKey("festival.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_activity_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    posts: Mapped[list[Post]] = relationship(
        back_populates="topic", cascade="all, delete-orphan", order_by="Post.created_at"
    )


class Post(Base):
    __tablename__ = "post"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topic.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    author_name: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    edited_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    topic: Mapped[Topic] = relationship(back_populates="posts")
