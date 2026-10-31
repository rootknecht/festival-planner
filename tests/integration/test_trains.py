"""Trains: a second way of travelling, with no seat limit.

A train is deliberately not a car with a different icon, and the tests say so
where it matters: no seat count can ever refuse a rider, but travelling twice
is still impossible, because two lists claiming the same person would make
both of them wrong.
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, reset_engine_for_tests
from app.main import create_app
from app.models.festival import Participant
from app.models.transport import Car, Train, TrainChange, TrainRider
from app.models.user import User
from app.security.csrf import CSRF_COOKIE
from app.services import geocode as geocode_service
from app.services.mail import capture_sink
from app.services.passwords import hash_password
from app.services.transport import MAX_TRAIN_CHANGES

PASSWORD = "a-long-enough-password"


@pytest.fixture(autouse=True)
def no_live_geocoding(monkeypatch):
    monkeypatch.setattr(
        geocode_service, "lookup", lambda query, settings=None: (53.5, 9.9, f"resolved: {query}")
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
    for name in ("Kai", "Sabine", "Jo"):
        owner.post(
            f"/festivals/{festival_id}/participants",
            data={"display_name": name, "csrf_token": csrf(owner)},
        )
    return festival_id, owner


def people(factory, festival_id) -> dict[str, Participant]:
    with factory() as db:
        return {
            p.display_name: p
            for p in db.scalars(
                select(Participant).where(Participant.festival_id == festival_id)
            )
        }


def add_train(client, festival_id, **fields):
    payload = {"number": "ICE 574", "departure_station": "Hamburg Hbf", "csrf_token": csrf(client)}
    payload.update(fields)
    response = client.post(f"/festivals/{festival_id}/transport/trains", data=payload)
    return response


def trains(factory, festival_id) -> list[Train]:
    with factory() as db:
        return list(db.scalars(select(Train).where(Train.festival_id == festival_id)))


def test_a_train_keeps_its_connection_details(festival, factory) -> None:
    festival_id, owner = festival
    add_train(
        owner,
        festival_id,
        departure_date="2027-07-28",
        departure_time="09:14",
        arrival_time="13:47",
    )
    train = trains(factory, festival_id)[0]
    assert train.number == "ICE 574"
    assert train.departure_station == "Hamburg Hbf"
    assert train.departure_on == dt.date(2027, 7, 28)
    assert train.departure_time == dt.time(9, 14)
    assert train.arrival_time == dt.time(13, 47)


def test_the_times_are_optional_but_an_identity_is_not(festival, factory) -> None:
    """A connection without number OR station is not information."""
    festival_id, owner = festival
    add_train(owner, festival_id, departure_time="", arrival_time="")
    assert trains(factory, festival_id)[0].departure_time is None

    refused = add_train(owner, festival_id, number="  ", departure_station="")
    assert refused.status_code == 200          # re-rendered with an error
    assert "at least a number or a departure station" in refused.text
    assert len(trains(factory, festival_id)) == 1


def test_a_station_alone_is_enough(festival, factory) -> None:
    festival_id, owner = festival
    assert add_train(owner, festival_id, number="").status_code == 303
    assert trains(factory, festival_id)[0].departure_station == "Hamburg Hbf"


def test_a_train_has_no_seat_limit(festival, factory) -> None:
    """The whole point of the type: a car refuses the sixth passenger."""
    festival_id, owner = festival
    add_train(owner, festival_id)
    train_id = trains(factory, festival_id)[0].id
    for name in ("Owner", "Kai", "Sabine", "Jo"):
        response = owner.post(
            f"/festivals/{festival_id}/transport/trains/{train_id}/riders",
            data={"participant_id": people(factory, festival_id)[name].id,
                  "csrf_token": csrf(owner)},
        )
        assert response.status_code == 303
    with factory() as db:
        riders = list(db.scalars(select(TrainRider).where(TrainRider.train_id == train_id)))
    assert len(riders) == 4


def test_nobody_travels_in_a_car_and_on_a_train_at_once(festival, factory) -> None:
    festival_id, owner = festival
    add_train(owner, festival_id)
    train_id = trains(factory, festival_id)[0].id
    kai = people(factory, festival_id)["Kai"]

    owner.post(
        f"/festivals/{festival_id}/transport/trains/{train_id}/riders",
        data={"participant_id": kai.id, "csrf_token": csrf(owner)},
    )
    # Now try to put the same person in a car.
    owner.post(
        f"/festivals/{festival_id}/transport/cars",
        data={"color": "red", "seats_total": 5, "csrf_token": csrf(owner)},
    )
    with factory() as db:
        car_id = db.scalars(select(Car)).first().id
    refused = owner.post(
        f"/festivals/{festival_id}/transport/cars/{car_id}/seats",
        data={"participant_id": kai.id, "csrf_token": csrf(owner)},
    )
    assert "already travelling by train" in refused.text


def test_somebody_on_a_train_is_no_longer_unassigned(festival, factory) -> None:
    festival_id, owner = festival
    add_train(owner, festival_id)
    train_id = trains(factory, festival_id)[0].id
    kai = people(factory, festival_id)["Kai"]
    owner.post(
        f"/festivals/{festival_id}/transport/trains/{train_id}/riders",
        data={"participant_id": kai.id, "csrf_token": csrf(owner)},
    )
    page = owner.get(f"/festivals/{festival_id}/transport").text
    not_yet = page.split("Not in a car yet")[1].split("</ul>")[0] if "Not in a car yet" in page else ""
    assert "Kai" not in not_yet


def test_putting_a_placeholder_on_a_train_notifies_nobody_and_still_works(
    festival, factory
) -> None:
    """A placeholder has no account, so there is no one to notify.

    The notification path must cope with that rather than raising on the way
    out — exactly the case where a car once needed a user and a placeholder
    had none.
    """
    festival_id, owner = festival
    add_train(owner, festival_id)
    train_id = trains(factory, festival_id)[0].id
    kai = people(factory, festival_id)["Kai"]
    assert kai.user_id is None

    response = owner.post(
        f"/festivals/{festival_id}/transport/trains/{train_id}/riders",
        data={"participant_id": kai.id, "csrf_token": csrf(owner)},
    )
    assert response.status_code == 303
    with factory() as db:
        riders = list(db.scalars(select(TrainRider).where(TrainRider.train_id == train_id)))
    assert len(riders) == 1


def test_a_rider_can_be_taken_off_again(festival, factory) -> None:
    festival_id, owner = festival
    add_train(owner, festival_id)
    train_id = trains(factory, festival_id)[0].id
    kai = people(factory, festival_id)["Kai"]
    owner.post(
        f"/festivals/{festival_id}/transport/trains/{train_id}/riders",
        data={"participant_id": kai.id, "csrf_token": csrf(owner)},
    )
    owner.post(
        f"/festivals/{festival_id}/transport/trains/{train_id}/riders/remove",
        data={"participant_id": kai.id, "csrf_token": csrf(owner)},
    )
    with factory() as db:
        assert list(db.scalars(select(TrainRider).where(TrainRider.train_id == train_id))) == []


def test_a_train_can_be_edited_and_deleted(festival, factory) -> None:
    festival_id, owner = festival
    add_train(owner, festival_id)
    train_id = trains(factory, festival_id)[0].id

    owner.post(
        f"/festivals/{festival_id}/transport/trains/{train_id}/edit",
        data={
            "number": "RE 7",
            "departure_station": "Bremen Hbf",
            "departure_date": "2027-07-29",
            "departure_time": "",
            "arrival_time": "18:05",
            "csrf_token": csrf(owner),
        },
    )
    train = trains(factory, festival_id)[0]
    assert (train.number, train.departure_station) == ("RE 7", "Bremen Hbf")
    assert train.departure_time is None
    assert train.arrival_time == dt.time(18, 5)

    owner.post(
        f"/festivals/{festival_id}/transport/trains/{train_id}/delete",
        data={"csrf_token": csrf(owner)},
    )
    assert trains(factory, festival_id) == []


def test_trains_cannot_be_touched_across_festivals(festival, factory, settings) -> None:
    festival_id, owner = festival
    add_train(owner, festival_id)
    train_id = trains(factory, festival_id)[0].id

    other = owner.post("/festivals", data={"name": "Other", "csrf_token": csrf(owner)})
    other_id = int(other.headers["location"].rsplit("/", 1)[1])
    response = owner.post(
        f"/festivals/{other_id}/transport/trains/{train_id}/delete",
        data={"csrf_token": csrf(owner)},
    )
    assert response.status_code == 404
    assert trains(factory, festival_id) != []


def test_the_page_offers_adding_a_train_and_shows_it(festival, factory) -> None:
    festival_id, owner = festival
    page = owner.get(f"/festivals/{festival_id}/transport").text
    assert "Car-sharing / Train" in page
    assert "Add a train" in page

    add_train(owner, festival_id, departure_date="2027-07-28", departure_time="09:14")
    page = owner.get(f"/festivals/{festival_id}/transport").text
    assert "ICE 574" in page
    assert "Hamburg Hbf" in page
    assert "09:14" in page


def test_a_journey_records_where_it_ends(festival, factory) -> None:
    festival_id, owner = festival
    add_train(owner, festival_id, arrival_station="Itzehoe")
    train = trains(factory, festival_id)[0]
    assert train.departure_station == "Hamburg Hbf"
    assert train.arrival_station == "Itzehoe"

    page = owner.get(f"/festivals/{festival_id}/transport").text
    assert "Itzehoe" in page


def test_changes_are_kept_in_order_with_their_details(festival, factory) -> None:
    festival_id, owner = festival
    add_train(owner, festival_id, arrival_station="Wacken")
    train_id = trains(factory, festival_id)[0].id

    for station, time, onward in (
        ("Hannover", "11:42", "ICE 596"),
        ("Itzehoe", "", ""),
    ):
        response = owner.post(
            f"/festivals/{festival_id}/transport/trains/{train_id}/changes",
            data={
                "station": station,
                "change_time": time,
                "onward_number": onward,
                "csrf_token": csrf(owner),
            },
        )
        assert response.status_code == 303

    with factory() as db:
        changes = sorted(
            db.scalars(select(TrainChange).where(TrainChange.train_id == train_id)),
            key=lambda c: c.position,
        )
    assert [c.station for c in changes] == ["Hannover", "Itzehoe"]
    assert changes[0].change_at == dt.time(11, 42)
    assert changes[0].onward_number == "ICE 596"
    # Everything but the station stays optional.
    assert changes[1].change_at is None and changes[1].onward_number == ""

    page = owner.get(f"/festivals/{festival_id}/transport").text
    assert "Hannover" in page and "11:42" in page and "ICE 596" in page


def test_a_change_without_a_station_is_refused(festival, factory) -> None:
    festival_id, owner = festival
    add_train(owner, festival_id)
    train_id = trains(factory, festival_id)[0].id
    response = owner.post(
        f"/festivals/{festival_id}/transport/trains/{train_id}/changes",
        data={"station": "   ", "csrf_token": csrf(owner)},
    )
    assert response.status_code == 200
    assert "a change needs a station" in response.text
    with factory() as db:
        assert list(db.scalars(select(TrainChange))) == []


def test_removing_a_change_closes_the_gap_in_the_order(festival, factory) -> None:
    """Otherwise the next change collides with a position still taken."""
    festival_id, owner = festival
    add_train(owner, festival_id)
    train_id = trains(factory, festival_id)[0].id
    for station in ("A-Stadt", "B-Stadt", "C-Stadt"):
        owner.post(
            f"/festivals/{festival_id}/transport/trains/{train_id}/changes",
            data={"station": station, "csrf_token": csrf(owner)},
        )
    with factory() as db:
        middle = db.scalars(
            select(TrainChange).where(TrainChange.station == "B-Stadt")
        ).first()
    owner.post(
        f"/festivals/{festival_id}/transport/changes/{middle.id}/delete",
        data={"csrf_token": csrf(owner)},
    )
    with factory() as db:
        remaining = sorted(
            db.scalars(select(TrainChange).where(TrainChange.train_id == train_id)),
            key=lambda c: c.position,
        )
    assert [(c.station, c.position) for c in remaining] == [("A-Stadt", 0), ("C-Stadt", 1)]


def test_a_change_cannot_be_deleted_from_another_festival(festival, factory) -> None:
    festival_id, owner = festival
    add_train(owner, festival_id)
    train_id = trains(factory, festival_id)[0].id
    owner.post(
        f"/festivals/{festival_id}/transport/trains/{train_id}/changes",
        data={"station": "Hannover", "csrf_token": csrf(owner)},
    )
    with factory() as db:
        change_id = db.scalars(select(TrainChange)).first().id

    other = owner.post("/festivals", data={"name": "Other", "csrf_token": csrf(owner)})
    other_id = int(other.headers["location"].rsplit("/", 1)[1])
    response = owner.post(
        f"/festivals/{other_id}/transport/changes/{change_id}/delete",
        data={"csrf_token": csrf(owner)},
    )
    assert response.status_code == 404
    with factory() as db:
        assert db.scalars(select(TrainChange)).first() is not None


def test_deleting_the_train_takes_its_changes_with_it(festival, factory) -> None:
    festival_id, owner = festival
    add_train(owner, festival_id)
    train_id = trains(factory, festival_id)[0].id
    owner.post(
        f"/festivals/{festival_id}/transport/trains/{train_id}/changes",
        data={"station": "Hannover", "csrf_token": csrf(owner)},
    )
    owner.post(
        f"/festivals/{festival_id}/transport/trains/{train_id}/delete",
        data={"csrf_token": csrf(owner)},
    )
    with factory() as db:
        assert list(db.scalars(select(TrainChange))) == []


def test_a_journey_cannot_collect_unlimited_changes(festival, factory) -> None:
    festival_id, owner = festival
    add_train(owner, festival_id)
    train_id = trains(factory, festival_id)[0].id
    for index in range(MAX_TRAIN_CHANGES):
        owner.post(
            f"/festivals/{festival_id}/transport/trains/{train_id}/changes",
            data={"station": f"Station {index}", "csrf_token": csrf(owner)},
        )
    refused = owner.post(
        f"/festivals/{festival_id}/transport/trains/{train_id}/changes",
        data={"station": "One too many", "csrf_token": csrf(owner)},
    )
    assert refused.status_code == 200
    assert "enough changes" in refused.text
    with factory() as db:
        assert len(list(db.scalars(select(TrainChange)))) == MAX_TRAIN_CHANGES


def test_the_card_draws_the_train_at_vehicle_size_not_icon_size(festival, factory) -> None:
    """A 22px icon beside a 98px car bar made the train look like a detail.

    The wagon therefore uses its own wide viewBox and width class rather than
    the toolbar `.icon`, and is wider than a five-seat car on purpose.
    """
    festival_id, owner = festival
    add_train(owner, festival_id)
    page = owner.get(f"/festivals/{festival_id}/transport").text
    card = page.split("<strong>")[0]
    assert 'class="trainbar"' in card
    assert 'viewBox="0 0 56 23"' in card
    assert 'class="icon"' not in card.split('class="trainbar"')[1][:200]


def test_an_archived_festival_freezes_trains(festival, factory) -> None:
    festival_id, owner = festival
    add_train(owner, festival_id)
    owner.post(f"/festivals/{festival_id}/archive", data={"csrf_token": csrf(owner)})
    refused = add_train(owner, festival_id, number="RE 7")
    assert refused.status_code == 409
    assert len(trains(factory, festival_id)) == 1
