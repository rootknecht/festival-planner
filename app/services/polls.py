"""Polls: create, vote, close, and who still owes an answer."""

from __future__ import annotations

import datetime as dt

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models.discussion import Poll, PollMode, PollOption, PollVote
from app.models.festival import Participant
from app.models.notification import NotificationKind, Tool
from app.models.user import User, utcnow
from app.services.notifications import clear_ref, notify

MAX_OPTIONS = 20


def create_poll(
    db: DbSession,
    *,
    festival_id: int,
    question: str,
    options: list[str],
    mode: str,
    closes_at: dt.date | None,
    actor: User,
) -> Poll:
    labels = [line.strip()[:200] for line in options if line.strip()][:MAX_OPTIONS]
    if not question.strip():
        raise HTTPException(status_code=400, detail="a poll needs a question")
    if len(labels) < 2:
        raise HTTPException(status_code=400, detail="a poll needs at least two options")

    poll = Poll(
        festival_id=festival_id,
        question=question.strip()[:500],
        mode=PollMode.MULTI.value if mode == PollMode.MULTI.value else PollMode.SINGLE.value,
        closes_at=(
            dt.datetime.combine(closes_at, dt.time(23, 59), tzinfo=dt.UTC) if closes_at else None
        ),
        created_by=actor.id,
    )
    db.add(poll)
    db.flush()
    for position, label in enumerate(labels):
        db.add(PollOption(poll_id=poll.id, label=label, position=position))
    db.commit()

    # Everybody with an account is asked, except the person who asked.
    targets = [
        p.user_id
        for p in db.scalars(select(Participant).where(Participant.festival_id == festival_id))
        if p.user_id
    ]
    notify(
        db,
        festival_id=festival_id,
        user_ids=targets,
        kind=NotificationKind.POLL,
        tool=Tool.POLLS,
        message_key="poll.open",
        payload={"question": poll.question},
        ref_id=poll.id,
        actor_name=actor.display_name,
        actor_user_id=actor.id,
    )
    return poll


def polls_of(db: DbSession, festival_id: int) -> list[Poll]:
    return list(
        db.scalars(
            select(Poll).where(Poll.festival_id == festival_id).order_by(Poll.created_at.desc())
        )
    )


def votes_by_option(db: DbSession, poll: Poll) -> dict[int, list[str]]:
    rows = db.execute(
        select(PollVote.option_id, Participant.display_name)
        .join(Participant, Participant.id == PollVote.participant_id)
        .join(PollOption, PollOption.id == PollVote.option_id)
        .where(PollOption.poll_id == poll.id)
    ).all()
    result: dict[int, list[str]] = {option.id: [] for option in poll.options}
    for option_id, name in rows:
        result.setdefault(option_id, []).append(name)
    for names in result.values():
        names.sort(key=str.lower)
    return result


def pending_voters(db: DbSession, poll: Poll) -> list[str]:
    voted = {
        row
        for row in db.scalars(
            select(PollVote.participant_id)
            .join(PollOption, PollOption.id == PollVote.option_id)
            .where(PollOption.poll_id == poll.id)
        )
    }
    return sorted(
        (
            p.display_name
            for p in db.scalars(
                select(Participant).where(Participant.festival_id == poll.festival_id)
            )
            if p.id not in voted
        ),
        key=str.lower,
    )


def vote(
    db: DbSession,
    *,
    poll: Poll,
    participant: Participant,
    option_ids: list[int],
    actor: User,
) -> None:
    if poll.is_closed():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="this poll is closed")
    valid = {option.id for option in poll.options}
    chosen = [option_id for option_id in option_ids if option_id in valid]
    if not chosen:
        raise HTTPException(status_code=400, detail="choose at least one option")
    if not poll.is_multi:
        chosen = chosen[:1]

    existing = list(
        db.scalars(
            select(PollVote)
            .join(PollOption, PollOption.id == PollVote.option_id)
            .where(PollOption.poll_id == poll.id, PollVote.participant_id == participant.id)
        )
    )
    for row in existing:
        db.delete(row)
    for option_id in chosen:
        db.add(PollVote(option_id=option_id, participant_id=participant.id))
    db.commit()

    clear_ref(db, actor.id, poll.festival_id, NotificationKind.POLL, poll.id)


def close_poll(db: DbSession, poll: Poll) -> None:
    if poll.closed_at is None:
        poll.closed_at = utcnow()
        db.commit()
    # A closed poll can no longer be answered, so its blocks are meaningless.
    for participant in db.scalars(
        select(Participant).where(Participant.festival_id == poll.festival_id)
    ):
        if participant.user_id:
            clear_ref(db, participant.user_id, poll.festival_id, NotificationKind.POLL, poll.id)


def reopen_poll(db: DbSession, poll: Poll, actor: User) -> None:
    """Put a closed poll back in play.

    Closing is reversible because it is usually a judgement call made too
    early — "everyone has voted" turns out to be wrong when somebody joins
    the festival a week later. Existing votes are kept: reopening is not a
    reset, and silently discarding answers people already gave would be the
    worse surprise.

    Everyone who has not voted gets their block back, since the question is
    live again and they are the ones who need to act.
    """
    if poll.closed_at is None:
        return
    poll.closed_at = None
    db.commit()

    # Only the people who still owe an answer get a block back. Re-notifying
    # someone who already voted would be asking them a question they have
    # answered, which is how people learn to ignore the dot.
    voted = {
        vote.participant_id
        for vote in db.scalars(
            select(PollVote).join(PollOption).where(PollOption.poll_id == poll.id)
        )
    }
    targets = [
        p.user_id
        for p in db.scalars(
            select(Participant).where(Participant.festival_id == poll.festival_id)
        )
        if p.user_id and p.id not in voted
    ]
    if targets:
        notify(
            db,
            festival_id=poll.festival_id,
            user_ids=targets,
            kind=NotificationKind.POLL,
            tool=Tool.POLLS,
            message_key="poll.open",
            payload={"question": poll.question},
            ref_id=poll.id,
            actor_name=actor.display_name,
            actor_user_id=actor.id,
        )


def delete_poll(db: DbSession, poll: Poll) -> None:
    """Remove a poll, its options and its votes.

    The blocks go first: a notification pointing at a poll that no longer
    exists would render as a dead link on everyone's dashboard.
    """
    for participant in db.scalars(
        select(Participant).where(Participant.festival_id == poll.festival_id)
    ):
        if participant.user_id:
            clear_ref(
                db, participant.user_id, poll.festival_id, NotificationKind.POLL, poll.id
            )
    db.delete(poll)
    db.commit()
