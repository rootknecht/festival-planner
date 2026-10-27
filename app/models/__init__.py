"""Model package.

Importing this package must register every table on ``Base.metadata`` so that
Alembic autogeneration sees the full schema.
"""

from __future__ import annotations

from app.models.bringlist import BringCell, BringRow, ChangeLog
from app.models.discussion import Message, Poll, PollOption, PollVote, Post, Topic
from app.models.festival import Festival, Invite, Participant
from app.models.notification import Notification
from app.models.password_reset import PasswordReset
from app.models.push import PushSubscription
from app.models.transport import Car, CarSeat, CarStop, GeocodeCache
from app.models.user import LoginAttempt, User, UserSession

__all__ = [
    "BringCell",
    "Car",
    "CarSeat",
    "CarStop",
    "GeocodeCache",
    "BringRow",
    "ChangeLog",
    "Festival",
    "Message",
    "Poll",
    "PollOption",
    "PollVote",
    "Post",
    "PushSubscription",
    "Topic",
    "Invite",
    "LoginAttempt",
    "Notification",
    "Participant",
    "PasswordReset",
    "User",
    "UserSession",
]
