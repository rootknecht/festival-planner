"""M3: application shell, themes, translations, notifications and profile."""

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, reset_engine_for_tests
from app.main import create_app
from app.models.festival import THEMES, Festival, Participant
from app.models.notification import NotificationKind, Tool
from app.models.user import User
from app.security.csrf import CSRF_COOKIE
from app.services import notifications
from app.services.passwords import hash_password, verify_password

PASSWORD = "a-long-enough-password"


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    reset_engine_for_tests(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with session_factory() as db:
        db.add(
            User(
                email="metal@example.org",
                password_hash=hash_password(PASSWORD),
                display_name="Metal Fan",
            )
        )
        db.commit()
    return session_factory


@pytest.fixture()
def settings() -> Settings:
    return Settings(_env_file=None, environment="test", base_url="https://example.org")


@pytest.fixture()
def client(factory, settings) -> TestClient:
    client = TestClient(create_app(settings), follow_redirects=False)
    client.get("/login")
    token = client.cookies[CSRF_COOKIE]
    client.post(
        "/login", data={"email": "metal@example.org", "password": PASSWORD, "csrf_token": token}
    )
    return client


def csrf(client: TestClient) -> str:
    return client.cookies[CSRF_COOKIE]


def make_festival(client: TestClient) -> int:
    response = client.post("/festivals", data={"name": "Wacken", "csrf_token": csrf(client)})
    return int(response.headers["location"].rsplit("/", 1)[1])


def test_dashboard_renders_the_shell_with_every_tool(client) -> None:
    festival_id = make_festival(client)
    page = client.get(f"/festivals/{festival_id}")
    assert page.status_code == 200
    for path in ("transport", "bringlist", "chat", "polls", "forum"):
        assert f"/festivals/{festival_id}/{path}" in page.text
    assert "Nothing needs your attention right now." in page.text


def test_every_tool_page_exists_and_unknown_tools_are_404(client) -> None:
    festival_id = make_festival(client)
    for path in ("transport", "bringlist", "chat", "polls", "forum"):
        assert client.get(f"/festivals/{festival_id}/{path}").status_code == 200
    assert client.get(f"/festivals/{festival_id}/nonsense").status_code == 404


def test_a_notification_shows_as_a_block_with_a_red_dot(client, factory) -> None:
    festival_id = make_festival(client)
    with factory() as db:
        user = db.scalar(select(User).where(User.email == "metal@example.org"))
        notifications.notify(
            db,
            festival_id=festival_id,
            user_ids=[user.id],
            kind=NotificationKind.CHANGE,
            tool=Tool.TRANSPORT,
            message_key="change.car.added",
            payload={"car": "the red van"},
            actor_name="Friend",
        )
    page = client.get(f"/festivals/{festival_id}")
    assert "Friend placed you in the red van." in page.text
    assert 'class="dot"' in page.text


def test_got_it_clears_a_block(client, factory) -> None:
    festival_id = make_festival(client)
    with factory() as db:
        user = db.scalar(select(User).where(User.email == "metal@example.org"))
        notifications.notify(
            db,
            festival_id=festival_id,
            user_ids=[user.id],
            kind=NotificationKind.CHANGE,
            tool=Tool.BRINGLIST,
            message_key="change.bring.assigned",
            payload={"item": "the pavilion"},
            actor_name="Friend",
        )
        notification_id = notifications.open_for(db, user.id, festival_id)[0].id

    response = client.post(
        f"/festivals/{festival_id}/notifications/{notification_id}/clear",
        data={"csrf_token": csrf(client)},
    )
    assert response.status_code == 303
    page = client.get(f"/festivals/{festival_id}")
    assert "Nothing needs your attention right now." in page.text
    assert 'class="dot"' not in page.text


def test_a_block_belonging_to_someone_else_cannot_be_cleared(client, factory) -> None:
    festival_id = make_festival(client)
    with factory() as db:
        other = User(
            email="other@example.org",
            password_hash=hash_password(PASSWORD),
            display_name="Other",
        )
        db.add(other)
        db.flush()
        db.add(
            Participant(festival_id=festival_id, user_id=other.id, display_name="Other")
        )
        db.commit()
        notifications.notify(
            db,
            festival_id=festival_id,
            user_ids=[other.id],
            kind=NotificationKind.MENTION,
            tool=Tool.CHAT,
            message_key="mention.chat",
            actor_name="Metal Fan",
        )
        foreign_id = notifications.open_for(db, other.id, festival_id)[0].id

    response = client.post(
        f"/festivals/{festival_id}/notifications/{foreign_id}/clear",
        data={"csrf_token": csrf(client)},
    )
    assert response.status_code == 404


def test_the_actor_is_never_notified_about_their_own_action(client, factory) -> None:
    festival_id = make_festival(client)
    with factory() as db:
        user = db.scalar(select(User).where(User.email == "metal@example.org"))
        created = notifications.notify(
            db,
            festival_id=festival_id,
            user_ids=[user.id],
            kind=NotificationKind.CHANGE,
            tool=Tool.TRANSPORT,
            message_key="change.car.added",
            payload={"car": "the bus"},
            actor_user_id=user.id,
        )
    assert created == 0


def test_notifications_from_another_festival_do_not_leak(client, factory) -> None:
    first = make_festival(client)
    second = make_festival(client)
    with factory() as db:
        user = db.scalar(select(User).where(User.email == "metal@example.org"))
        notifications.notify(
            db,
            festival_id=second,
            user_ids=[user.id],
            kind=NotificationKind.POLL,
            tool=Tool.POLLS,
            message_key="poll.open",
            payload={"question": "Which campsite?"},
        )
    assert "Which campsite?" not in client.get(f"/festivals/{first}").text
    assert "Which campsite?" in client.get(f"/festivals/{second}").text


def test_the_language_switch_changes_the_interface_language(client) -> None:
    festival_id = make_festival(client)
    assert "Who brings what" in client.get(f"/festivals/{festival_id}").text

    response = client.post(
        "/profile/language",
        data={"language": "de", "next": f"/festivals/{festival_id}", "csrf_token": csrf(client)},
    )
    assert response.status_code == 303
    german = client.get(f"/festivals/{festival_id}").text
    assert "Wer bringt was" in german
    assert "Offen für dich" in german


def test_the_language_switch_cannot_be_used_as_an_open_redirect(client) -> None:
    response = client.post(
        "/profile/language",
        data={"language": "de", "next": "//evil.example.com", "csrf_token": csrf(client)},
    )
    assert response.headers["location"] == "/"


def test_the_festival_theme_applies_and_the_user_override_wins(client, factory) -> None:
    festival_id = make_festival(client)
    client.post(
        f"/festivals/{festival_id}/settings",
        data={"name": "Wacken", "theme": "matrix", "csrf_token": csrf(client)},
    )
    page = client.get(f"/festivals/{festival_id}").text
    assert "themes/matrix.css" in page

    client.post(
        "/profile",
        data={"display_name": "Metal Fan", "language": "en", "theme_override": "light",
              "csrf_token": csrf(client)},
    )
    page = client.get(f"/festivals/{festival_id}").text
    assert "themes/light.css" in page


def test_an_unknown_theme_is_rejected(client, factory) -> None:
    """A theme name becomes a stylesheet path, so it must never be free text."""
    festival_id = make_festival(client)
    client.post(
        f"/festivals/{festival_id}/settings",
        data={"name": "Wacken", "theme": "../../etc/passwd", "csrf_token": csrf(client)},
    )
    with factory() as db:
        theme = db.get(Festival, festival_id).theme
    assert theme == "dark"
    assert theme in THEMES


def test_profile_changes_name_and_language(client, factory) -> None:
    response = client.post(
        "/profile",
        data={"display_name": "Headbanger", "language": "de", "theme_override": "",
              "csrf_token": csrf(client)},
    )
    assert response.status_code == 303
    with factory() as db:
        user = db.scalar(select(User).where(User.email == "metal@example.org"))
        assert user.display_name == "Headbanger"
        assert user.language == "de"
        assert user.theme_override is None


def test_changing_the_password_requires_the_current_one(client, factory) -> None:
    response = client.post(
        "/profile/password",
        data={
            "current_password": "wrong-password",
            "password": "a-brand-new-password",
            "password_repeat": "a-brand-new-password",
            "csrf_token": csrf(client),
        },
    )
    assert response.status_code == 200
    assert "not correct" in response.text
    with factory() as db:
        user = db.scalar(select(User).where(User.email == "metal@example.org"))
        assert verify_password(user.password_hash, PASSWORD) is True


def test_changing_the_password_signs_every_device_out(client, factory) -> None:
    response = client.post(
        "/profile/password",
        data={
            "current_password": PASSWORD,
            "password": "a-brand-new-password",
            "password_repeat": "a-brand-new-password",
            "csrf_token": csrf(client),
        },
    )
    assert response.status_code == 303
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303


def test_two_factor_can_be_enabled_and_is_then_required_at_login(client, factory, settings) -> None:
    page = client.get("/profile/totp")
    assert page.status_code == 200
    secret = page.text.split("<code>")[1].split("</code>")[0]

    bad = client.post(
        "/profile/totp", data={"secret": secret, "code": "000000", "csrf_token": csrf(client)}
    )
    assert bad.status_code == 400

    good = client.post(
        "/profile/totp",
        data={"secret": secret, "code": pyotp.TOTP(secret).now(), "csrf_token": csrf(client)},
    )
    assert good.status_code == 303

    fresh = TestClient(create_app(settings), follow_redirects=False)
    fresh.get("/login")
    token = fresh.cookies[CSRF_COOKIE]
    without_code = fresh.post(
        "/login",
        data={"email": "metal@example.org", "password": PASSWORD, "csrf_token": token},
    )
    assert without_code.status_code == 401

    with_code = fresh.post(
        "/login",
        data={
            "email": "metal@example.org",
            "password": PASSWORD,
            "totp": pyotp.TOTP(secret).now(),
            "csrf_token": token,
        },
    )
    assert with_code.status_code == 303


def test_the_qr_endpoint_refuses_anything_that_is_not_a_provisioning_uri(client) -> None:
    assert client.get("/profile/totp/qr?uri=https://example.org/evil").status_code == 400
    ok = client.get("/profile/totp/qr?uri=otpauth://totp/test%3Fsecret%3DABC")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/svg+xml"
