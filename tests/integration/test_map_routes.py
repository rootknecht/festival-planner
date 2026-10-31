"""Road routes for cars, station chains for trains.

The routing service is NEVER called for real here: a test suite that depends
on a third-party API fails on a train, and a daily quota is not a thing to
spend on CI. Every test below stubs the HTTP call and asserts on what the app
does with the answer — including when the answer is "no".
"""

import datetime as dt
import json
from pathlib import Path

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
from app.services import routing, transport
from app.services.passwords import hash_password

PASSWORD = "a-long-enough-password"

#: A plausible answer from openrouteservice: GeoJSON, lon/lat order.
GEOMETRY = [[9.9, 53.5], [9.7, 53.8], [9.4, 54.0]]


@pytest.fixture(autouse=True)
def no_live_geocoding(monkeypatch):
    """Stations resolve to fixed coordinates; nothing leaves the machine."""
    places = {
        # A main station is found either way; a tram stop ONLY under its own
        # name; a town only without the word. All three shapes occur in
        # production, which is why the lookup tries the bare name first.
        "Hamburg Hbf": (53.5528, 10.0067),
        "Hamburg Hbf station": (53.5528, 10.0067),
        "Venloer Str./ Gürtel": (50.9479, 6.9123),
        "Itzehoe station": (53.9239, 9.5156),
        "Wacken": (54.0186, 9.3761),
    }

    def fake_lookup(query, settings=None):
        if query not in places:
            # Nominatim's real behaviour for a tram stop with " station"
            # appended, and for anything else it cannot place: nothing.
            return None
        lat, lon = places[query]
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


def test_the_request_asks_the_router_to_look_further_than_350m(factory, monkeypatch) -> None:
    """A festival site is a field. Without this, every line through it 404s."""
    sent = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"features": [{"geometry": {"coordinates": GEOMETRY}}]}

    def fake_post(url, **kwargs):
        sent.update(kwargs["json"])
        return Response()

    monkeypatch.setattr(routing.httpx, "post", fake_post)
    settings = settings_with(ors_api_key="k")
    with factory() as db:
        routing.road_route(db, [(53.5, 9.9), (54.0, 9.4), (51.7, 11.2)], settings)

    # One radius per waypoint, which is the shape the API demands.
    assert sent["radiuses"] == [settings.ors_snap_radius] * 3
    assert settings.ors_snap_radius >= 1000


def test_raising_the_radius_does_not_keep_serving_the_old_refusal(
    factory, monkeypatch
) -> None:
    """The radius is part of the cache key, or the fix appears to do nothing."""
    calls = []

    def refuse(url, **kwargs):
        calls.append(1)
        request = routing.httpx.Request("POST", url)
        response = routing.httpx.Response(
            404,
            json={"error": {"code": 2010, "message": "Could not find routable point"}},
            request=request,
        )
        raise routing.httpx.HTTPStatusError("404", request=request, response=response)

    monkeypatch.setattr(routing.httpx, "post", refuse)
    coords = [(53.5, 9.9), (51.7433843, 11.2284084)]

    with factory() as db:
        assert routing.road_route(db, coords, settings_with(ors_api_key="k")) is None
        # Same radius: the refusal is remembered, no second request.
        assert routing.road_route(db, coords, settings_with(ors_api_key="k")) is None
        assert len(calls) == 1
        # A bigger radius is a DIFFERENT question, so it is asked.
        routing.road_route(
            db, coords, settings_with(ors_api_key="k", ors_snap_radius=10000.0)
        )
    assert len(calls) == 2


def test_the_api_s_own_reason_reaches_the_log(factory, monkeypatch, caplog) -> None:
    """A bare '404 Not Found' sent one debugging round the wrong way."""

    def refuse(url, **kwargs):
        request = routing.httpx.Request("POST", url)
        response = routing.httpx.Response(
            404,
            json={
                "error": {
                    "code": 2010,
                    "message": "Could not find routable point within a radius of 350.0 meters",
                }
            },
            request=request,
        )
        raise routing.httpx.HTTPStatusError("404", request=request, response=response)

    monkeypatch.setattr(routing.httpx, "post", refuse)
    with caplog.at_level("WARNING"):
        assert routing.fetch([(53.5, 9.9), (51.7, 11.2)], settings_with(ors_api_key="k")) is None
    assert "2010" in caplog.text
    assert "routable point" in caplog.text


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


def test_the_legend_names_the_train_line(factory) -> None:
    client = client_for(settings_with())
    festival_id = make_festival(client, factory)
    before = client.get(f"/festivals/{festival_id}/transport").text
    assert "legend__swatch--train" not in before, "no trains, no train legend row"

    add_train(client, festival_id)
    page = client.get(f"/festivals/{festival_id}/transport").text
    legend = page.split('<ul class="legend">')[1].split("</ul>")[0]
    assert "legend__swatch--train" in legend
    assert ">Train<" in legend or "Train" in legend


def test_one_legend_row_however_many_trains(factory) -> None:
    """They all share one colour, so repeating the row says nothing."""
    client = client_for(settings_with())
    festival_id = make_festival(client, factory)
    add_train(client, festival_id)
    add_train(client, festival_id, number="RE 7")
    page = client.get(f"/festivals/{festival_id}/transport").text
    assert page.count("legend__swatch--train") == 1


def test_the_legend_swatch_matches_the_colour_the_map_draws(factory) -> None:
    """A swatch in a different colour than its line is worse than no legend."""
    css = Path("app/static/css/shell.css").read_text(encoding="utf-8")
    assert f".legend__swatch--train {{ background: {transport.TRAIN_LINE_COLOR}; }}" in css

    client = client_for(settings_with())
    festival_id = make_festival(client, factory)
    add_train(client, festival_id)
    page = client.get(f"/festivals/{festival_id}/transport").text
    # And the browser is told the same value, rather than map.js hardcoding it.
    assert f'data-train-color="{transport.TRAIN_LINE_COLOR}"' in page


def test_the_map_script_lays_overlapping_lines_side_by_side(factory) -> None:
    """Two cars on the same road hid each other; the lanes are the fix.

    This asserts the mechanism exists and is driven by the data, since the
    actual pixels can only be judged by looking at a rendered map.
    """
    script = Path("app/static/js/map.js").read_text(encoding="utf-8")
    assert "laneOffset" in script and "SPACING_PX" in script
    # Recomputed after a zoom, or the lanes drift apart as the scale changes.
    assert 'map.on("zoomend"' in script
    # A single line must stay on its true route.
    assert "if (lanes.length < 2) return 0;" in script
    # Road geometry has points ~0.2px apart; offsetting each one by its own
    # normal produced a sawtooth that read as a line twice as thick as its
    # neighbour. Simplify first, or the fix becomes the next bug report.
    assert "L.LineUtil.simplify(points, 1)" in script


def test_a_tram_stop_is_placed_under_its_own_name(factory) -> None:
    """Appending " station" to a tram stop finds nothing.

    This is why no line appeared for a Köln U-Bahn journey: the only query
    ever sent was the one that cannot resolve.
    """
    client = client_for(settings_with())
    festival_id = make_festival(client, factory)
    add_train(
        client,
        festival_id,
        number="U 13",
        departure_station="Venloer Str./ Gürtel",
        arrival_station="Wacken",
    )
    with factory() as db:
        train = db.scalars(select(Train)).first()
        assert (train.departure_lat, train.departure_lon) == (50.9479, 6.9123)
        assert (train.arrival_lat, train.arrival_lon) == (54.0186, 9.3761)


def test_a_station_that_resolves_only_with_the_word_is_still_placed(factory) -> None:
    client = client_for(settings_with())
    festival_id = make_festival(client, factory)
    add_train(client, festival_id, departure_station="Itzehoe", arrival_station="Wacken")
    with factory() as db:
        train = db.scalars(select(Train)).first()
        # Found on the second attempt, with " station" appended.
        assert (train.departure_lat, train.departure_lon) == (53.9239, 9.5156)


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
