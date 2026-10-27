"""Login throttling and temporary lockout.

Counts recent failures per identity (the submitted email address). The lockout
releases by itself — an attacker must not be able to lock a victim out
permanently, and a legitimate user must not need an admin to get back in.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSession

from app.models.user import LoginAttempt, utcnow

MAX_FAILURES = 5
WINDOW_SECONDS = 900


def _normalise(identity: str) -> str:
    return identity.strip().lower()


def record_failure(db: DbSession, identity: str) -> None:
    db.add(LoginAttempt(identity=_normalise(identity)))
    db.commit()


def record_success(db: DbSession, identity: str) -> None:
    db.execute(delete(LoginAttempt).where(LoginAttempt.identity == _normalise(identity)))
    db.commit()


def recent_failures(db: DbSession, identity: str, window: int = WINDOW_SECONDS) -> int:
    since = utcnow() - dt.timedelta(seconds=window)
    return (
        db.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.identity == _normalise(identity),
                LoginAttempt.created_at >= since,
            )
        )
        or 0
    )


def is_locked(db: DbSession, identity: str, window: int = WINDOW_SECONDS) -> bool:
    return recent_failures(db, identity, window) >= MAX_FAILURES


def delay_for(failures: int) -> float:
    """Progressive delay applied before answering a failed login."""
    if failures <= 0:
        return 0.0
    return float(min(2 ** (failures - 1), 8))


def expire_older_than(db: DbSession, identity: str, seconds: int) -> None:
    cutoff = utcnow() - dt.timedelta(seconds=seconds)
    db.execute(
        delete(LoginAttempt).where(
            LoginAttempt.identity == _normalise(identity),
            LoginAttempt.created_at <= cutoff,
        )
    )
    db.commit()
