"""Per-festival sender brand: From address and link base, chosen from an allowlist.

The point of these tests is that a festival's mail identity is a PAIR. A mail
whose From says one domain and whose link points at another reads as phishing,
so sender and link base are asserted together everywhere.

The second point is that the allowlist is the only source of truth. Both the
stored value and the Host header are attacker-reachable, and either one would
otherwise be enough to forge a reset link that looks exactly right.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, reset_engine_for_tests
from app.main import create_app
from app.models.festival import Festival
from app.models.user import User
from app.security.csrf import CSRF_COOKIE
from app.services.mail import capture_sink
from app.services.passwords import hash_password

PASSWORD = "a-long-enough-password"
BRANDS = "firstfest.example,secondfest.example=hello@secondfest.example"


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
                email="owner@example.org",
                password_hash=hash_password(PASSWORD),
                display_name="Owner",
            )
        )
        db.commit()
    capture_sink().clear()
    return session_factory


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        base_url="https://firstfest.example",
        mail_brands=BRANDS,
    )


@pytest.fixture()
def client(settings: Settings, factory) -> TestClient:
    client = TestClient(create_app(settings), follow_redirects=False)
    client.get("/login")
    token = client.cookies[CSRF_COOKIE]
    response = client.post(
        "/login",
        data={"email": "owner@example.org", "password": PASSWORD, "csrf_token": token},
    )
    assert response.status_code == 303
    return client


def csrf(client: TestClient) -> str:
    client.get("/festivals/new")
    return client.cookies[CSRF_COOKIE]


def make_festival(client: TestClient, **extra) -> int:
    response = client.post(
        "/festivals", data={"name": "Wacken", "csrf_token": csrf(client), **extra}
    )
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[1])


def invite(client: TestClient, festival_id: int, address: str) -> None:
    response = client.post(
        f"/festivals/{festival_id}/invites",
        data={"email": address, "csrf_token": csrf(client)},
    )
    assert response.status_code == 303


def test_the_configuration_derives_a_sender_per_domain(settings: Settings) -> None:
    """A bare domain means noreply@domain; an explicit address wins."""
    assert settings.brand_senders == {
        "firstfest.example": "noreply@firstfest.example",
        "secondfest.example": "hello@secondfest.example",
    }


def test_the_creation_form_offers_every_brand_plus_the_default(client: TestClient) -> None:
    page = client.get("/festivals/new").text
    assert 'name="mail_domain"' in page
    assert 'value="firstfest.example"' in page
    assert 'value="secondfest.example"' in page
    assert "hello@secondfest.example" in page
    # The instance default must stay choosable, as the empty value.
    assert 'value=""' in page


def test_a_festival_sends_and_links_under_its_chosen_domain(client, factory) -> None:
    festival_id = make_festival(client, mail_domain="secondfest.example")
    with factory() as db:
        assert db.get(Festival, festival_id).mail_domain == "secondfest.example"

    capture_sink().clear()
    invite(client, festival_id, "friend@example.org")
    message = capture_sink().sent[-1]
    assert message.from_ == "hello@secondfest.example"
    assert "https://secondfest.example/invite/" in message.body
    assert "firstfest.example" not in message.body


def test_a_festival_without_a_choice_uses_the_instance_default(client) -> None:
    festival_id = make_festival(client)
    capture_sink().clear()
    invite(client, festival_id, "friend@example.org")
    message = capture_sink().sent[-1]
    # No per-message sender: the mailer falls back to settings.smtp_from.
    assert message.from_ == ""
    assert "https://firstfest.example/invite/" in message.body


def test_an_unknown_domain_falls_back_instead_of_being_stored(client, factory) -> None:
    """A tampered select must not become a mail header or a link base."""
    festival_id = make_festival(client, mail_domain="evil.example")
    with factory() as db:
        assert db.get(Festival, festival_id).mail_domain == ""

    capture_sink().clear()
    invite(client, festival_id, "friend@example.org")
    assert "evil.example" not in capture_sink().sent[-1].body


def test_settings_can_change_the_brand_and_reject_a_bogus_one(client, factory) -> None:
    festival_id = make_festival(client)
    client.post(
        f"/festivals/{festival_id}/settings",
        data={"name": "Wacken", "mail_domain": "secondfest.example", "csrf_token": csrf(client)},
    )
    with factory() as db:
        assert db.get(Festival, festival_id).mail_domain == "secondfest.example"

    client.post(
        f"/festivals/{festival_id}/settings",
        data={"name": "Wacken", "mail_domain": "../../etc/passwd", "csrf_token": csrf(client)},
    )
    with factory() as db:
        assert db.get(Festival, festival_id).mail_domain == ""


def test_a_festival_whose_domain_left_the_allowlist_degrades_quietly(
    client, factory, settings
) -> None:
    """Configuration can shrink; stored data must not start forging senders."""
    festival_id = make_festival(client, mail_domain="secondfest.example")
    object.__setattr__(settings, "mail_brands", "firstfest.example")
    capture_sink().clear()
    invite(client, festival_id, "friend@example.org")
    message = capture_sink().sent[-1]
    assert message.from_ == ""
    assert "https://firstfest.example/invite/" in message.body


def test_self_service_reset_follows_the_domain_it_was_asked_on(client) -> None:
    capture_sink().clear()
    client.get("/forgot")
    client.post(
        "/forgot",
        data={"email": "owner@example.org", "csrf_token": client.cookies[CSRF_COOKIE]},
        headers={"host": "secondfest.example"},
    )
    message = capture_sink().sent[-1]
    assert message.from_ == "hello@secondfest.example"
    assert "https://secondfest.example/reset/" in message.body


def test_a_forged_host_cannot_move_a_reset_link_or_its_sender(client) -> None:
    """Host is attacker-controlled: anything unlisted must hit the default."""
    for forged in ("evil.example", "firstfest.example.evil.example", "secondfest.example@evil.example"):
        capture_sink().clear()
        client.get("/forgot")
        response = client.post(
            "/forgot",
            data={"email": "owner@example.org", "csrf_token": client.cookies[CSRF_COOKIE]},
            headers={"host": forged},
        )
        assert response.status_code == 200
        message = capture_sink().sent[-1]
        assert message.from_ == ""
        assert "https://firstfest.example/reset/" in message.body
        assert "evil.example" not in message.body


def test_a_host_with_a_port_still_matches_its_brand(client) -> None:
    capture_sink().clear()
    client.get("/forgot")
    client.post(
        "/forgot",
        data={"email": "owner@example.org", "csrf_token": client.cookies[CSRF_COOKIE]},
        headers={"host": "secondfest.example:443"},
    )
    assert "https://secondfest.example/reset/" in capture_sink().sent[-1].body


def test_the_reset_route_still_reveals_nothing_about_unknown_addresses(client) -> None:
    client.get("/forgot")
    known = client.post(
        "/forgot",
        data={"email": "owner@example.org", "csrf_token": client.cookies[CSRF_COOKIE]},
        headers={"host": "secondfest.example"},
    )
    client.get("/forgot")
    unknown = client.post(
        "/forgot",
        data={"email": "nobody@example.org", "csrf_token": client.cookies[CSRF_COOKIE]},
        headers={"host": "secondfest.example"},
    )
    assert known.status_code == unknown.status_code == 200
    assert known.text == unknown.text
