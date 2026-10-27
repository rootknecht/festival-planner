"""Server-side sessions.

The cookie carries an opaque random token; only its hash is stored. Sessions
are therefore revocable (logout, password change, account deletion) — which a
stateless JWT would not be.
"""

from __future__ import annotations

import datetime as dt
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models.user import User, UserSession, utcnow
from app.services.tokens import generate_token, hash_token

COOKIE_NAME = "fp_session"


def _fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_session(
    db: DbSession,
    user: User,
    lifetime: int,
    user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[str, UserSession]:
    token = generate_token()
    record = UserSession(
        user_id=user.id,
        token_hash=hash_token(token),
        expires_at=utcnow() + dt.timedelta(seconds=lifetime),
        user_agent_hash=_fingerprint(user_agent),
        ip_hash=_fingerprint(ip),
    )
    db.add(record)
    db.commit()
    return token, record


def resolve_session(db: DbSession, token: str | None) -> User | None:
    """Return the owning user for a valid, unexpired session token."""
    if not token:
        return None
    record = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    if record is None:
        return None
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.UTC)
    if expires_at <= utcnow():
        return None
    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        return None
    record.last_seen_at = utcnow()
    db.commit()
    return user


def revoke_session(db: DbSession, token: str) -> None:
    record = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    if record is not None:
        db.delete(record)
        db.commit()


def revoke_all_sessions(db: DbSession, user: User) -> None:
    for record in db.scalars(select(UserSession).where(UserSession.user_id == user.id)):
        db.delete(record)
    db.commit()


def purge_expired(db: DbSession) -> int:
    stale = list(db.scalars(select(UserSession).where(UserSession.expires_at <= utcnow())))
    for record in stale:
        db.delete(record)
    db.commit()
    return len(stale)
