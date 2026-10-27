"""Integration tests for the login flow, CSRF protection and lockout."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, reset_engine_for_tests
from app.main import create_app
from app.models.user import User
from app.security.csrf import CSRF_COOKIE
from app.services.passwords import hash_password
from app.services.sessions import COOKIE_NAME

PASSWORD = "a-long-enough-password"


@pytest.fixture()
def client() -> TestClient:
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
    settings = Settings(_env_file=None, environment="test")
    return TestClient(create_app(settings), follow_redirects=False)


def _csrf(client: TestClient) -> str:
    client.get("/login")
    return client.cookies[CSRF_COOKIE]


def test_anonymous_root_redirects_to_login(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_page_renders_a_form(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert "Festival Planner" in response.text
    assert 'name="csrf_token"' in response.text


def test_post_without_csrf_token_is_rejected(client: TestClient) -> None:
    response = client.post("/login", data={"email": "metal@example.org", "password": PASSWORD})
    assert response.status_code == 403


def test_post_with_a_forged_csrf_token_is_rejected(client: TestClient) -> None:
    _csrf(client)
    response = client.post(
        "/login",
        data={"email": "metal@example.org", "password": PASSWORD, "csrf_token": "forged"},
    )
    assert response.status_code == 403


def test_successful_login_sets_a_session_cookie(client: TestClient) -> None:
    token = _csrf(client)
    response = client.post(
        "/login",
        data={"email": "metal@example.org", "password": PASSWORD, "csrf_token": token},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert COOKIE_NAME in response.cookies
    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.replace("samesite", "SameSite")


def test_wrong_password_is_rejected_with_a_generic_message(client: TestClient) -> None:
    token = _csrf(client)
    response = client.post(
        "/login",
        data={"email": "metal@example.org", "password": "wrong-password", "csrf_token": token},
    )
    assert response.status_code == 401
    assert "not correct" in response.text
    assert COOKIE_NAME not in response.cookies


def test_unknown_email_gives_the_same_message_as_a_wrong_password(client: TestClient) -> None:
    token = _csrf(client)
    response = client.post(
        "/login",
        data={"email": "nobody@example.org", "password": PASSWORD, "csrf_token": token},
    )
    assert response.status_code == 401
    assert "not correct" in response.text


def test_repeated_failures_lock_the_account_out(client: TestClient) -> None:
    token = _csrf(client)
    for _ in range(5):
        client.post(
            "/login",
            data={"email": "metal@example.org", "password": "wrong", "csrf_token": token},
        )
    response = client.post(
        "/login",
        data={"email": "metal@example.org", "password": PASSWORD, "csrf_token": token},
    )
    assert response.status_code == 429
    assert COOKIE_NAME not in response.cookies


def test_logged_in_user_sees_their_festival_list_and_can_log_out(client: TestClient) -> None:
    token = _csrf(client)
    client.post(
        "/login",
        data={"email": "metal@example.org", "password": PASSWORD, "csrf_token": token},
    )
    home = client.get("/")
    assert home.status_code == 200
    assert "Your festivals" in home.text

    logout = client.post("/logout", data={"csrf_token": token})
    assert logout.status_code == 303
    after = client.get("/")
    assert after.status_code == 303
    assert after.headers["location"] == "/login"
