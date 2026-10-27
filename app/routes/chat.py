"""Chat: one room per festival, over HTTP and over a WebSocket."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from starlette.websockets import WebSocketDisconnect

from app.db import get_session_factory
from app.deps import get_db, require_user
from app.models.discussion import Message
from app.models.notification import Tool
from app.models.user import User, utcnow
from app.realtime.hub import hub
from app.services import notifications
from app.services.access import find_membership, require_membership, require_writable
from app.services.markup import render as render_markup
from app.services.mentions import notify_mentions, participant_names
from app.services.sessions import COOKIE_NAME, resolve_session
from app.templating import render

router = APIRouter(tags=["chat"])

HISTORY_LIMIT = 300


def _messages(db: DbSession, festival_id: int) -> list[Message]:
    rows = list(
        db.scalars(
            select(Message)
            .where(Message.festival_id == festival_id, Message.deleted_at.is_(None))
            .order_by(Message.created_at.desc())
            .limit(HISTORY_LIMIT)
        )
    )
    return list(reversed(rows))


@router.get("/festivals/{festival_id}/chat")
async def room(
    request: Request,
    festival_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_membership(db, user, festival_id)
    # Reading the room is acknowledging its mentions.
    notifications.clear_tool(db, user.id, festival_id, Tool.CHAT)
    names = participant_names(db, festival_id)
    messages = [
        {
            "id": message.id,
            "author": message.author_name,
            "body_html": render_markup(message.body, names),
            "created_at": message.created_at,
            "edited": message.edited_at is not None,
            "mine": message.author_id == user.id,
            "can_edit": message.editable_by(user, membership.is_admin),
        }
        for message in _messages(db, festival_id)
    ]
    return render(
        request,
        "tools/chat.html",
        {
            "festival": membership.festival,
            "membership": membership,
            "badges": notifications.badges(db, user.id, festival_id),
            "active_tool": "chat",
            "messages": messages,
            "names": names,
        },
    )


@router.post("/festivals/{festival_id}/chat")
async def post_message(
    request: Request,
    festival_id: int,
    body: str = Form(...),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_writable(require_membership(db, user, festival_id))
    text = body.strip()[:4000]
    if text:
        message = Message(
            festival_id=festival_id,
            author_id=user.id,
            author_name=membership.participant.display_name or user.display_name,
            body=text,
        )
        db.add(message)
        db.commit()
        notify_mentions(
            db,
            festival_id=festival_id,
            text=text,
            actor=user,
            tool=Tool.CHAT,
            ref_id=message.id,
        )
        await hub.broadcast(
            festival_id,
            {
                "type": "message",
                "id": message.id,
                "author": message.author_name,
                "html": render_markup(text, participant_names(db, festival_id)),
            },
        )
    return RedirectResponse(f"/festivals/{festival_id}/chat", status_code=303)


@router.post("/festivals/{festival_id}/chat/{message_id}/delete")
async def delete_message(
    request: Request,
    festival_id: int,
    message_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_writable(require_membership(db, user, festival_id))
    message = db.get(Message, message_id)
    if message is None or message.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    if not message.editable_by(user, membership.is_admin):
        raise HTTPException(status_code=403, detail="that message can no longer be changed")
    message.deleted_at = utcnow()
    db.commit()
    await hub.broadcast(festival_id, {"type": "changed", "tool": "chat"})
    return RedirectResponse(f"/festivals/{festival_id}/chat", status_code=303)


@router.post("/festivals/{festival_id}/chat/{message_id}/edit")
async def edit_message(
    request: Request,
    festival_id: int,
    message_id: int,
    body: str = Form(...),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_writable(require_membership(db, user, festival_id))
    message = db.get(Message, message_id)
    if message is None or message.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    if message.author_id != user.id or not message.editable_by(user, is_admin=False):
        # Admins may delete anything but not rewrite what somebody else said.
        raise HTTPException(status_code=403, detail="that message can no longer be edited")
    message.body = body.strip()[:4000]
    message.edited_at = utcnow()
    db.commit()
    await hub.broadcast(festival_id, {"type": "changed", "tool": "chat"})
    return RedirectResponse(f"/festivals/{festival_id}/chat", status_code=303)


@router.websocket("/festivals/{festival_id}/ws")
async def festival_socket(websocket: WebSocket, festival_id: int) -> None:
    """Live channel for one festival.

    Authorisation happens before the socket joins: the session cookie must
    belong to a participant of exactly this festival.
    """
    session = get_session_factory()()
    try:
        user = resolve_session(session, websocket.cookies.get(COOKIE_NAME))
        if user is None or find_membership(session, user, festival_id) is None:
            await websocket.close(code=4403)
            return
    finally:
        session.close()

    await websocket.accept()
    await hub.join(festival_id, websocket)
    try:
        while True:
            # The client never sends anything meaningful; this keeps the
            # connection open and notices when it goes away.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.leave(festival_id, websocket)
