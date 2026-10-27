"""Request-scoped dependencies: database session and the current user."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session as DbSession

from app.db import get_session_factory
from app.models.user import User
from app.services.sessions import COOKIE_NAME, resolve_session


def get_db() -> Iterator[DbSession]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def current_user(request: Request, db: DbSession = Depends(get_db)) -> User | None:
    user = resolve_session(db, request.cookies.get(COOKIE_NAME))
    request.state.user = user
    return user


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
    return user


def require_operator(user: User = Depends(require_user)) -> User:
    if not user.is_instance_operator:
        # 404, not 403: the operator area does not advertise its existence.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return user
