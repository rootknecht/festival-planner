"""Road routes for cars, station chains for trains.

The routing service is NEVER called for real here: a test suite that depends
on a third-party API fails on a train, and a daily quota is not a thing to
spend on CI. Every test below stubs the HTTP call and asserts on what the app
does with the answer — including when the answer is "no".
"""

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, reset_engine_for_tests
from app.main import create_app
from app.models.festival import Festival
from app.models.transport import Car, RouteCache, Train
from app.models.user import User
from app.security.csrf import CSRF_COOKIE
from app.services import geocode as geocode_service
from app.services import routing
from app.services.passwords import hash_password

PASSWORD = "a-long-enough-password"

#: A plausible answer from openrouteservice: GeoJSON, lon/lat order.
GEOMETRY = [[9.9, 53.5], [9.7, 53.8], [9.4, 54.0]]


@pytest.fixture(autouse=True)
def no_live_geocoding(monkeypatch):
    """Stations resolve to fixed coordinates; nothing leaves the machine."""
    places = {
        "Hamburg Hbf station": (53.5528, 10.0067),
        "Itzehoe station": (53.9239, 9.5156),
        "Wacken station": (54.0186, 9.3761),
    }

    def fake_lookup(query, settings=None):
        lat, lon = places.get(query, (53.0, 9.0))
        return lat, lon, f"resolved {query}"

    monkeypatch.setattr(geocode_service, "lookup", fake_lookup)


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
    return session_factory


def settings_with(**extra) -> Settings:
    return Settings(_env_file=None, environment="test", base_url="https://example.org", **extra)


def client_for(settings: Settings) -> TestClient:
    client = TestClient(create_app(settings), follow_redirects=False)
    client.get("/login")
    client.post(
        "/login",
        data={
            "email": "owner@example.org",
            "password": PASSWORD,
            "csrf_token": client.cookies[CSRF_COOKIE],
        },
    )
    return client


def csrf(client: TestClient) -> str:
    return client.cookies[CSRF_COOKIE]


def make_festival(client, factory=None, placed: bool = True) -> int:
    """A festival, with coordinates unless a test wants it unplaced.

    A festival is geocoded from its SETTINGS page, not at creation, so the
    coordinates are set directly here: the thing under test is the map
    payload, not the settings form.
    """
    created = client.post(
        "/festivals", data={"name": "Wacken", "csrf_token": csrf(client)}
    )
    festival_id = int(created.headers["location"].rsplit("/", 1)[1])
    if placed and factory is not None:
        with factory() as db:
            festival = db.get(Festival, festival_id)
            festival.lat, festival.lon = 54.0186, 9.3761
            db.commit()
    return festival_id


def add_car_with_route(client, festival_id, factory) -> None:
    created = client.post(
        f"/festivals/{festival_id}/transport/cars",
        data={"color": "red", "seats_total": 4, "csrf_token": csrf(client)},
    )
    assert created.status_code == 303
    with factory() as db:
        car_id = db.scalars(select(Car)).first().id
    # Every status code asserted: a typo in a URL returns 404 and a test that
    # ignores it "passes" while exercising nothing.
    placed = client.post(
        f"/festivals/{festival_id}/transport/cars/{car_id}/departure",
        data={"address": "Hamburg Hbf", "csrf_token": csrf(client)},
    )
    assert placed.status_code == 303


def add_train(client, festival_id, **extra) -> None:
    data = {
        "number": "ICE 574",
        "departure_station": "Hamburg Hbf",
        "arrival_station": "Wacken",
        "csrf_token": csrf(client),
    }
    data.update(extra)
    client.post(f"/festivals/{festival_id}/transport/trains", data=data)


def map_node(page: str, attribute: str) -> dict:
    """Pull one data-attribute of the map div back out of the HTML."""
    fragment = page.split(f'{attribute}="')[1].split('"')[0]
    return json.loads(fragment.replace("&#34;", '"').replace("&quot;", '"'))


# --- the service itself ------------------------------------------------------


def test_no_key_means_no_request_and_no_cache_row(factory, monkeypatch) -> None:
    """The feature is off by default and must not phone anybody."""
    called = []
    monkeypatch.setattr(routing.httpx, "post", lambda *a, **k: called.append(a))
    with factory() as db:
        assert routing.road_route(db, [(53.5, 9.9), (54.0, 9.4)], settings_with()) is None
        assert list(db.scalars(select(RouteCache))) == []
    assert called == []


def test_a_geometry_is_fetched_once_and_then_served_from_cache(factory, monkeypatch) -> None:
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"features": [{"geometry": {"coordinates": GEOMETRY}}]}

    def fake_post(url, **kwargs):
        calls.append(kwargs["json"]["coordinates"])
        return Response()

    monkeypatch.setattr(routing.httpx, "post", fake_post)
    settings = settings_with(ors_api_key="test-key")
    coords = [(53.5, 9.9), (54.0, 9.4)]

    with factory() as db:
        first = routing.road_route(db, coords, settings)
        second = routing.road_route(db, coords, settings)

    # lat/lon came back the right way round...
    assert first == [[53.5, 9.9], [53.8, 9.7], [54.0, 9.4]]
    assert second == first
    # ...and the second render spent nothing.
    assert len(calls) == 1
    # The request itself was sent lon,lat — the API's order, not ours.
    assert calls[0] == [[9.9, 53.5], [9.4, 54.0]]


def test_editing_a_stop_earns_a_new_lookup(factory, monkeypatch) -> None:
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"features": [{"geometry": {"coordinates": GEOMETRY}}]}

    monkeypatch.setattr(
        routing.httpx, "post", lambda url, **kw: (calls.append(1), Response())[1]
    )
    settings = settings_with(ors_api_key="test-key")
    with factory() as db:
        routing.road_route(db, [(53.5, 9.9), (54.0, 9.4)], settings)
        routing.road_route(db, [(53.5, 9.9), (54.0, 9.5)], settings)
    assert len(calls) == 2


def test_a_failure_is_remembered_so_it_is_not_retried_every_page_view(
    factory, monkeypatch
) -> None:
    calls = []

    def fake_post(url, **kwargs):
        calls.append(1)
        raise routing.httpx.ConnectTimeout("nope")

    monkeypatch.setattr(routing.httpx, "post", fake_post)
    settings = settings_with(ors_api_key="test-key")
    coords = [(53.5, 9.9), (54.0, 9.4)]

    with factory() as db:
        assert routing.road_route(db, coords, settings) is None
        assert routing.road_route(db, coords, settings) is None
        row = db.scalars(select(RouteCache)).first()
        assert row is not None and row.geometry == ""
    assert len(calls) == 1

    # ...but it IS retried once the failure has aged out.
    with factory() as db:
        row = db.scalars(select(RouteCache)).first()
        row.fetched_at = row.fetched_at - routing.FAILURE_RETRY - dt.timedelta(minutes=1)
        db.commit()
        routing.road_route(db, coords, settings)
    assert len(calls) == 2


def test_a_nonsense_payload_is_not_drawn(factory, monkeypatch) -> None:
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"features": []}

    monkeypatch.setattr(routing.httpx, "post", lambda url, **kw: Response())
    with factory() as db:
        assert (
            routing.road_route(
                db, [(53.5, 9.9), (54.0, 9.4)], settings_with(ors_api_key="k")
            )
            is None
        )


def test_too_many_waypoints_is_refused_rather_than_charged(factory, monkeypatch) -> None:
    monkeypatch.setattr(
        routing.httpx, "post", lambda *a, **k: pytest.fail("should not be called")
    )
    coords = [(50.0 + i / 100, 9.0) for i in range(routing.MAX_WAYPOINTS + 1)]
    with factory() as db:
        assert routing.road_route(db, coords, settings_with(ors_api_key="k")) is None


# --- what the page ships -----------------------------------------------------


def test_without_a_key_the_map_still_gets_its_straight_line_points(factory) -> None:
    client = client_for(settings_with())
    festival_id = make_festival(client, factory)
    add_car_with_route(client, festival_id, factory)

    page = client.get(f"/festivals/{festival_id}/transport").text
    assert map_node(page, "data-routes") != {}
    # No road geometry offered, so the browser draws the dashed sketch.
    assert map_node(page, "data-roads") == {}


def test_with_a_key_the_car_line_carries_road_geometry(factory, monkeypatch) -> None:
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"features": [{"geometry": {"coordinates": GEOMETRY}}]}

    monkeypatch.setattr(routing.httpx, "post", lambda url, **kw: Response())
    client = client_for(settings_with(ors_api_key="test-key"))
    festival_id = make_festival(client, factory)
    add_car_with_route(client, festival_id, factory)

    roads = map_node(client.get(f"/festivals/{festival_id}/transport").text, "data-roads")
    assert roads, "a routed car should ship geometry"
    assert roads[next(iter(roads))][0] == [53.5, 9.9]


def test_a_train_journey_is_drawn_from_its_geocoded_stations(factory) -> None:
    client = client_for(settings_with())
    festival_id = make_festival(client, factory)
    add_train(client, festival_id)
    train_id = None
    with factory() as db:
        train = db.scalars(select(Train)).first()
        train_id = train.id
        # Stations were geocoded on save, not on render.
        assert (train.departure_lat, train.departure_lon) == (53.5528, 10.0067)
        assert (train.arrival_lat, train.arrival_lon) == (54.0186, 9.3761)

    trains = map_node(client.get(f"/festivals/{festival_id}/transport").text, "data-trains")
    points = trains[str(train_id)]
    assert [p["kind"] for p in points] == ["start", "arrival"]
    assert points[0]["label"] == "Hamburg Hbf"


def test_a_change_appears_between_the_two_stations_in_order(factory) -> None:
    client = client_for(settings_with())
    festival_id = make_festival(client, factory)
    add_train(client, festival_id)
    with factory() as db:
        train_id = db.scalars(select(Train)).first().id
    client.post(
        f"/festivals/{festival_id}/transport/trains/{train_id}/changes",
        data={"station": "Itzehoe", "onward_number": "RB 61", "csrf_token": csrf(client)},
    )

    trains = map_node(client.get(f"/festivals/{festival_id}/transport").text, "data-trains")
    points = trains[str(train_id)]
    assert [p["kind"] for p in points] == ["start", "change", "arrival"]
    assert points[1]["label"] == "Itzehoe → RB 61"
    assert (points[1]["lat"], points[1]["lon"]) == (53.9239, 9.5156)


def test_a_journey_with_one_placeable_station_is_not_drawn_as_a_line(factory) -> None:
    """A single dot pretending to be a route is worse than no route."""
    client = client_for(settings_with())
    # No festival coordinates either, so there is genuinely one point only.
    festival_id = make_festival(client, factory, placed=False)
    client.post(
        f"/festivals/{festival_id}/transport/trains",
        data={"number": "ICE 1", "departure_station": "Hamburg Hbf", "csrf_token": csrf(client)},
    )
    with factory() as db:
        train = db.scalars(select(Train)).first()
        assert train.departure_lat is not None
        assert train.arrival_lat is None
    page = client.get(f"/festivals/{festival_id}/transport").text
    assert map_node(page, "data-trains") == {}


def test_renaming_a_station_moves_its_pin(factory) -> None:
    client = client_for(settings_with())
    festival_id = make_festival(client, factory)
    add_train(client, festival_id)
    with factory() as db:
        train_id = db.scalars(select(Train)).first().id

    client.post(
        f"/festivals/{festival_id}/transport/trains/{train_id}/edit",
        data={
            "number": "ICE 574",
            "departure_station": "Itzehoe",
            "arrival_station": "Wacken",
            "csrf_token": csrf(client),
        },
    )
    with factory() as db:
        train = db.get(Train, train_id)
        assert (train.departure_lat, train.departure_lon) == (53.9239, 9.5156)
