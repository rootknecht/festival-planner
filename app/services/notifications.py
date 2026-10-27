"""Creating, counting and clearing notifications."""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models.festival import Participant
from app.models.notification import Notification, NotificationKind, Tool
from app.models.user import utcnow


def notify(
    db: DbSession,
    *,
    festival_id: int,
    user_ids: list[int],
    kind: NotificationKind,
    tool: Tool,
    message_key: str,
    payload: dict | None = None,
    ref_id: int | None = None,
    actor_name: str | None = None,
    actor_user_id: int | None = None,
) -> int:
    """Create one notification per recipient. Never notifies the actor."""
    created = 0
    for user_id in dict.fromkeys(user_ids):
        if actor_user_id is not None and user_id == actor_user_id:
            continue
        db.add(
            Notification(
                festival_id=festival_id,
                user_id=user_id,
                kind=kind.value,
                tool=tool.value,
                message_key=message_key,
                payload=json.dumps(payload) if payload else None,
                ref_id=ref_id,
                actor_name=actor_name,
            )
        )
        created += 1
    db.commit()
    return created


def notify_participant(
    db: DbSession,
    *,
    festival_id: int,
    participant_id: int,
    **kwargs,
) -> int:
    """Notify the account behind a participant, if it has one.

    Placeholder participants have no account, so nothing is sent — their
    changes are visible to the admin who maintains them.
    """
    participant = db.get(Participant, participant_id)
    if participant is None or participant.user_id is None:
        return 0
    return notify(db, festival_id=festival_id, user_ids=[participant.user_id], **kwargs)


def open_for(db: DbSession, user_id: int, festival_id: int) -> list[Notification]:
    return list(
        db.scalars(
            select(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.festival_id == festival_id,
                Notification.cleared_at.is_(None),
            )
            .order_by(Notification.created_at.desc())
        )
    )


def badges(db: DbSession, user_id: int, festival_id: int) -> dict[str, int]:
    """Open notification count per toolbar icon — the red dots."""
    rows = db.execute(
        select(Notification.tool, func.count())
        .where(
            Notification.user_id == user_id,
            Notification.festival_id == festival_id,
            Notification.cleared_at.is_(None),
        )
        .group_by(Notification.tool)
    ).all()
    return {tool: count for tool, count in rows}


def clear(db: DbSession, notification_id: int, user_id: int) -> bool:
    """Clear one block. Only its owner can clear it."""
    notification = db.get(Notification, notification_id)
    if notification is None or notification.user_id != user_id:
        return False
    if notification.cleared_at is None:
        notification.cleared_at = utcnow()
        db.commit()
    return True


def clear_tool(db: DbSession, user_id: int, festival_id: int, tool: Tool) -> int:
    """Clear every open block for one tool — e.g. on opening the chat."""
    rows = list(
        db.scalars(
            select(Notification).where(
                Notification.user_id == user_id,
                Notification.festival_id == festival_id,
                Notification.tool == tool.value,
                Notification.cleared_at.is_(None),
            )
        )
    )
    for row in rows:
        row.cleared_at = utcnow()
    db.commit()
    return len(rows)


def clear_ref(
    db: DbSession,
    user_id: int,
    festival_id: int,
    kind: NotificationKind,
    ref_id: int,
) -> int:
    """Clear blocks pointing at one object — e.g. after voting in a poll."""
    rows = list(
        db.scalars(
            select(Notification).where(
                Notification.user_id == user_id,
                Notification.festival_id == festival_id,
                Notification.kind == kind.value,
                Notification.ref_id == ref_id,
                Notification.cleared_at.is_(None),
            )
        )
    )
    for row in rows:
        row.cleared_at = utcnow()
    db.commit()
    return len(rows)


def payload_of(notification: Notification) -> dict:
    if not notification.payload:
        return {}
    try:
        return json.loads(notification.payload)
    except ValueError:
        return {}
