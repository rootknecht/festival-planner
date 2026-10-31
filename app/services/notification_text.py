"""Turning a stored notification into a sentence in the reader's language.

Notifications store a key plus a payload, never a finished sentence, so the
same event reads correctly for a German and an English participant.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.notification import Notification
from app.services.notifications import payload_of
from app.translations import N_

TEMPLATES: dict[str, str] = {
    "mention.chat": N_("{actor} mentioned you in the chat."),
    "mention.chat.everyone": N_("{actor} wrote to everyone in the chat."),
    "mention.forum": N_("{actor} mentioned you in “{topic}”."),
    "poll.open": N_("A poll is waiting for your answer: “{question}”."),
    "change.car.added": N_("{actor} placed you in {car}."),
    "change.car.removed": N_("{actor} took you out of {car}."),
    "change.train.added": N_("{actor} put you on {train}."),
    "change.train.removed": N_("{actor} took you off {train}."),
    "change.bring.assigned": N_("{actor} noted that you bring {item}."),
    "change.bring.changed": N_("{actor} changed your amount for {item}."),
    "change.bring.cleared": N_("{actor} cleared your entry for {item}."),
    # Expenses. The amount is included because "you owe Kai something" is not
    # actionable and the dashboard block has to stand on its own.
    "change.expense.added": N_("{actor} paid for {what} — your share is {amount}."),
    "change.expense.paid": N_("{actor} says they paid you {amount} for {what}."),
    "change.expense.received": N_("{actor} confirmed your {amount} for {what}."),
    # Settling happens per PERSON, so these carry a total instead of an item.
    "change.expense.paid.total": N_("{actor} says they paid you {amount}."),
    "change.expense.received.total": N_("{actor} confirmed receiving your {amount}."),
}

TOOL_PATHS = {
    "transport": "transport",
    "bringlist": "bringlist",
    "expenses": "expenses",
    "chat": "chat",
    "polls": "polls",
    "forum": "forum",
}


@dataclass(frozen=True)
class Block:
    id: int
    kind: str
    tool: str
    text: str
    link: str | None


def describe(notification: Notification, gettext, festival_id: int) -> Block:
    data = payload_of(notification)
    data.setdefault("actor", notification.actor_name or gettext("Someone"))
    template = TEMPLATES.get(notification.message_key, "{actor} did something.")
    try:
        text = gettext(template).format(**data)
    except (KeyError, IndexError):
        # A payload that does not fit its template must not break the
        # dashboard — show the untranslated fallback instead.
        text = gettext("Something changed that involves you.")

    path = TOOL_PATHS.get(notification.tool)
    link = f"/festivals/{festival_id}/{path}" if path else None
    return Block(
        id=notification.id,
        kind=notification.kind,
        tool=notification.tool,
        text=text,
        link=link,
    )
