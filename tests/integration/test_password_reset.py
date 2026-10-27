"""Integration tests for the password reset flow."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, reset_engine_for_tests
from app.main import create_app
from app.models.password_reset import PasswordReset
from app.models.user import User, utcnow
from app.security.csrf import CSRF_COOKIE
from app.services.mail import capture_sink
from app.services.passwords import hash_password, verify_password
from app.services.sessions import COOKIE_NAME

PASSWORD = "a-long-enough-password"
NEW_PASSWORD = "an-even-better-password"


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    reset_engine_for_tests(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as db:
        db.add(
            User(
                email="metal@example.org",
                password_hash=hash_password(PASSWORD),
                display_name="Metal Fan",
            )
        )
        db.commit()
    capture_sink().clear()
    settings = Settings(_env_file=None, environment="test", base_url="https://example.org")
    client = TestClient(create_app(settings), follow_redirects=False)
    return client, factory


def _csrf(client: TestClient) -> str:
    client.get("/forgot")
    return client.cookies[CSRF_COOKIE]


def _request_reset(client: TestClient, email: str):
    token = _csrf(client)
    return client.post("/forgot", data={"email": email, "csrf_token": token})


def _link_token() -> str:
    body = capture_sink().sent[-1].body
    return body.split("/reset/")[1].split()[0]


def test_reset_request_captures_a_mail_with_a_link(env) -> None:
    client, _ = env
    response = _request_reset(client, "metal@example.org")
    assert response.status_code == 200
    assert "on its way" in response.text
    assert len(capture_sink().sent) == 1
    assert "https://example.org/reset/" in capture_sink().sent[0].body


def test_unknown_address_gets_the_same_answer_and_sends_nothing(env) -> None:
    client, _ = env
    response = _request_reset(client, "nobody@example.org")
    assert response.status_code == 200
    assert "on its way" in response.text
    assert capture_sink().sent == []


def test_a_valid_link_sets_a_new_password(env) -> None:
    client, factory = env
    _request_reset(client, "metal@example.org")
    token = _link_token()

    page = client.get(f"/reset/{token}")
    assert page.status_code == 200

    csrf = client.cookies[CSRF_COOKIE]
    response = client.post(
        f"/reset/{token}",
        data={"password": NEW_PASSWORD, "password_repeat": NEW_PASSWORD, "csrf_token": csrf},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    with factory() as db:
        user = db.scalar(select(User).where(User.email == "metal@example.org"))
        assert verify_password(user.password_hash, NEW_PASSWORD) is True
        assert verify_password(user.password_hash, PASSWORD) is False


def test_mismatched_passwords_are_rejected(env) -> None:
    client, _ = env
    _request_reset(client, "metal@example.org")
    token = _link_token()
    client.get(f"/reset/{token}")
    csrf = client.cookies[CSRF_COOKIE]
    response = client.post(
        f"/reset/{token}",
        data={"password": NEW_PASSWORD, "password_repeat": "something-else", "csrf_token": csrf},
    )
    assert response.status_code == 400
    assert "do not match" in response.text


def test_a_short_password_is_rejected(env) -> None:
    client, _ = env
    _request_reset(client, "metal@example.org")
    token = _link_token()
    client.get(f"/reset/{token}")
    csrf = client.cookies[CSRF_COOKIE]
    response = client.post(
        f"/reset/{token}",
        data={"password": "short", "password_repeat": "short", "csrf_token": csrf},
    )
    assert response.status_code == 400
    assert "at least" in response.text


def test_a_token_cannot_be_used_twice(env) -> None:
    client, _ = env
    _request_reset(client, "metal@example.org")
    token = _link_token()
    client.get(f"/reset/{token}")
    csrf = client.cookies[CSRF_COOKIE]
    data = {"password": NEW_PASSWORD, "password_repeat": NEW_PASSWORD, "csrf_token": csrf}
    assert client.post(f"/reset/{token}", data=data).status_code == 303
    assert client.post(f"/reset/{token}", data=data).status_code == 410


def test_an_expired_token_is_refused(env) -> None:
    client, factory = env
    _request_reset(client, "metal@example.org")
    token = _link_token()
    with factory() as db:
        record = db.scalars(select(PasswordReset)).one()
        record.expires_at = utcnow() - dt.timedelta(seconds=1)
        db.commit()
    assert client.get(f"/reset/{token}").status_code == 410


def test_an_unknown_token_is_refused(env) -> None:
    client, _ = env
    assert client.get("/reset/not-a-real-token").status_code == 410


def test_resetting_logs_every_session_out(env) -> None:
    client, _ = env
    csrf = _csrf(client)
    client.post(
        "/login", data={"email": "metal@example.org", "password": PASSWORD, "csrf_token": csrf}
    )
    assert client.get("/").status_code == 200

    _request_reset(client, "metal@example.org")
    token = _link_token()
    client.get(f"/reset/{token}")
    csrf = client.cookies[CSRF_COOKIE]
    client.post(
        f"/reset/{token}",
        data={"password": NEW_PASSWORD, "password_repeat": NEW_PASSWORD, "csrf_token": csrf},
    )
    after = client.get("/")
    assert after.status_code == 303
    assert COOKIE_NAME not in after.cookies
