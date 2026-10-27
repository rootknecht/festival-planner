"""Integration tests for festivals, participants, invitations and access rules.

The authorisation assertions here are the point of the file: a non-participant
must not be able to tell that a festival exists at all.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, reset_engine_for_tests
from app.main import create_app
from app.models.festival import Festival, Invite, Participant
from app.models.user import User
from app.security.csrf import CSRF_COOKIE
from app.services.mail import capture_sink
from app.services.passwords import hash_password

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
        for email, name in (
            ("owner@example.org", "Owner"),
            ("friend@example.org", "Friend"),
            ("stranger@example.org", "Stranger"),
        ):
            db.add(
                User(email=email, password_hash=hash_password(PASSWORD), display_name=name)
            )
        db.commit()
    capture_sink().clear()
    return session_factory


@pytest.fixture()
def settings() -> Settings:
    return Settings(_env_file=None, environment="test", base_url="https://example.org")


def make_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), follow_redirects=False)


def login(client: TestClient, email: str) -> str:
    client.get("/login")
    token = client.cookies[CSRF_COOKIE]
    response = client.post(
        "/login", data={"email": email, "password": PASSWORD, "csrf_token": token}
    )
    assert response.status_code == 303
    return token


def create_festival(client: TestClient, csrf: str, name: str = "Wacken") -> int:
    response = client.post(
        "/festivals",
        data={"name": name, "start_date": "2027-07-28", "end_date": "2027-07-31",
              "csrf_token": csrf},
    )
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[1])


def test_creator_becomes_admin_and_sees_the_dashboard(factory, settings) -> None:
    client = make_client(settings)
    csrf = login(client, "owner@example.org")
    festival_id = create_festival(client, csrf)

    dashboard = client.get(f"/festivals/{festival_id}")
    assert dashboard.status_code == 200
    assert "Wacken" in dashboard.text
    assert "Owner" in dashboard.text

    listing = client.get("/")
    assert "Wacken" in listing.text


def test_a_festival_without_a_name_is_refused(factory, settings) -> None:
    client = make_client(settings)
    csrf = login(client, "owner@example.org")
    response = client.post("/festivals", data={"name": "   ", "csrf_token": csrf})
    assert response.status_code == 400


def test_a_stranger_gets_404_not_403_on_every_festival_route(factory, settings) -> None:
    owner = make_client(settings)
    owner_csrf = login(owner, "owner@example.org")
    festival_id = create_festival(owner, owner_csrf)

    stranger = make_client(settings)
    stranger_csrf = login(stranger, "stranger@example.org")

    assert stranger.get(f"/festivals/{festival_id}").status_code == 404
    assert stranger.get(f"/festivals/{festival_id}/settings").status_code == 404
    assert stranger.post(
        f"/festivals/{festival_id}/participants",
        data={"display_name": "Nobody", "csrf_token": stranger_csrf},
    ).status_code == 404
    assert stranger.post(
        f"/festivals/{festival_id}/invites",
        data={"email": "x@example.org", "csrf_token": stranger_csrf},
    ).status_code == 404
    assert stranger.get("/").text.count("Wacken") == 0


def test_an_anonymous_visitor_is_sent_to_the_login_page(factory, settings) -> None:
    client = make_client(settings)
    response = client.get("/", headers={"accept": "text/html"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_a_member_may_view_but_not_administer(factory, settings) -> None:
    owner = make_client(settings)
    owner_csrf = login(owner, "owner@example.org")
    festival_id = create_festival(owner, owner_csrf)
    owner.post(
        f"/festivals/{festival_id}/invites",
        data={"email": "friend@example.org", "csrf_token": owner_csrf},
    )
    token = capture_sink().sent[-1].body.split("/invite/")[1].split()[0]

    friend = make_client(settings)
    friend.get(f"/invite/{token}")
    friend_csrf = friend.cookies[CSRF_COOKIE]
    accepted = friend.post(f"/invite/{token}", data={"csrf_token": friend_csrf})
    assert accepted.status_code == 303  # existing account: join, then sign in

    login(friend, "friend@example.org")
    assert friend.get(f"/festivals/{festival_id}").status_code == 200
    assert friend.get(f"/festivals/{festival_id}/settings").status_code == 403


def test_invitation_creates_an_account_and_joins_the_festival(factory, settings) -> None:
    owner = make_client(settings)
    owner_csrf = login(owner, "owner@example.org")
    festival_id = create_festival(owner, owner_csrf)
    owner.post(
        f"/festivals/{festival_id}/invites",
        data={"email": "newcomer@example.org", "csrf_token": owner_csrf},
    )
    token = capture_sink().sent[-1].body.split("/invite/")[1].split()[0]

    newcomer = make_client(settings)
    page = newcomer.get(f"/invite/{token}")
    assert page.status_code == 200
    csrf = newcomer.cookies[CSRF_COOKIE]
    response = newcomer.post(
        f"/invite/{token}",
        data={
            "display_name": "Newcomer",
            "password": PASSWORD,
            "password_repeat": PASSWORD,
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/festivals/{festival_id}"
    assert newcomer.get(f"/festivals/{festival_id}").status_code == 200


def test_an_invitation_cannot_be_used_twice(factory, settings) -> None:
    owner = make_client(settings)
    owner_csrf = login(owner, "owner@example.org")
    festival_id = create_festival(owner, owner_csrf)
    owner.post(
        f"/festivals/{festival_id}/invites",
        data={"email": "newcomer@example.org", "csrf_token": owner_csrf},
    )
    token = capture_sink().sent[-1].body.split("/invite/")[1].split()[0]

    first = make_client(settings)
    first.get(f"/invite/{token}")
    csrf = first.cookies[CSRF_COOKIE]
    first.post(
        f"/invite/{token}",
        data={"display_name": "Newcomer", "password": PASSWORD,
              "password_repeat": PASSWORD, "csrf_token": csrf},
    )
    second = make_client(settings)
    assert second.get(f"/invite/{token}").status_code == 410


def test_a_revoked_invitation_stops_working(factory, settings) -> None:
    client = make_client(settings)
    csrf = login(client, "owner@example.org")
    festival_id = create_festival(client, csrf)
    client.post(
        f"/festivals/{festival_id}/invites",
        data={"email": "newcomer@example.org", "csrf_token": csrf},
    )
    token = capture_sink().sent[-1].body.split("/invite/")[1].split()[0]
    invite_id = 1
    assert client.post(
        f"/festivals/{festival_id}/invites/{invite_id}/revoke", data={"csrf_token": csrf}
    ).status_code == 303
    assert make_client(settings).get(f"/invite/{token}").status_code == 410


def test_a_placeholder_is_claimed_and_keeps_its_identity(factory, settings) -> None:
    client = make_client(settings)
    csrf = login(client, "owner@example.org")
    festival_id = create_festival(client, csrf)
    client.post(
        f"/festivals/{festival_id}/participants",
        data={"display_name": "Partner of Owner", "csrf_token": csrf},
    )
    with factory() as db:
        placeholder = db.scalar(
            select(Participant).where(Participant.display_name == "Partner of Owner")
        )
        assert placeholder.is_placeholder is True
        placeholder_id = placeholder.id

    client.post(
        f"/festivals/{festival_id}/invites",
        data={
            "email": "partner@example.org",
            "claims_participant_id": str(placeholder_id),
            "csrf_token": csrf,
        },
    )
    token = capture_sink().sent[-1].body.split("/invite/")[1].split()[0]

    partner = make_client(settings)
    partner.get(f"/invite/{token}")
    partner_csrf = partner.cookies[CSRF_COOKIE]
    partner.post(
        f"/invite/{token}",
        data={"display_name": "Partner", "password": PASSWORD,
              "password_repeat": PASSWORD, "csrf_token": partner_csrf},
    )

    with factory() as db:
        claimed = db.get(Participant, placeholder_id)
        assert claimed.user_id is not None
        assert claimed.display_name == "Partner"
        rows = db.scalars(
            select(Participant).where(Participant.festival_id == festival_id)
        ).all()
        # owner + the claimed placeholder, not a third row
        assert len(rows) == 2


def test_the_last_admin_cannot_be_demoted_or_removed(factory, settings) -> None:
    client = make_client(settings)
    csrf = login(client, "owner@example.org")
    festival_id = create_festival(client, csrf)
    with factory() as db:
        admin = db.scalar(
            select(Participant).where(Participant.festival_id == festival_id)
        )
    demote = client.post(
        f"/festivals/{festival_id}/participants/{admin.id}/role",
        data={"role": "member", "csrf_token": csrf},
    )
    assert demote.status_code == 409
    remove = client.post(
        f"/festivals/{festival_id}/participants/{admin.id}/delete", data={"csrf_token": csrf}
    )
    assert remove.status_code == 409


def test_a_placeholder_cannot_be_made_admin(factory, settings) -> None:
    client = make_client(settings)
    csrf = login(client, "owner@example.org")
    festival_id = create_festival(client, csrf)
    client.post(
        f"/festivals/{festival_id}/participants",
        data={"display_name": "Ghost", "csrf_token": csrf},
    )
    with factory() as db:
        ghost = db.scalar(select(Participant).where(Participant.display_name == "Ghost"))
    response = client.post(
        f"/festivals/{festival_id}/participants/{ghost.id}/role",
        data={"role": "admin", "csrf_token": csrf},
    )
    assert response.status_code == 400


def test_an_archived_festival_is_read_only(factory, settings) -> None:
    client = make_client(settings)
    csrf = login(client, "owner@example.org")
    festival_id = create_festival(client, csrf)
    assert client.post(
        f"/festivals/{festival_id}/archive", data={"csrf_token": csrf}
    ).status_code == 303

    assert client.get(f"/festivals/{festival_id}").status_code == 200
    assert client.post(
        f"/festivals/{festival_id}/participants",
        data={"display_name": "Late Arrival", "csrf_token": csrf},
    ).status_code == 409
    assert client.post(
        f"/festivals/{festival_id}/invites",
        data={"email": "late@example.org", "csrf_token": csrf},
    ).status_code == 409

    assert client.post(
        f"/festivals/{festival_id}/unarchive", data={"csrf_token": csrf}
    ).status_code == 303
    assert client.post(
        f"/festivals/{festival_id}/participants",
        data={"display_name": "Late Arrival", "csrf_token": csrf},
    ).status_code == 303


def test_a_suspended_festival_disappears_for_its_participants(factory, settings) -> None:
    client = make_client(settings)
    csrf = login(client, "owner@example.org")
    festival_id = create_festival(client, csrf)
    with factory() as db:
        festival = db.get(Festival, festival_id)
        festival.status = "suspended"
        db.commit()
    assert client.get(f"/festivals/{festival_id}").status_code == 404
    assert "Wacken" not in client.get("/").text


def test_invites_are_scoped_to_their_festival(factory, settings) -> None:
    owner = make_client(settings)
    owner_csrf = login(owner, "owner@example.org")
    first = create_festival(owner, owner_csrf, "Wacken")
    second = create_festival(owner, owner_csrf, "Hellfest")
    owner.post(
        f"/festivals/{first}/invites",
        data={"email": "newcomer@example.org", "csrf_token": owner_csrf},
    )
    with factory() as db:
        invite = db.scalars(select(Invite)).one()
    response = owner.post(
        f"/festivals/{second}/invites/{invite.id}/revoke", data={"csrf_token": owner_csrf}
    )
    assert response.status_code == 404
