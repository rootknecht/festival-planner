"""M5: car sharing — seats, double-booking, stops, geocoding, the map."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, reset_engine_for_tests
from app.main import create_app
from app.models.festival import Participant
from app.models.transport import Car, CarSeat, CarStop, GeocodeCache
from app.models.user import User
from app.security.csrf import CSRF_COOKIE
from app.services import geocode as geocode_service
from app.services import notifications
from app.services.mail import capture_sink
from app.services.passwords import hash_password

PASSWORD = "a-long-enough-password"


@pytest.fixture(autouse=True)
def no_live_geocoding(monkeypatch):
    """Tests must never call the public Nominatim service."""
    calls: list[str] = []

    def fake_lookup(query: str, settings=None):
        calls.append(query)
        if "nowhere" in query.lower():
            return None
        return 53.5, 9.9, f"resolved: {query}"

    monkeypatch.setattr(geocode_service, "lookup", fake_lookup)
    return calls


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
    token = client.cookies[CSRF_COOKIE]
    client.post("/login", data={"email": email, "password": PASSWORD, "csrf_token": token})
    return client


def csrf(client: TestClient) -> str:
    return client.cookies[CSRF_COOKIE]


@pytest.fixture()
def festival(factory, settings):
    owner = client_for(settings, "owner@example.org")
    created = owner.post("/festivals", data={"name": "Wacken", "csrf_token": csrf(owner)})
    festival_id = int(created.headers["location"].rsplit("/", 1)[1])
    for name in ("Kai", "Sabine"):
        owner.post(
            f"/festivals/{festival_id}/participants",
            data={"display_name": name, "csrf_token": csrf(owner)},
        )
    return festival_id, owner


def people(factory, festival_id) -> dict[str, Participant]:
    with factory() as db:
        rows = db.scalars(
            select(Participant).where(Participant.festival_id == festival_id)
        ).all()
    return {row.display_name: row for row in rows}


def add_car(client, festival_id, driver_id=None, seats=5, icon="van", color="blue"):
    return client.post(
        f"/festivals/{festival_id}/transport/cars",
        data={
            "icon": icon,
            "color": color,
            "seats_total": seats,
            "driver_participant_id": str(driver_id or ""),
            "csrf_token": csrf(client),
        },
    )


def seat(client, festival_id, car_id, participant_id):
    return client.post(
        f"/festivals/{festival_id}/transport/cars/{car_id}/seats",
        data={"participant_id": participant_id, "csrf_token": csrf(client)},
    )


def only_car(factory) -> Car:
    with factory() as db:
        return db.scalars(select(Car)).first()


def test_a_car_shows_its_driver_and_free_seats(festival, factory) -> None:
    festival_id, owner = festival
    who = people(factory, festival_id)
    assert add_car(owner, festival_id, who["Owner"].id, seats=4).status_code == 303

    page = owner.get(f"/festivals/{festival_id}/transport").text
    assert "Owner" in page
    assert "1 / 4" in page
    assert page.count("free seat") == 3


def test_total_seats_include_the_driver(festival, factory) -> None:
    festival_id, owner = festival
    who = people(factory, festival_id)
    add_car(owner, festival_id, who["Owner"].id, seats=2)
    car = only_car(factory)
    assert seat(owner, festival_id, car.id, who["Kai"].id).status_code == 303
    # 2 seats total: driver + one passenger. The third person does not fit.
    assert seat(owner, festival_id, car.id, who["Sabine"].id).status_code == 409


def test_a_person_cannot_sit_in_two_cars(festival, factory) -> None:
    festival_id, owner = festival
    who = people(factory, festival_id)
    add_car(owner, festival_id, who["Owner"].id, seats=5)
    first = only_car(factory)
    seat(owner, festival_id, first.id, who["Kai"].id)

    add_car(owner, festival_id, None, seats=5, icon="bus", color="green")
    with factory() as db:
        second = db.scalars(select(Car).order_by(Car.id.desc())).first()
    assert seat(owner, festival_id, second.id, who["Kai"].id).status_code == 409


def test_a_driver_of_one_car_cannot_drive_another(festival, factory) -> None:
    festival_id, owner = festival
    who = people(factory, festival_id)
    add_car(owner, festival_id, who["Owner"].id)
    assert add_car(owner, festival_id, who["Owner"].id, icon="bus").status_code == 409


def test_seat_limits_are_clamped_to_one_through_five(festival, factory) -> None:
    festival_id, owner = festival
    add_car(owner, festival_id, None, seats=99)
    assert only_car(factory).seats_total == 5


def test_unassigned_people_are_listed_until_they_get_a_seat(festival, factory) -> None:
    festival_id, owner = festival
    who = people(factory, festival_id)
    page = owner.get(f"/festivals/{festival_id}/transport").text
    assert "Not in a car yet" in page

    add_car(owner, festival_id, who["Owner"].id)
    car = only_car(factory)
    seat(owner, festival_id, car.id, who["Kai"].id)
    seat(owner, festival_id, car.id, who["Sabine"].id)
    page = owner.get(f"/festivals/{festival_id}/transport").text
    assert "Everybody has a seat." in page


def test_being_seated_notifies_the_passenger(factory, settings, festival) -> None:
    festival_id, owner = festival
    who = people(factory, festival_id)
    capture_sink().clear()
    owner.post(
        f"/festivals/{festival_id}/invites",
        data={
            "email": "friend@example.org",
            "claims_participant_id": str(who["Kai"].id),
            "csrf_token": csrf(owner),
        },
    )
    token = capture_sink().sent[-1].body.split("/invite/")[1].split()[0]
    joiner = TestClient(create_app(settings), follow_redirects=False)
    joiner.get(f"/invite/{token}")
    joiner.post(f"/invite/{token}", data={"csrf_token": joiner.cookies[CSRF_COOKIE]})

    add_car(owner, festival_id, who["Owner"].id)
    car = only_car(factory)
    seat(owner, festival_id, car.id, who["Kai"].id)

    with factory() as db:
        friend = db.scalar(select(User).where(User.email == "friend@example.org"))
        blocks = notifications.open_for(db, friend.id, festival_id)
    assert [b.message_key for b in blocks] == ["change.car.added"]

    friend_client = client_for(settings, "friend@example.org")
    assert "placed you in" in friend_client.get(f"/festivals/{festival_id}").text


def test_taking_someone_out_frees_the_seat(festival, factory) -> None:
    festival_id, owner = festival
    who = people(factory, festival_id)
    add_car(owner, festival_id, who["Owner"].id, seats=3)
    car = only_car(factory)
    seat(owner, festival_id, car.id, who["Kai"].id)

    removed = owner.post(
        f"/festivals/{festival_id}/transport/cars/{car.id}/seats/remove",
        data={"participant_id": who["Kai"].id, "csrf_token": csrf(owner)},
    )
    assert removed.status_code == 303
    with factory() as db:
        assert db.scalars(select(CarSeat)).all() == []


def test_a_departure_address_is_geocoded_once_and_cached(festival, factory,
                                                         no_live_geocoding) -> None:
    festival_id, owner = festival
    add_car(owner, festival_id, None)
    car = only_car(factory)
    for _ in range(3):
        owner.post(
            f"/festivals/{festival_id}/transport/cars/{car.id}/departure",
            data={"address": "Hamburg", "csrf_token": csrf(owner)},
        )
    assert no_live_geocoding == ["Hamburg"]  # looked up once, then cached
    with factory() as db:
        stored = db.get(Car, car.id)
        assert (stored.departure_lat, stored.departure_lon) == (53.5, 9.9)
        assert len(db.scalars(select(GeocodeCache)).all()) == 1


def test_an_address_that_cannot_be_found_is_still_stored(festival, factory) -> None:
    festival_id, owner = festival
    add_car(owner, festival_id, None)
    car = only_car(factory)
    owner.post(
        f"/festivals/{festival_id}/transport/cars/{car.id}/departure",
        data={"address": "Nowhere at all", "csrf_token": csrf(owner)},
    )
    with factory() as db:
        stored = db.get(Car, car.id)
    assert stored.departure_address == "Nowhere at all"
    assert stored.departure_lat is None


def test_stops_keep_their_order_and_appear_on_the_map(festival, factory) -> None:
    festival_id, owner = festival
    who = people(factory, festival_id)
    add_car(owner, festival_id, who["Owner"].id)
    car = only_car(factory)
    owner.post(
        f"/festivals/{festival_id}/transport/cars/{car.id}/departure",
        data={"address": "Hamburg", "csrf_token": csrf(owner)},
    )
    for address in ("Bremen", "Hannover"):
        owner.post(
            f"/festivals/{festival_id}/transport/cars/{car.id}/stops",
            data={"address": address, "csrf_token": csrf(owner)},
        )
    with factory() as db:
        stops = db.scalars(select(CarStop).order_by(CarStop.position)).all()
    assert [stop.address for stop in stops] == ["Bremen", "Hannover"]

    page = owner.get(f"/festivals/{festival_id}/transport").text
    assert "data-routes" in page
    assert "Bremen" in page and "Hannover" in page


def test_a_stop_without_an_address_is_refused(festival, factory) -> None:
    festival_id, owner = festival
    add_car(owner, festival_id, None)
    car = only_car(factory)
    response = owner.post(
        f"/festivals/{festival_id}/transport/cars/{car.id}/stops",
        data={"address": "   ", "csrf_token": csrf(owner)},
    )
    assert response.status_code == 200
    assert "needs an address" in response.text


def test_a_stranger_cannot_touch_the_cars(factory, settings, festival) -> None:
    festival_id, owner = festival
    who = people(factory, festival_id)
    add_car(owner, festival_id, who["Owner"].id)
    car = only_car(factory)
    with factory() as db:
        db.add(
            User(
                email="stranger@example.org",
                password_hash=hash_password(PASSWORD),
                display_name="Stranger",
            )
        )
        db.commit()
    stranger = client_for(settings, "stranger@example.org")
    assert stranger.get(f"/festivals/{festival_id}/transport").status_code == 404
    assert seat(stranger, festival_id, car.id, who["Kai"].id).status_code == 404


def test_a_car_from_another_festival_is_not_reachable(factory, settings, festival) -> None:
    festival_id, owner = festival
    who = people(factory, festival_id)
    add_car(owner, festival_id, who["Owner"].id)
    car = only_car(factory)
    other = owner.post("/festivals", data={"name": "Hellfest", "csrf_token": csrf(owner)})
    other_id = int(other.headers["location"].rsplit("/", 1)[1])
    assert seat(owner, other_id, car.id, who["Kai"].id).status_code == 404


def test_an_archived_festival_freezes_the_cars(festival, factory) -> None:
    festival_id, owner = festival
    who = people(factory, festival_id)
    add_car(owner, festival_id, who["Owner"].id)
    car = only_car(factory)
    owner.post(f"/festivals/{festival_id}/archive", data={"csrf_token": csrf(owner)})

    assert owner.get(f"/festivals/{festival_id}/transport").status_code == 200
    assert seat(owner, festival_id, car.id, who["Kai"].id).status_code == 409
    assert add_car(owner, festival_id, None).status_code == 409


def test_the_venue_address_is_geocoded_when_it_changes(festival, factory,
                                                       no_live_geocoding) -> None:
    """The map needs a destination marker, and the venue only moves when edited."""
    festival_id, owner = festival
    owner.post(
        f"/festivals/{festival_id}/settings",
        data={"name": "Wacken", "address_text": "Wacken, Germany", "csrf_token": csrf(owner)},
    )
    with factory() as db:
        from app.models.festival import Festival

        stored = db.get(Festival, festival_id)
    assert (stored.lat, stored.lon) == (53.5, 9.9)
    assert no_live_geocoding == ["Wacken, Germany"]

    # saving again without touching the address must not ask the geocoder again
    owner.post(
        f"/festivals/{festival_id}/settings",
        data={"name": "Wacken Open Air", "address_text": "Wacken, Germany",
              "csrf_token": csrf(owner)},
    )
    assert no_live_geocoding == ["Wacken, Germany"]
