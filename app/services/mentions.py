"""Mentions: turning @names in a text into notifications."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models.festival import Participant
from app.models.notification import NotificationKind, Tool
from app.models.user import User
from app.services.markup import find_mentions
from app.services.notifications import notify


def participant_names(db: DbSession, festival_id: int) -> list[str]:
    return list(
        db.scalars(
            select(Participant.display_name).where(Participant.festival_id == festival_id)
        )
    )


def notify_mentions(
    db: DbSession,
    *,
    festival_id: int,
    text: str,
    actor: User,
    tool: Tool,
    ref_id: int | None,
    topic_title: str | None = None,
) -> int:
    """Create mention notifications for the people named in a text."""
    participants = list(
        db.scalars(select(Participant).where(Participant.festival_id == festival_id))
    )
    names = [p.display_name for p in participants]
    mentioned, everyone = find_mentions(text, names)

    if everyone:
        targets = [p.user_id for p in participants if p.user_id]
        key = "mention.chat.everyone" if tool is Tool.CHAT else "mention.forum"
    else:
        targets = [p.user_id for p in participants if p.user_id and p.display_name in mentioned]
        key = "mention.chat" if tool is Tool.CHAT else "mention.forum"

    if not targets:
        return 0

    return notify(
        db,
        festival_id=festival_id,
        user_ids=targets,
        kind=NotificationKind.MENTION,
        tool=tool,
        message_key=key,
        payload={"topic": topic_title} if topic_title else None,
        ref_id=ref_id,
        actor_name=actor.display_name,
        actor_user_id=actor.id,
    )
