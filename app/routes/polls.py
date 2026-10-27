"""Poll routes."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session as DbSession

from app.deps import get_db, require_user
from app.models.discussion import Poll
from app.models.notification import Tool
from app.models.user import User
from app.realtime.hub import notify_change
from app.services import notifications, polls
from app.services.access import require_membership, require_writable
from app.templating import render

router = APIRouter(tags=["polls"])


def _page(request: Request, db: DbSession, user: User, festival_id: int, **extra) -> Response:
    membership = require_membership(db, user, festival_id)
    rows = polls.polls_of(db, festival_id)
    data = [
        {
            "poll": poll,
            "votes": polls.votes_by_option(db, poll),
            "pending": polls.pending_voters(db, poll),
            "mine": {
                option.id
                for option in poll.options
                if membership.participant.display_name
                in polls.votes_by_option(db, poll).get(option.id, [])
            },
            "closed": poll.is_closed(),
        }
        for poll in rows
    ]
    return render(
        request,
        "tools/polls.html",
        {
            "festival": membership.festival,
            "membership": membership,
            "badges": notifications.badges(db, user.id, festival_id),
            "active_tool": "polls",
            "polls": data,
            **extra,
        },
    )


def _poll_of(db: DbSession, festival_id: int, poll_id: int) -> Poll:
    poll = db.get(Poll, poll_id)
    if poll is None or poll.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    return poll


@router.get("/festivals/{festival_id}/polls")
async def overview(
    request: Request,
    festival_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_membership(db, user, festival_id)
    return _page(request, db, user, festival_id, error=None)


@router.post("/festivals/{festival_id}/polls")
async def create(
    request: Request,
    festival_id: int,
    question: str = Form(...),
    options: str = Form(...),
    mode: str = Form(default="single"),
    closes_on: str = Form(default=""),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_writable(require_membership(db, user, festival_id))
    closes_at = None
    if closes_on.strip():
        try:
            closes_at = dt.date.fromisoformat(closes_on.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid date") from exc
    polls.create_poll(
        db,
        festival_id=festival_id,
        question=question,
        options=options.splitlines(),
        mode=mode,
        closes_at=closes_at,
        actor=user,
    )
    notify_change(festival_id, "polls")
    return RedirectResponse(f"/festivals/{festival_id}/polls", status_code=303)


@router.post("/festivals/{festival_id}/polls/{poll_id}/vote")
async def vote(
    request: Request,
    festival_id: int,
    poll_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_writable(require_membership(db, user, festival_id))
    form = await request.form()
    option_ids = [int(value) for value in form.getlist("option_id") if str(value).isdigit()]
    polls.vote(
        db,
        poll=_poll_of(db, festival_id, poll_id),
        participant=membership.participant,
        option_ids=option_ids,
        actor=user,
    )
    notify_change(festival_id, "polls")
    return RedirectResponse(f"/festivals/{festival_id}/polls", status_code=303)


@router.post("/festivals/{festival_id}/polls/{poll_id}/close")
async def close(
    request: Request,
    festival_id: int,
    poll_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_writable(require_membership(db, user, festival_id))
    poll = _poll_of(db, festival_id, poll_id)
    # The creator or any festival admin may close a poll — nobody else.
    if poll.created_by != user.id and not membership.is_admin:
        raise HTTPException(status_code=403, detail="only the creator or an admin may close this")
    polls.close_poll(db, poll)
    notify_change(festival_id, Tool.POLLS.value)
    return RedirectResponse(f"/festivals/{festival_id}/polls", status_code=303)
