import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.services import login_throttle


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        yield session


def test_a_fresh_identity_is_not_locked(db: Session) -> None:
    assert login_throttle.is_locked(db, "nobody@example.org") is False


def test_lockout_after_the_configured_number_of_failures(db: Session) -> None:
    for _ in range(login_throttle.MAX_FAILURES):
        login_throttle.record_failure(db, "target@example.org")
    assert login_throttle.is_locked(db, "target@example.org") is True


def test_one_failure_short_of_the_limit_is_still_open(db: Session) -> None:
    for _ in range(login_throttle.MAX_FAILURES - 1):
        login_throttle.record_failure(db, "target@example.org")
    assert login_throttle.is_locked(db, "target@example.org") is False


def test_lockout_expires(db: Session) -> None:
    for _ in range(login_throttle.MAX_FAILURES):
        login_throttle.record_failure(db, "target@example.org")
    assert login_throttle.is_locked(db, "target@example.org") is True
    login_throttle.expire_older_than(db, "target@example.org", seconds=0)
    assert login_throttle.is_locked(db, "target@example.org") is False


def test_success_clears_the_counter(db: Session) -> None:
    for _ in range(login_throttle.MAX_FAILURES - 1):
        login_throttle.record_failure(db, "target@example.org")
    login_throttle.record_success(db, "target@example.org")
    for _ in range(login_throttle.MAX_FAILURES - 1):
        login_throttle.record_failure(db, "target@example.org")
    assert login_throttle.is_locked(db, "target@example.org") is False


def test_lockout_is_per_identity(db: Session) -> None:
    for _ in range(login_throttle.MAX_FAILURES):
        login_throttle.record_failure(db, "victim@example.org")
    assert login_throttle.is_locked(db, "someone-else@example.org") is False
