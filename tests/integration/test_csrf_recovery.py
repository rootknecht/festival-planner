"""A stale CSRF token must be recoverable, not a dead end.

This file exists because of a real failure on a phone: the browser's site
data was cleared while a page was open, the form on screen still carried a
token whose cookie no longer existed, and the app answered a human being
with {"detail":"CSRF validation failed"} as raw JSON. The protection worked
exactly as designed; the experience was indefensible.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.db import Base, reset_engine_for_tests
from app.main import create_app
from app.models.user import User
from app.security.csrf import CSRF_COOKIE
from app.services.passwords import hash_password

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
        db.add(User(email="metal@example.org", password_hash=hash_password(PASSWORD),
                    display_name="Metal Fan"))
        db.commit()
    return TestClient(create_app(Settings(_env_file=None, environment="test")),
                      follow_redirects=False)


def _token(client) -> str:
    page = client.get("/login", headers={"accept": "text/html"}).text
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match, "the login form carries no CSRF token"
    return match.group(1)


def test_a_browser_gets_a_readable_page_not_json(client) -> None:
    token = _token(client)
    client.cookies.clear()
    response = client.post(
        "/login",
        data={"email": "a@example.org", "password": "b", "csrf_token": token},
        headers={"accept": "text/html,application/xhtml+xml"},
    )
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert "CSRF" not in response.text, "the page must not shout jargon at a user"
    assert "try that again" in response.text.lower()


def test_the_rejection_hands_back_a_working_token(client) -> None:
    """Without a fresh cookie the retry fails too, and the user is stuck."""
    token = _token(client)
    client.cookies.clear()
    rejected = client.post(
        "/login",
        data={"csrf_token": token},
        headers={"accept": "text/html"},
    )
    assert CSRF_COOKIE in rejected.cookies or CSRF_COOKIE in client.cookies

    fresh = _token(client)
    retry = client.post(
        "/login",
        data={"email": "nobody@example.org", "password": "wrong-but-well-formed",
              "csrf_token": fresh},
        headers={"accept": "text/html"},
    )
    assert retry.status_code != 403, "the retry after a rejection must not fail on CSRF"


def test_htmx_is_told_to_reload_rather_than_shown_an_error_page(client) -> None:
    """An error page swapped into a live layout would be worse than useless."""
    token = _token(client)
    client.cookies.clear()
    response = client.post(
        "/login", data={"csrf_token": token}, headers={"hx-request": "true"}
    )
    assert response.status_code == 403
    assert response.headers.get("hx-refresh") == "true"


def test_an_api_client_still_gets_json(client) -> None:
    token = _token(client)
    client.cookies.clear()
    response = client.post(
        "/login", data={"csrf_token": token}, headers={"accept": "application/json"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF validation failed"


def test_the_token_cookie_outlives_nothing_shorter_than_the_session() -> None:
    """The original bug in one line: the cookie lasted a day, the session a
    month, so a tab left open overnight came back broken."""
    import inspect

    from app.security import csrf

    source = inspect.getsource(csrf._set_token_cookie)
    assert "session_lifetime" in source, "the two lifetimes must not drift apart again"
    assert get_settings().session_lifetime > 60 * 60 * 24
