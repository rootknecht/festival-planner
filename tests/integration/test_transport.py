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
from app.models.transport import (
    MAX_SEATS,
    Car,
    CarSeat,
    CarStop,
    GeocodeCache,
    MeetingPoint,
)
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


def add_car(client, festival_id, driver_id=None, seats=5, color="blue",
            departure_date="", departure_time=""):
    return client.post(
        f"/festivals/{festival_id}/transport/cars",
        data={
            "color": color,
            "departure_date": departure_date,
            "departure_time": departure_time,
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

    add_car(owner, festival_id, None, seats=5, color="green")
    with factory() as db:
        second = db.scalars(select(Car).order_by(Car.id.desc())).first()
    assert seat(owner, festival_id, second.id, who["Kai"].id).status_code == 409


def test_a_driver_of_one_car_cannot_drive_another(festival, factory) -> None:
    festival_id, owner = festival
    who = people(factory, festival_id)
    add_car(owner, festival_id, who["Owner"].id)
    assert add_car(owner, festival_id, who["Owner"].id).status_code == 409


def test_seat_limits_are_clamped_to_the_allowed_range(festival, factory) -> None:
    """A minibus is a real thing; 99 seats is a typo."""
    festival_id, owner = festival
    add_car(owner, festival_id, None, seats=99)
    assert only_car(factory).seats_total == MAX_SEATS


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


# --- v0.2: departure time, editing, meeting points --------------------------


def edit_car(client, festival_id, car_id, **fields):
    data = {"csrf_token": csrf(client), **fields}
    return client.post(f"/festivals/{festival_id}/transport/cars/{car_id}/edit", data=data)


def add_meeting_point(client, festival_id, name="Gate services", address="Dinkelsbuehl",
                      meet_at="", car_ids=None):
    # Repeated form keys go in as a LIST VALUE. httpx rejects a list of tuples
    # for data= and silently sends it as raw bytes, which then fails CSRF for
    # reasons that look nothing like the real cause.
    data = {
        "csrf_token": csrf(client),
        "name": name,
        "address": address,
        "meet_at": meet_at,
        "car_ids": [str(car_id) for car_id in (car_ids or [])],
    }
    return client.post(f"/festivals/{festival_id}/transport/meeting-points", data=data)


def test_a_car_can_carry_a_departure_time(festival, factory) -> None:
    festival_id, owner = festival
    add_car(owner, festival_id, None, departure_date="2027-08-18", departure_time="06:30")
    car = only_car(factory)
    assert car.departure_at is not None
    assert car.departure_at.strftime("%Y-%m-%d %H:%M") == "2027-08-18 06:30"


def test_a_half_typed_departure_is_treated_as_unset_not_as_an_error(festival, factory) -> None:
    """Losing the whole form over a bad date would be worse than no date.

    This is the shape of the bug reported from real use: a single
    datetime-local keeps a half-entered value the user cannot clear and then
    refuses the form. Separate date and time fields have no partial state.
    """
    festival_id, owner = festival
    assert add_car(owner, festival_id, None, departure_date="2027-08").status_code == 303
    assert only_car(factory).departure_at is None


def test_a_car_stays_editable_after_creation(festival, factory) -> None:
    festival_id, owner = festival
    add_car(owner, festival_id, None, seats=4, color="blue")
    car = only_car(factory)
    assert edit_car(owner, festival_id, car.id, color="grey", seats_total=7,
                    departure_date="2027-08-17", departure_time="18:00").status_code == 303
    with factory() as db:
        fresh = db.get(Car, car.id)
        assert fresh.color == "grey"
        assert fresh.seats_total == 7
        assert fresh.departure_at.hour == 18


def test_grey_is_an_available_colour(festival, factory) -> None:
    festival_id, owner = festival
    add_car(owner, festival_id, None, color="grey")
    assert only_car(factory).color == "grey"


def test_an_unknown_colour_falls_back_instead_of_failing(festival, factory) -> None:
    festival_id, owner = festival
    add_car(owner, festival_id, None, color="chartreuse")
    assert only_car(factory).color in ("red",)


def test_shrinking_a_car_below_its_passengers_is_refused(festival, factory) -> None:
    """The alternative is throwing somebody out without telling them."""
    festival_id, owner = festival
    who = people(factory, festival_id)
    add_car(owner, festival_id, who["Owner"].id, seats=4)
    car = only_car(factory)
    seat(owner, festival_id, car.id, who["Kai"].id)
    seat(owner, festival_id, car.id, who["Sabine"].id)

    response = edit_car(owner, festival_id, car.id, seats_total=2)
    assert response.status_code == 200
    assert "fewer seats than there are people" in response.text
    with factory() as db:
        assert db.get(Car, car.id).seats_total == 4


def test_the_departure_time_can_be_cleared(festival, factory) -> None:
    festival_id, owner = festival
    add_car(owner, festival_id, None, departure_date="2027-08-18", departure_time="06:30")
    car = only_car(factory)
    edit_car(owner, festival_id, car.id, clear_departure="1")
    with factory() as db:
        assert db.get(Car, car.id).departure_at is None


def test_car_size_shows_as_icon_length(festival, factory) -> None:
    """The vehicle-type picker is gone; length carries the information now."""
    festival_id, owner = festival
    add_car(owner, festival_id, None, seats=2)
    small = only_car(factory).icon_width_px
    with factory() as db:
        car = db.get(Car, only_car(factory).id)
        car.seats_total = 8
        db.commit()
        assert car.icon_width_px > small


def test_a_full_car_says_so(festival, factory) -> None:
    festival_id, owner = festival
    who = people(factory, festival_id)
    add_car(owner, festival_id, who["Owner"].id, seats=1)
    page = owner.get(f"/festivals/{festival_id}/transport").text
    assert "full" in page
    with factory() as db:
        assert db.scalars(select(Car)).one().is_full is True


def test_an_admin_can_add_a_meeting_point_with_a_car_selection(festival, factory) -> None:
    festival_id, owner = festival
    who = people(factory, festival_id)
    add_car(owner, festival_id, who["Owner"].id)
    first = only_car(factory)
    add_car(owner, festival_id, who["Kai"].id)

    assert add_meeting_point(owner, festival_id, car_ids=[first.id]).status_code == 303
    with factory() as db:
        point = db.scalars(select(MeetingPoint)).one()
        assert point.name == "Gate services"
        assert point.car_ids == {first.id}


def test_not_every_car_has_to_join_a_meeting_point(festival, factory) -> None:
    """Latecomers must not be sent to a rendezvous that already happened."""
    festival_id, owner = festival
    who = people(factory, festival_id)  # noqa: F841 - seeds the participants
    add_car(owner, festival_id, who["Owner"].id)
    early = only_car(factory)
    add_car(owner, festival_id, who["Kai"].id)
    with factory() as db:
        late = [c for c in db.scalars(select(Car)).all() if c.id != early.id][0].id

    add_meeting_point(owner, festival_id, car_ids=[early.id, late])
    with factory() as db:
        point = db.scalars(select(MeetingPoint)).one()
    owner.post(
        f"/festivals/{festival_id}/transport/meeting-points/{point.id}/cars",
        data={"csrf_token": csrf(owner), "car_ids": [str(early.id)]},
    )
    with factory() as db:
        assert db.scalars(select(MeetingPoint)).one().car_ids == {early.id}


def test_a_member_cannot_create_or_change_a_meeting_point(festival, factory, settings) -> None:
    """A meeting point is a decision for the whole group, so admins only."""
    festival_id, owner = festival
    owner.post(
        f"/festivals/{festival_id}/invites",
        data={"email": "friend@example.org", "csrf_token": csrf(owner)},
    )
    token = capture_sink().sent[-1].body.split("/invite/")[1].split()[0]
    member = client_for(settings, "friend@example.org")
    member.get(f"/invite/{token}")
    member.post(f"/invite/{token}", data={"csrf_token": csrf(member)})
    assert add_meeting_point(member, festival_id).status_code == 403


def test_at_most_two_meeting_points(festival, factory) -> None:
    festival_id, owner = festival
    assert add_meeting_point(owner, festival_id, name="One", address="A").status_code == 303
    assert add_meeting_point(owner, festival_id, name="Two", address="B").status_code == 303
    assert add_meeting_point(owner, festival_id, name="Three", address="C").status_code == 409


def test_a_meeting_point_needs_a_name_and_an_address(festival, factory) -> None:
    festival_id, owner = festival
    assert add_meeting_point(owner, festival_id, name="", address="A").status_code == 400
    assert add_meeting_point(owner, festival_id, name="A", address="").status_code == 400


def test_a_car_from_another_festival_is_ignored_in_the_selection(festival, factory) -> None:
    """A foreign id must not confirm that the other festival exists."""
    festival_id, owner = festival
    created = owner.post("/festivals", data={"name": "Other", "csrf_token": csrf(owner)})
    other_id = int(created.headers["location"].rsplit("/", 1)[1])
    add_car(owner, other_id, None)
    with factory() as db:
        foreign = db.scalars(select(Car).where(Car.festival_id == other_id)).one().id

    add_meeting_point(owner, festival_id, car_ids=[foreign])
    with factory() as db:
        assert db.scalars(select(MeetingPoint)).one().car_ids == set()


def test_the_map_legend_names_the_drivers(festival, factory) -> None:
    festival_id, owner = festival
    who = people(factory, festival_id)
    add_car(owner, festival_id, who["Owner"].id, color="grey")
    page = owner.get(f"/festivals/{festival_id}/transport").text
    assert "legend__swatch" in page
    # Colour arrives as a class, not an inline style: the CSP forbids inline
    # styles, so a hex value in the HTML would be silently ignored.
    assert "carcolor--grey" in page


def test_the_add_car_form_is_hidden_behind_a_button(festival, factory) -> None:
    """Every 'new X' form collapses until asked for — the whole point of v0.2."""
    festival_id, owner = festival
    page = owner.get(f"/festivals/{festival_id}/transport").text
    assert "details class=\"reveal\"" in page or 'class="reveal"' in page
    assert "Add a car" in page


def test_a_date_without_a_time_means_midnight(festival, factory) -> None:
    """"We leave on Friday" is worth recording even before the hour is agreed."""
    festival_id, owner = festival
    add_car(owner, festival_id, None, departure_date="2027-08-18")
    car = only_car(factory)
    assert car.departure_at is not None
    assert car.departure_at.strftime("%Y-%m-%d %H:%M") == "2027-08-18 00:00"


def test_a_time_without_a_date_is_ignored(festival, factory) -> None:
    """A time alone means nothing, and guessing today would be a wrong fact."""
    festival_id, owner = festival
    add_car(owner, festival_id, None, departure_time="06:30")
    assert only_car(factory).departure_at is None


def test_a_meeting_point_takes_a_separate_date_and_time(festival, factory) -> None:
    festival_id, owner = festival
    response = owner.post(
        f"/festivals/{festival_id}/transport/meeting-points",
        data={
            "csrf_token": csrf(owner),
            "name": "Gate services",
            "address": "Dinkelsbuehl",
            "meet_date": "2027-08-18",
            "meet_time": "14:30",
            "car_ids": [],
        },
    )
    assert response.status_code == 303
    with factory() as db:
        point = db.scalars(select(MeetingPoint)).one()
    assert point.meet_at.strftime("%Y-%m-%d %H:%M") == "2027-08-18 14:30"


def test_a_meeting_point_without_a_time_still_saves(festival, factory) -> None:
    """The reported bug: the form refused to submit at all. It must not."""
    festival_id, owner = festival
    response = owner.post(
        f"/festivals/{festival_id}/transport/meeting-points",
        data={
            "csrf_token": csrf(owner),
            "name": "Gate services",
            "address": "Dinkelsbuehl",
            "meet_date": "",
            "meet_time": "",
            "car_ids": [],
        },
    )
    assert response.status_code == 303
    with factory() as db:
        assert db.scalars(select(MeetingPoint)).one().meet_at is None
