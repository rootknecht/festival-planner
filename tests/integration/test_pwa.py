"""M7: PWA plumbing and Web Push."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, reset_engine_for_tests
from app.main import create_app
from app.models.notification import NotificationKind, Tool
from app.models.push import PushSubscription
from app.models.user import User
from app.security.csrf import CSRF_COOKIE
from app.services import notifications, push
from app.services.passwords import hash_password

PASSWORD = "a-long-enough-password"
SUBSCRIPTION = {
    "endpoint": "https://push.example.org/send/abc123",
    "keys": {"p256dh": "BPublicKeyValue", "auth": "AuthSecret"},
}


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
        for email, name in (("owner@example.org", "Owner"), ("friend@example.org", "Friend")):
            db.add(User(email=email, password_hash=hash_password(PASSWORD), display_name=name))
        db.commit()
    return session_factory


@pytest.fixture()
def settings() -> Settings:
    return Settings(_env_file=None, environment="test", base_url="https://example.org")


@pytest.fixture()
def push_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        base_url="https://example.org",
        vapid_public_key="BPublicTestKey",
        vapid_private_key="-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----",
        vapid_subject="mailto:admin@example.org",
    )


def client_for(settings: Settings, email: str) -> TestClient:
    client = TestClient(create_app(settings), follow_redirects=False)
    client.get("/login")
    token = client.cookies[CSRF_COOKIE]
    client.post("/login", data={"email": email, "password": PASSWORD, "csrf_token": token})
    return client


def csrf(client: TestClient) -> str:
    return client.cookies[CSRF_COOKIE]


def json_post(client: TestClient, url: str, payload: dict):
    return client.post(
        url,
        content=json.dumps(payload),
        headers={"Content-Type": "application/json", "X-CSRF-Token": csrf(client)},
    )


# ------------------------------------------------------------- manifest ----


def test_the_manifest_is_served_with_the_right_type(factory, settings) -> None:
    client = TestClient(create_app(settings))
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    data = response.json()
    assert data["start_url"] == "/"
    assert data["display"] == "standalone"
    assert any(icon.get("purpose") == "maskable" for icon in data["icons"])


def test_every_manifest_icon_actually_exists(factory, settings) -> None:
    client = TestClient(create_app(settings))
    for icon in client.get("/manifest.webmanifest").json()["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]


def test_the_service_worker_is_served_from_the_root_with_root_scope(factory, settings) -> None:
    """At /static/sw.js it could only control /static/ — the scope would be useless."""
    client = TestClient(create_app(settings))
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["service-worker-allowed"] == "/"
    assert "addEventListener" in response.text


def test_the_offline_page_renders(factory, settings) -> None:
    client = TestClient(create_app(settings))
    response = client.get("/offline")
    assert response.status_code == 200
    assert "No connection" in response.text


def test_every_file_the_worker_precaches_is_reachable(factory, settings) -> None:
    client = TestClient(create_app(settings))
    worker = client.get("/sw.js").text
    shell = worker.split("var SHELL = [")[1].split("];")[0]
    paths = [line.strip().strip('",') for line in shell.splitlines() if '"' in line]
    for path in paths:
        assert client.get(path).status_code == 200, f"precached but missing: {path}"


# ----------------------------------------------------------------- push ----


def test_the_key_endpoint_reports_push_disabled_without_keys(factory, settings) -> None:
    client = client_for(settings, "owner@example.org")
    data = client.get("/push/key").json()
    assert data["enabled"] is False
    assert data["key"] == ""


def test_the_key_endpoint_reports_the_public_key_when_configured(factory, push_settings) -> None:
    client = client_for(push_settings, "owner@example.org")
    data = client.get("/push/key").json()
    assert data["enabled"] is True
    assert data["key"] == "BPublicTestKey"


def test_push_endpoints_require_a_login(factory, settings) -> None:
    anonymous = TestClient(create_app(settings), follow_redirects=False)
    anonymous.get("/login")
    response = anonymous.post(
        "/push/subscribe",
        content=json.dumps(SUBSCRIPTION),
        headers={
            "Content-Type": "application/json",
            "X-CSRF-Token": anonymous.cookies[CSRF_COOKIE],
            "Accept": "application/json",
        },
    )
    assert response.status_code in (303, 401)


def test_subscribing_stores_the_device(factory, settings) -> None:
    client = client_for(settings, "owner@example.org")
    assert json_post(client, "/push/subscribe", SUBSCRIPTION).status_code == 200
    with factory() as db:
        rows = db.scalars(select(PushSubscription)).all()
    assert len(rows) == 1
    assert rows[0].endpoint == SUBSCRIPTION["endpoint"]


def test_subscribing_twice_does_not_duplicate_the_device(factory, settings) -> None:
    client = client_for(settings, "owner@example.org")
    json_post(client, "/push/subscribe", SUBSCRIPTION)
    json_post(client, "/push/subscribe", SUBSCRIPTION)
    with factory() as db:
        assert len(db.scalars(select(PushSubscription)).all()) == 1


def test_the_same_browser_can_change_hands(factory, settings) -> None:
    """A shared browser must not keep pushing a previous account's news."""
    first = client_for(settings, "owner@example.org")
    json_post(first, "/push/subscribe", SUBSCRIPTION)
    second = client_for(settings, "friend@example.org")
    json_post(second, "/push/subscribe", SUBSCRIPTION)
    with factory() as db:
        rows = db.scalars(select(PushSubscription)).all()
        friend = db.scalar(select(User).where(User.email == "friend@example.org"))
    assert len(rows) == 1
    assert rows[0].user_id == friend.id


def test_an_incomplete_subscription_is_refused(factory, settings) -> None:
    client = client_for(settings, "owner@example.org")
    response = json_post(client, "/push/subscribe", {"endpoint": "https://push.example.org/x"})
    assert response.status_code == 400
    with factory() as db:
        assert db.scalars(select(PushSubscription)).all() == []


def test_unsubscribing_removes_only_your_own_device(factory, settings) -> None:
    owner = client_for(settings, "owner@example.org")
    json_post(owner, "/push/subscribe", SUBSCRIPTION)
    friend = client_for(settings, "friend@example.org")
    assert json_post(friend, "/push/unsubscribe", {"endpoint": SUBSCRIPTION["endpoint"]}).status_code == 200
    with factory() as db:
        assert len(db.scalars(select(PushSubscription)).all()) == 1

    assert json_post(owner, "/push/unsubscribe", {"endpoint": SUBSCRIPTION["endpoint"]}).status_code == 200
    with factory() as db:
        assert db.scalars(select(PushSubscription)).all() == []


def test_nothing_is_sent_when_push_is_not_configured(factory, settings, monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(push, "_deliver", lambda *a, **k: sent.append(a))
    client = client_for(settings, "owner@example.org")
    json_post(client, "/push/subscribe", SUBSCRIPTION)
    with factory() as db:
        owner = db.scalar(select(User).where(User.email == "owner@example.org"))
        queued = push.send_to_user(
            db, owner.id, title="x", body="y", url="/", settings=settings
        )
    assert queued == 0
    assert sent == []


def test_a_push_is_queued_for_each_device(factory, push_settings, monkeypatch) -> None:
    calls = []

    def fake_deliver(info, payload, settings):
        calls.append((info["endpoint"], json.loads(payload)))
        return None

    monkeypatch.setattr(push, "_deliver", fake_deliver)
    client = client_for(push_settings, "owner@example.org")
    json_post(client, "/push/subscribe", SUBSCRIPTION)
    second = dict(SUBSCRIPTION, endpoint="https://push.example.org/send/second")
    json_post(client, "/push/subscribe", second)

    with factory() as db:
        owner = db.scalar(select(User).where(User.email == "owner@example.org"))
        queued = push.send_to_user(
            db,
            owner.id,
            title="Festival Planner",
            body="Kai mentioned you",
            url="/festivals/1",
            settings=push_settings,
        )
    assert queued == 2
    assert {endpoint for endpoint, _ in calls} == {
        SUBSCRIPTION["endpoint"],
        second["endpoint"],
    }
    assert calls[0][1]["body"] == "Kai mentioned you"


def test_a_dead_subscription_is_deleted(factory, push_settings, monkeypatch) -> None:
    monkeypatch.setattr(push, "_deliver", lambda info, payload, settings: 410)
    client = client_for(push_settings, "owner@example.org")
    json_post(client, "/push/subscribe", SUBSCRIPTION)

    with factory() as db:
        owner = db.scalar(select(User).where(User.email == "owner@example.org"))
        push.send_to_user(db, owner.id, title="x", body="y", url="/", settings=push_settings)

    with factory() as db:
        assert db.scalars(select(PushSubscription)).all() == []


def test_a_notification_also_triggers_a_push(factory, push_settings, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        push, "_deliver", lambda info, payload, settings: calls.append(json.loads(payload))
    )
    # notify() reads the process settings, so point those at the push config
    monkeypatch.setattr(push, "get_settings", lambda: push_settings)
    owner = client_for(push_settings, "owner@example.org")
    json_post(owner, "/push/subscribe", SUBSCRIPTION)

    with factory() as db:
        owner_user = db.scalar(select(User).where(User.email == "owner@example.org"))
        notifications.notify(
            db,
            festival_id=1,
            user_ids=[owner_user.id],
            kind=NotificationKind.MENTION,
            tool=Tool.CHAT,
            message_key="mention.chat",
            actor_name="Kai",
            push_text="Kai mentioned you in the chat.",
        )
    assert calls and calls[0]["body"] == "Kai mentioned you in the chat."
    assert calls[0]["url"] == "/festivals/1"


def test_the_push_body_is_built_from_the_notification(factory, push_settings, monkeypatch) -> None:
    """Without an explicit text, the push says what actually happened."""
    calls = []
    monkeypatch.setattr(
        push, "_deliver", lambda info, payload, settings: calls.append(json.loads(payload))
    )
    monkeypatch.setattr(push, "get_settings", lambda: push_settings)
    owner = client_for(push_settings, "owner@example.org")
    json_post(owner, "/push/subscribe", SUBSCRIPTION)

    with factory() as db:
        owner_user = db.scalar(select(User).where(User.email == "owner@example.org"))
        notifications.notify(
            db,
            festival_id=7,
            user_ids=[owner_user.id],
            kind=NotificationKind.CHANGE,
            tool=Tool.TRANSPORT,
            message_key="change.car.added",
            payload={"car": "the red van"},
            actor_name="Sabine",
        )
    assert calls[0]["body"] == "Sabine placed you in the red van."
    assert calls[0]["url"] == "/festivals/7"


def test_a_pem_key_file_enables_push_without_putting_the_key_in_the_env(tmp_path) -> None:
    """A PEM is multi-line; env files are line-based. The file path is the key."""
    pem = tmp_path / "vapid.pem"
    pem.write_text("-----BEGIN PRIVATE KEY-----\nnot-real\n-----END PRIVATE KEY-----\n")
    settings = Settings(
        _env_file=None,
        environment="test",
        vapid_public_key="BPublicTestKey",
        vapid_private_key_file=str(pem),
    )
    assert push.enabled(settings) is True
    assert settings.vapid_private == str(pem)


def test_push_stays_off_when_neither_key_form_is_configured() -> None:
    settings = Settings(_env_file=None, environment="test", vapid_public_key="BPublicTestKey")
    assert push.enabled(settings) is False
