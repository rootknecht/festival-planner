"""Forum: flat topics, linear threads."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.deps import get_db, require_user
from app.models.discussion import Post, Topic
from app.models.notification import Tool
from app.models.user import User, utcnow
from app.services import notifications
from app.services.access import require_admin, require_membership, require_writable
from app.services.markup import render as render_markup
from app.services.mentions import notify_mentions, participant_names
from app.templating import render

router = APIRouter(tags=["forum"])


def _topic_of(db: DbSession, festival_id: int, topic_id: int) -> Topic:
    topic = db.get(Topic, topic_id)
    if topic is None or topic.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    return topic


@router.get("/festivals/{festival_id}/forum")
async def index(
    request: Request,
    festival_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_membership(db, user, festival_id)
    rows = db.execute(
        select(Topic, func.count(Post.id))
        .outerjoin(Post, (Post.topic_id == Topic.id) & Post.deleted_at.is_(None))
        .where(Topic.festival_id == festival_id)
        .group_by(Topic.id)
        .order_by(Topic.last_activity_at.desc())
    ).all()
    return render(
        request,
        "tools/forum.html",
        {
            "festival": membership.festival,
            "membership": membership,
            "badges": notifications.badges(db, user.id, festival_id),
            "active_tool": "forum",
            "topics": [{"topic": topic, "posts": count} for topic, count in rows],
            "error": None,
        },
    )


@router.post("/festivals/{festival_id}/forum")
async def create_topic(
    request: Request,
    festival_id: int,
    title: str = Form(...),
    body: str = Form(...),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_writable(require_membership(db, user, festival_id))
    if not title.strip() or not body.strip():
        raise HTTPException(status_code=400, detail="a topic needs a title and a first post")

    topic = Topic(
        festival_id=festival_id,
        title=title.strip()[:200],
        created_by=user.id,
    )
    db.add(topic)
    db.flush()
    db.add(
        Post(
            topic_id=topic.id,
            author_id=user.id,
            author_name=membership.participant.display_name or user.display_name,
            body=body.strip()[:8000],
        )
    )
    db.commit()
    notify_mentions(
        db,
        festival_id=festival_id,
        text=body,
        actor=user,
        tool=Tool.FORUM,
        ref_id=topic.id,
        topic_title=topic.title,
    )
    return RedirectResponse(f"/festivals/{festival_id}/forum/{topic.id}", status_code=303)


@router.get("/festivals/{festival_id}/forum/{topic_id}")
async def thread(
    request: Request,
    festival_id: int,
    topic_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_membership(db, user, festival_id)
    topic = _topic_of(db, festival_id, topic_id)
    notifications.clear_tool(db, user.id, festival_id, Tool.FORUM)
    names = participant_names(db, festival_id)
    posts = [
        {
            "id": post.id,
            "author": post.author_name,
            "body_html": render_markup(post.body, names),
            "created_at": post.created_at,
            "edited": post.edited_at is not None,
            "can_delete": post.author_id == user.id or membership.is_admin,
        }
        for post in topic.posts
        if post.deleted_at is None
    ]
    return render(
        request,
        "tools/topic.html",
        {
            "festival": membership.festival,
            "membership": membership,
            "badges": notifications.badges(db, user.id, festival_id),
            "active_tool": "forum",
            "topic": topic,
            "posts": posts,
            "names": names,
        },
    )


@router.post("/festivals/{festival_id}/forum/{topic_id}/posts")
async def reply(
    request: Request,
    festival_id: int,
    topic_id: int,
    body: str = Form(...),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_writable(require_membership(db, user, festival_id))
    topic = _topic_of(db, festival_id, topic_id)
    if topic.locked and not membership.is_admin:
        raise HTTPException(status_code=409, detail="this topic is locked")
    text = body.strip()[:8000]
    if text:
        db.add(
            Post(
                topic_id=topic.id,
                author_id=user.id,
                author_name=membership.participant.display_name or user.display_name,
                body=text,
            )
        )
        topic.last_activity_at = utcnow()
        db.commit()
        notify_mentions(
            db,
            festival_id=festival_id,
            text=text,
            actor=user,
            tool=Tool.FORUM,
            ref_id=topic.id,
            topic_title=topic.title,
        )
    return RedirectResponse(f"/festivals/{festival_id}/forum/{topic_id}", status_code=303)


@router.post("/festivals/{festival_id}/forum/{topic_id}/posts/{post_id}/delete")
async def delete_post(
    request: Request,
    festival_id: int,
    topic_id: int,
    post_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_writable(require_membership(db, user, festival_id))
    topic = _topic_of(db, festival_id, topic_id)
    post = db.get(Post, post_id)
    if post is None or post.topic_id != topic.id:
        raise HTTPException(status_code=404, detail="not found")
    if post.author_id != user.id and not membership.is_admin:
        raise HTTPException(status_code=403, detail="not yours to delete")
    post.deleted_at = utcnow()
    db.commit()
    return RedirectResponse(f"/festivals/{festival_id}/forum/{topic_id}", status_code=303)


@router.post("/festivals/{festival_id}/forum/{topic_id}/delete")
async def delete_topic(
    request: Request,
    festival_id: int,
    topic_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_writable(require_admin(db, user, festival_id))
    db.delete(_topic_of(db, festival_id, topic_id))
    db.commit()
    return RedirectResponse(f"/festivals/{festival_id}/forum", status_code=303)
