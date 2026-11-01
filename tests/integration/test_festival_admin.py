"""Invitation status, tidying, and deleting a festival for good.

Three things an admin needs and one of them is irreversible, so the tests
cover the refusals as carefully as the happy paths.
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, _apply_sqlite_pragmas, reset_engine_for_tests
from app.main import create_app
from app.models.bringlist import BringRow
from app.models.festival import Festival, Invite, Participant
from app.models.transport import Car, Train
from app.models.user import User, utcnow
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
    # Production attaches these pragmas to every connection, and one of them
    # is foreign_keys=ON. A test engine without it does NOT cascade deletes,
    # so a cascade test would quietly prove nothing — it would fail here
    # while working live. Mirror the application engine instead.
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    Base.metadata.create_all(engine)
    reset_engine_for_tests(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with session_factory() as db:
        for email, name in (("owner@example.org", "Owner"), ("member@example.org", "Member")):
            db.add(User(email=email, password_hash=hash_password(PASSWORD), display_name=name))
        db.commit()
    capture_sink().clear()
    return session_factory


@pytest.fixture()
def settings() -> Settings:
    return Settings(_env_file=None, environment="test", base_url="https://example.org")


def client_for(settings: Settings, email: str = "owner@example.org") -> TestClient:
    client = TestClient(create_app(settings), follow_redirects=False)
    client.get("/login")
    client.post(
        "/login",
        data={"email": email, "password": PASSWORD, "csrf_token": client.cookies[CSRF_COOKIE]},
    )
    return client


def csrf(client: TestClient) -> str:
    return client.cookies[CSRF_COOKIE]


@pytest.fixture()
def festival(factory, settings):
    owner = client_for(settings)
    created = owner.post("/festivals", data={"name": "Wacken", "csrf_token": csrf(owner)})
    return int(created.headers["location"].rsplit("/", 1)[1]), owner


def invite(client, festival_id, email="guest@example.org") -> str:
    """Create an invitation and return its raw token."""
    response = client.post(
        f"/festivals/{festival_id}/invites",
        data={"email": email, "csrf_token": csrf(client)},
    )
    assert response.status_code == 303
    return response.headers["location"].split("link=")[1]


def settings_page(client, festival_id) -> str:
    return client.get(f"/festivals/{festival_id}/settings").text


# --- status ------------------------------------------------------------------


def test_the_admin_sees_what_became_of_every_invitation(festival, factory) -> None:
    """A bounce leaves no trace in the app, so status is all there is."""
    festival_id, owner = festival
    invite(owner, festival_id, "open@example.org")
    invite(owner, festival_id, "revoked@example.org")
    invite(owner, festival_id, "expired@example.org")

    with factory() as db:
        revoked = db.scalars(
            select(Invite).where(Invite.email == "revoked@example.org")
        ).first()
        revoked.revoked_at = utcnow()
        expired = db.scalars(
            select(Invite).where(Invite.email == "expired@example.org")
        ).first()
        expired.expires_at = utcnow() - dt.timedelta(days=1)
        db.commit()
        statuses = {i.email: i.status for i in db.scalars(select(Invite))}

    assert statuses == {
        "open@example.org": "open",
        "revoked@example.org": "revoked",
        "expired@example.org": "expired",
    }

    page = settings_page(owner, festival_id)
    for email in statuses:
        assert email in page
    # An accepted invitation used to vanish from the page entirely.
    assert page.count("New link") == 3


def test_an_accepted_invitation_stays_accepted_after_its_window_passes(
    festival, factory
) -> None:
    festival_id, owner = festival
    invite(owner, festival_id)
    with factory() as db:
        row = db.scalars(select(Invite)).first()
        row.accepted_at = utcnow()
        row.expires_at = utcnow() - dt.timedelta(days=9)
        db.commit()
        assert row.status == "accepted"
        assert row.is_finished


def test_an_accepted_invitation_offers_no_new_link(festival, factory) -> None:
    festival_id, owner = festival
    invite(owner, festival_id)
    with factory() as db:
        row = db.scalars(select(Invite)).first()
        row.accepted_at = utcnow()
        db.commit()
        invite_id = row.id

    assert "New link" not in settings_page(owner, festival_id)
    refused = owner.post(
        f"/festivals/{festival_id}/invites/{invite_id}/relink",
        data={"csrf_token": csrf(owner)},
    )
    assert refused.status_code == 400


# --- a fresh link ------------------------------------------------------------


def test_a_new_link_replaces_the_old_one_and_works(festival, factory, settings) -> None:
    """The point of the feature: mail failed, so hand the link over directly."""
    festival_id, owner = festival
    old_token = invite(owner, festival_id, "guest@example.org")
    with factory() as db:
        invite_id = db.scalars(select(Invite)).first().id

    capture_sink().clear()
    response = owner.post(
        f"/festivals/{festival_id}/invites/{invite_id}/relink",
        data={"csrf_token": csrf(owner)},
    )
    assert response.status_code == 303
    new_token = response.headers["location"].split("link=")[1]
    assert new_token != old_token
    # No second mail: the whole reason for this button is that mail did not work.
    assert capture_sink().sent == []

    with factory() as db:
        rows = {i.token_hash: i for i in db.scalars(select(Invite))}
        assert len(rows) == 2
        statuses = sorted(i.status for i in rows.values())
    assert statuses == ["open", "revoked"]

    # The old link is dead, the new one lets the guest in.
    guest = TestClient(create_app(settings), follow_redirects=False)
    assert guest.get(f"/invite/{old_token}").status_code == 410
    assert guest.get(f"/invite/{new_token}").status_code == 200


# --- clearing ----------------------------------------------------------------


def test_clearing_removes_only_the_finished_invitations(festival, factory) -> None:
    festival_id, owner = festival
    invite(owner, festival_id, "open@example.org")
    invite(owner, festival_id, "revoked@example.org")
    invite(owner, festival_id, "accepted@example.org")

    with factory() as db:
        db.scalars(
            select(Invite).where(Invite.email == "revoked@example.org")
        ).first().revoked_at = utcnow()
        db.scalars(
            select(Invite).where(Invite.email == "accepted@example.org")
        ).first().accepted_at = utcnow()
        db.commit()

    response = owner.post(
        f"/festivals/{festival_id}/invites/clear", data={"csrf_token": csrf(owner)}
    )
    assert response.status_code == 303
    assert "cleared=2" in response.headers["location"]

    with factory() as db:
        left = [(i.email, i.status) for i in db.scalars(select(Invite))]
    assert left == [("open@example.org", "open")]


def test_clearing_with_nothing_finished_changes_nothing(festival, factory) -> None:
    festival_id, owner = festival
    invite(owner, festival_id)
    owner.post(f"/festivals/{festival_id}/invites/clear", data={"csrf_token": csrf(owner)})
    with factory() as db:
        assert len(list(db.scalars(select(Invite)))) == 1


def test_a_member_cannot_clear_or_relink(festival, factory, settings) -> None:
    festival_id, owner = festival
    token = invite(owner, festival_id, "member@example.org")
    joiner = TestClient(create_app(settings), follow_redirects=False)
    joiner.get(f"/invite/{token}")
    joiner.post(f"/invite/{token}", data={"csrf_token": joiner.cookies[CSRF_COOKIE]})
    member = client_for(settings, "member@example.org")

    with factory() as db:
        invite_id = db.scalars(select(Invite)).first().id
    for url in (
        f"/festivals/{festival_id}/invites/clear",
        f"/festivals/{festival_id}/invites/{invite_id}/relink",
    ):
        assert member.post(url, data={"csrf_token": csrf(member)}).status_code == 403


# --- deleting a festival -----------------------------------------------------


def seed_content(client, festival_id, factory) -> None:
    """A festival with something in every corner, so deletion has work to do."""
    client.post(
        f"/festivals/{festival_id}/transport/cars",
        data={"color": "red", "seats_total": 4, "csrf_token": csrf(client)},
    )
    client.post(
        f"/festivals/{festival_id}/transport/trains",
        data={"number": "ICE 1", "departure_station": "Hamburg", "csrf_token": csrf(client)},
    )
    client.post(
        f"/festivals/{festival_id}/bringlist/rows",
        data={"label": "Beer", "csrf_token": csrf(client)},
    )
    invite(client, festival_id, "guest@example.org")


def test_deleting_needs_the_name_typed_exactly(festival, factory) -> None:
    festival_id, owner = festival
    for wrong in ("", "wacken", "Wacken 2027", " Wacke "):
        response = owner.post(
            f"/festivals/{festival_id}/delete",
            data={"confirm_name": wrong, "csrf_token": csrf(owner)},
        )
        assert response.status_code == 200, wrong
        assert "not deleted" in response.text
        with factory() as db:
            assert db.get(Festival, festival_id) is not None


def test_surrounding_whitespace_is_forgiven(festival, factory) -> None:
    """A name pasted with a trailing space is still the name."""
    festival_id, owner = festival
    response = owner.post(
        f"/festivals/{festival_id}/delete",
        data={"confirm_name": "  Wacken  ", "csrf_token": csrf(owner)},
    )
    assert response.status_code == 303
    with factory() as db:
        assert db.get(Festival, festival_id) is None


def test_deleting_takes_every_child_row_with_it(festival, factory) -> None:
    festival_id, owner = festival
    seed_content(owner, festival_id, factory)

    with factory() as db:
        before = {
            table.name: db.scalar(select(func.count()).select_from(table))
            for table in Base.metadata.sorted_tables
        }
    assert before["car"] and before["train"] and before["bring_row"] and before["invite"]
    assert before["participant"]

    response = owner.post(
        f"/festivals/{festival_id}/delete",
        data={"confirm_name": "Wacken", "csrf_token": csrf(owner)},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    with factory() as db:
        assert db.get(Festival, festival_id) is None
        for model in (Participant, Invite, Car, Train, BringRow):
            assert list(db.scalars(select(model))) == [], model.__name__
        # The accounts themselves survive: a user is not festival content.
        assert len(list(db.scalars(select(User)))) == 2


def test_a_member_cannot_delete_the_festival(festival, factory, settings) -> None:
    festival_id, owner = festival
    token = invite(owner, festival_id, "member@example.org")
    joiner = TestClient(create_app(settings), follow_redirects=False)
    joiner.get(f"/invite/{token}")
    joiner.post(f"/invite/{token}", data={"csrf_token": joiner.cookies[CSRF_COOKIE]})
    member = client_for(settings, "member@example.org")

    refused = member.post(
        f"/festivals/{festival_id}/delete",
        data={"confirm_name": "Wacken", "csrf_token": csrf(member)},
    )
    assert refused.status_code == 403
    with factory() as db:
        assert db.get(Festival, festival_id) is not None


def test_a_stranger_cannot_delete_the_festival(festival, factory, settings) -> None:
    festival_id, _owner = festival
    stranger = client_for(settings, "member@example.org")
    refused = stranger.post(
        f"/festivals/{festival_id}/delete",
        data={"confirm_name": "Wacken", "csrf_token": csrf(stranger)},
    )
    assert refused.status_code in (403, 404)
    with factory() as db:
        assert db.get(Festival, festival_id) is not None


def test_the_delete_form_asks_for_the_name_and_warns(festival, factory) -> None:
    festival_id, owner = festival
    page = settings_page(owner, festival_id)
    assert 'name="confirm_name"' in page
    assert "There is no undo" in page
    # Archiving is offered right beside it as the reversible alternative.
    assert "Archive after the event" in page
