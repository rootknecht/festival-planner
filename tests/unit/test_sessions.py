import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models.user import User
from app.services import sessions as session_service
from app.services.passwords import hash_password


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        yield session


@pytest.fixture()
def user(db: Session) -> User:
    user = User(
        email="someone@example.org",
        password_hash=hash_password("a-long-enough-password"),
        display_name="Someone",
    )
    db.add(user)
    db.commit()
    return user


def test_create_session_returns_a_token_that_is_not_stored(db: Session, user: User) -> None:
    token, record = session_service.create_session(db, user, lifetime=3600)
    assert len(token) > 20
    assert token != record.token_hash
    assert record.user_id == user.id


def test_a_fresh_session_resolves_to_its_user(db: Session, user: User) -> None:
    token, _ = session_service.create_session(db, user, lifetime=3600)
    assert session_service.resolve_session(db, token).id == user.id


def test_an_unknown_token_resolves_to_nothing(db: Session, user: User) -> None:
    session_service.create_session(db, user, lifetime=3600)
    assert session_service.resolve_session(db, "not-a-real-token") is None


def test_an_expired_session_resolves_to_nothing(db: Session, user: User) -> None:
    token, record = session_service.create_session(db, user, lifetime=3600)
    record.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    db.commit()
    assert session_service.resolve_session(db, token) is None


def test_revoked_session_resolves_to_nothing(db: Session, user: User) -> None:
    token, _ = session_service.create_session(db, user, lifetime=3600)
    session_service.revoke_session(db, token)
    assert session_service.resolve_session(db, token) is None


def test_revoke_all_logs_out_every_device(db: Session, user: User) -> None:
    first, _ = session_service.create_session(db, user, lifetime=3600)
    second, _ = session_service.create_session(db, user, lifetime=3600)
    session_service.revoke_all_sessions(db, user)
    assert session_service.resolve_session(db, first) is None
    assert session_service.resolve_session(db, second) is None


def test_a_soft_deleted_user_cannot_resolve_a_session(db: Session, user: User) -> None:
    token, _ = session_service.create_session(db, user, lifetime=3600)
    user.deleted_at = dt.datetime.now(dt.UTC)
    db.commit()
    assert session_service.resolve_session(db, token) is None
