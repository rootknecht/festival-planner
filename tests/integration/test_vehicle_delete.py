"""Deleting a vehicle: who may, and what the control looks like.

Two things are asserted here that used to be true only by accident.

The CONTROL is a button labelled "Delete", not a bare cross. A cross sitting
beside a passenger list is one mis-tap away from throwing everybody out of a
car, and an icon alone does not say what it does.

The PERMISSION lives in the route. The page only ever offered the control to
an admin or the vehicle's owner, but the endpoint accepted it from any
member — a hidden button is not an authorisation check.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, reset_engine_for_tests
from app.main import create_app
from app.models.festival import Participant
from app.models.transport import Car, Train
from app.models.user import User
from app.security.csrf import CSRF_COOKIE
from app.services import geocode as geocode_service
from app.services.mail import capture_sink
from app.services.passwords import hash_password

PASSWORD = "a-long-enough-password"


@pytest.fixture(autouse=True)
def no_live_geocoding(monkeypatch):
    monkeypatch.setattr(
        geocode_service, "lookup", lambda query, settings=None: (53.5, 9.9, "resolved")
    )


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
    capture_sink().clear()
    return session_factory


@pytest.fixture()
def settings() -> Settings:
    return Settings(_env_file=None, environment="test", base_url="https://example.org")


def client_for(settings: Settings, email: str) -> TestClient:
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
    """An admin, plus a plain member who joined through an invitation."""
    owner = client_for(settings, "owner@example.org")
    created = owner.post("/festivals", data={"name": "Wacken", "csrf_token": csrf(owner)})
    festival_id = int(created.headers["location"].rsplit("/", 1)[1])

    capture_sink().clear()
    owner.post(
        f"/festivals/{festival_id}/invites",
        data={"email": "friend@example.org", "csrf_token": csrf(owner)},
    )
    token = capture_sink().sent[-1].body.split("/invite/")[1].split()[0]
    joiner = TestClient(create_app(settings), follow_redirects=False)
    joiner.get(f"/invite/{token}")
    joiner.post(f"/invite/{token}", data={"csrf_token": joiner.cookies[CSRF_COOKIE]})

    member = client_for(settings, "friend@example.org")
    return festival_id, owner, member


def participants(factory, festival_id) -> dict[str, Participant]:
    with factory() as db:
        return {
            p.display_name: p
            for p in db.scalars(
                select(Participant).where(Participant.festival_id == festival_id)
            )
        }


def add_car(client, festival_id, driver_id=None) -> int:
    data = {"color": "red", "seats_total": 5, "csrf_token": csrf(client)}
    if driver_id is not None:
        data["driver_participant_id"] = str(driver_id)
    client.post(f"/festivals/{festival_id}/transport/cars", data=data)
    return 0


def only_car(factory) -> Car:
    with factory() as db:
        return db.scalars(select(Car)).first()


def add_train(client, festival_id) -> None:
    client.post(
        f"/festivals/{festival_id}/transport/trains",
        data={"number": "ICE 574", "departure_station": "Hamburg", "csrf_token": csrf(client)},
    )


def only_train(factory) -> Train:
    with factory() as db:
        return db.scalars(select(Train)).first()


def test_the_car_edit_form_offers_a_labelled_delete_button(festival, factory) -> None:
    festival_id, owner, _member = festival
    add_car(owner, festival_id)
    car = only_car(factory)
    page = owner.get(f"/festivals/{festival_id}/transport").text

    assert f'formaction="/festivals/{festival_id}/transport/cars/{car.id}/delete"' in page
    # The word, not a cross: the control has to say what it does.
    delete_button = page.split(f"/transport/cars/{car.id}/delete")[1][:120]
    assert "Delete" in delete_button
    assert "btn--danger" in page.split(f"/transport/cars/{car.id}/delete")[0][-200:]


def test_the_train_edit_form_offers_the_same_labelled_delete_button(festival, factory) -> None:
    festival_id, owner, _member = festival
    add_train(owner, festival_id)
    train = only_train(factory)
    page = owner.get(f"/festivals/{festival_id}/transport").text

    assert f'formaction="/festivals/{festival_id}/transport/trains/{train.id}/delete"' in page
    assert "Delete" in page.split(f"/transport/trains/{train.id}/delete")[1][:120]


def test_no_vehicle_is_deleted_by_an_unlabelled_cross(festival, factory) -> None:
    """Regression guard: the delete controls used to be bare close icons."""
    festival_id, owner, _member = festival
    add_car(owner, festival_id)
    add_train(owner, festival_id)
    page = owner.get(f"/festivals/{festival_id}/transport").text
    for marker in ("/transport/cars/", "/transport/trains/"):
        for chunk in page.split(marker):
            if chunk.startswith(("1/delete", "2/delete")):
                assert "linkbtn" not in chunk[:200]


def test_a_member_cannot_delete_somebody_elses_car(festival, factory) -> None:
    festival_id, owner, member = festival
    people = participants(factory, festival_id)
    add_car(owner, festival_id, people["Owner"].id)
    car = only_car(factory)

    refused = member.post(
        f"/festivals/{festival_id}/transport/cars/{car.id}/delete",
        data={"csrf_token": csrf(member)},
    )
    assert refused.status_code == 403
    assert only_car(factory) is not None

    # And the page does not offer them the control either.
    assert f"/transport/cars/{car.id}/delete" not in member.get(
        f"/festivals/{festival_id}/transport"
    ).text


def test_a_driver_may_delete_their_own_car(festival, factory) -> None:
    festival_id, owner, member = festival
    people = participants(factory, festival_id)
    add_car(owner, festival_id, people["Friend"].id)
    car = only_car(factory)

    response = member.post(
        f"/festivals/{festival_id}/transport/cars/{car.id}/delete",
        data={"csrf_token": csrf(member)},
    )
    assert response.status_code == 303
    assert only_car(factory) is None


def test_an_admin_may_delete_any_car(festival, factory) -> None:
    festival_id, owner, _member = festival
    people = participants(factory, festival_id)
    add_car(owner, festival_id, people["Friend"].id)
    car = only_car(factory)

    response = owner.post(
        f"/festivals/{festival_id}/transport/cars/{car.id}/delete",
        data={"csrf_token": csrf(owner)},
    )
    assert response.status_code == 303
    assert only_car(factory) is None


def test_a_member_cannot_delete_a_train_they_did_not_add(festival, factory) -> None:
    festival_id, owner, member = festival
    add_train(owner, festival_id)
    train = only_train(factory)

    refused = member.post(
        f"/festivals/{festival_id}/transport/trains/{train.id}/delete",
        data={"csrf_token": csrf(member)},
    )
    assert refused.status_code == 403
    assert only_train(factory) is not None


def test_whoever_added_the_train_may_delete_it(festival, factory) -> None:
    festival_id, _owner, member = festival
    add_train(member, festival_id)
    train = only_train(factory)

    response = member.post(
        f"/festivals/{festival_id}/transport/trains/{train.id}/delete",
        data={"csrf_token": csrf(member)},
    )
    assert response.status_code == 303
    assert only_train(factory) is None
