"""Transportation routes: cars, seats, departure addresses and stops."""

from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session as DbSession

from app.deps import get_db, require_user
from app.models.festival import Participant
from app.models.notification import Tool
from app.models.transport import Car, CarStop, MeetingPoint
from app.models.user import User
from app.realtime.hub import notify_change
from app.services import notifications, transport
from app.services.access import require_membership, require_writable
from app.services.geocode import geocode
from app.templating import language_for, render
from app.translations import gettext_for

router = APIRouter(tags=["transport"])


def _settings_for(request: Request):
    from app.config import get_settings

    return getattr(request.app.state, "settings", None) or get_settings()


def _page(request: Request, db: DbSession, user: User, festival_id: int, **extra) -> Response:
    membership = require_membership(db, user, festival_id)
    festival = membership.festival
    cars = transport.cars_of(db, festival_id)
    routes = {
        car.id: [
            {"label": point.label, "lat": point.lat, "lon": point.lon, "kind": point.kind}
            for point in transport.map_points(car, festival.lat, festival.lon)
        ]
        for car in cars
    }
    return render(
        request,
        "tools/transport.html",
        {
            "festival": festival,
            "membership": membership,
            "badges": notifications.badges(db, user.id, festival_id),
            "active_tool": "transport",
            "cars": cars,
            "unassigned": transport.unassigned(db, festival_id),
            "colors": transport.color_labels(gettext_for(language_for(request))),
            "color_hex": transport.COLOR_HEX,
            "meeting_points": transport.meeting_points_of(db, festival_id),
            "max_meeting_points": transport.MAX_MEETING_POINTS,
            "routes_json": json.dumps(routes),
            "legend_json": json.dumps(
                [
                    {
                        "car_id": car.id,
                        "driver": car.driver.display_name if car.driver else None,
                        "hex": transport.COLOR_HEX.get(car.color, "#888888"),
                    }
                    for car in cars
                ]
            ),
            **extra,
        },
    )


def _parse_when(raw: str) -> dt.datetime | None:
    """Parse an <input type=datetime-local> value.

    Browsers send "2027-08-18T06:30" (no seconds, no zone). An unparseable or
    empty value means "not set" rather than an error: a car without a departure
    time is a perfectly normal state, and rejecting the whole form over a
    half-typed date would lose the rest of the edit.
    """
    raw = raw.strip()
    if not raw:
        return None
    for shape in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(raw, shape)
        except ValueError:
            continue
    return None


def _meeting_point_of(db: DbSession, festival_id: int, point_id: int) -> MeetingPoint:
    point = db.get(MeetingPoint, point_id)
    if point is None or point.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    return point


def _car_of(db: DbSession, festival_id: int, car_id: int) -> Car:
    car = db.get(Car, car_id)
    if car is None or car.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    return car


def _participant_of(db: DbSession, festival_id: int, participant_id: int) -> Participant:
    participant = db.get(Participant, participant_id)
    if participant is None or participant.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    return participant


@router.get("/festivals/{festival_id}/transport")
async def overview(
    request: Request,
    festival_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_membership(db, user, festival_id)
    notifications.clear_tool(db, user.id, festival_id, Tool.TRANSPORT)
    return _page(request, db, user, festival_id, error=None)


@router.post("/festivals/{festival_id}/transport/cars")
async def add_car(
    request: Request,
    festival_id: int,
    color: str = Form(default="red"),
    seats_total: int = Form(default=5),
    driver_participant_id: str = Form(default=""),
    departure_at: str = Form(default=""),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_writable(require_membership(db, user, festival_id))
    driver = (
        _participant_of(db, festival_id, int(driver_participant_id))
        if driver_participant_id.strip()
        else None
    )
    transport.add_car(
        db,
        festival_id=festival_id,
        color=color,
        seats_total=seats_total,
        driver=driver,
        actor=user,
        departure_at=_parse_when(departure_at),
    )
    notify_change(festival_id, "transport")
    return RedirectResponse(f"/festivals/{festival_id}/transport", status_code=303)


@router.post("/festivals/{festival_id}/transport/cars/{car_id}/delete")
async def delete_car(
    request: Request,
    festival_id: int,
    car_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_writable(require_membership(db, user, festival_id))
    transport.delete_car(db, _car_of(db, festival_id, car_id), user)
    notify_change(festival_id, "transport")
    return RedirectResponse(f"/festivals/{festival_id}/transport", status_code=303)


@router.post("/festivals/{festival_id}/transport/cars/{car_id}/seats")
async def assign_seat(
    request: Request,
    festival_id: int,
    car_id: int,
    participant_id: int = Form(...),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_writable(require_membership(db, user, festival_id))
    transport.assign_seat(
        db,
        car=_car_of(db, festival_id, car_id),
        participant=_participant_of(db, festival_id, participant_id),
        actor=user,
    )
    notify_change(festival_id, "transport")
    return RedirectResponse(f"/festivals/{festival_id}/transport", status_code=303)


@router.post("/festivals/{festival_id}/transport/cars/{car_id}/seats/remove")
async def remove_seat(
    request: Request,
    festival_id: int,
    car_id: int,
    participant_id: int = Form(...),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_writable(require_membership(db, user, festival_id))
    transport.remove_seat(
        db,
        car=_car_of(db, festival_id, car_id),
        participant=_participant_of(db, festival_id, participant_id),
        actor=user,
    )
    notify_change(festival_id, "transport")
    return RedirectResponse(f"/festivals/{festival_id}/transport", status_code=303)


@router.post("/festivals/{festival_id}/transport/cars/{car_id}/departure")
async def set_departure(
    request: Request,
    festival_id: int,
    car_id: int,
    address: str = Form(default=""),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_writable(require_membership(db, user, festival_id))
    car = _car_of(db, festival_id, car_id)
    address = address.strip()[:500]
    car.departure_address = address or None
    # Geocoding happens here, on an explicit save — never on page render.
    car.departure_lat, car.departure_lon = (
        geocode(db, address, _settings_for(request)) if address else (None, None)
    )
    db.commit()
    notify_change(festival_id, "transport")
    return RedirectResponse(f"/festivals/{festival_id}/transport", status_code=303)


@router.post("/festivals/{festival_id}/transport/cars/{car_id}/stops")
async def add_stop(
    request: Request,
    festival_id: int,
    car_id: int,
    address: str = Form(...),
    participant_id: str = Form(default=""),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_writable(require_membership(db, user, festival_id))
    car = _car_of(db, festival_id, car_id)
    if not address.strip():
        return _page(request, db, user, festival_id, error="A stop needs an address.")
    lat, lon = geocode(db, address, _settings_for(request))
    transport.add_stop(
        db,
        car=car,
        address=address,
        lat=lat,
        lon=lon,
        participant=(
            _participant_of(db, festival_id, int(participant_id))
            if participant_id.strip()
            else None
        ),
        actor=user,
    )
    notify_change(festival_id, "transport")
    return RedirectResponse(f"/festivals/{festival_id}/transport", status_code=303)


@router.post("/festivals/{festival_id}/transport/stops/{stop_id}/delete")
async def delete_stop(
    request: Request,
    festival_id: int,
    stop_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_writable(require_membership(db, user, festival_id))
    stop = db.get(CarStop, stop_id)
    if stop is None or stop.car.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    transport.delete_stop(db, stop=stop, actor=user, festival_id=festival_id)
    notify_change(festival_id, "transport")
    return RedirectResponse(f"/festivals/{festival_id}/transport", status_code=303)


@router.post("/festivals/{festival_id}/transport/cars/{car_id}/edit")
async def edit_car(
    request: Request,
    festival_id: int,
    car_id: int,
    color: str = Form(default=""),
    seats_total: int = Form(default=0),
    departure_at: str = Form(default=""),
    clear_departure: str = Form(default=""),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    """Change an existing car. Anyone in the festival may; cars are shared."""
    require_writable(require_membership(db, user, festival_id))
    car = _car_of(db, festival_id, car_id)
    try:
        transport.edit_car(
            db,
            car=car,
            color=color or None,
            seats_total=seats_total or None,
            departure_at=_parse_when(departure_at),
            clear_departure_at=bool(clear_departure),
            actor=user,
        )
    except transport.SeatRefused as exc:
        return _page(request, db, user, festival_id, error=str(exc))
    notify_change(festival_id, "transport")
    return RedirectResponse(f"/festivals/{festival_id}/transport", status_code=303)


@router.post("/festivals/{festival_id}/transport/meeting-points")
async def add_meeting_point(
    request: Request,
    festival_id: int,
    name: str = Form(default=""),
    address: str = Form(default=""),
    meet_at: str = Form(default=""),
    car_ids: list[int] = Form(default=[]),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    """Admin-only: a meeting point is a decision for the group, not per person."""
    membership = require_writable(require_membership(db, user, festival_id))
    if not membership.is_admin:
        raise HTTPException(status_code=403, detail="admins only")
    point = transport.add_meeting_point(
        db,
        festival_id=festival_id,
        name=name,
        address=address,
        meet_at=_parse_when(meet_at),
        car_ids=car_ids,
        actor=user,
    )
    # Geocode after saving, never before: a point the geocoder cannot find is
    # still a valid point ("car park behind the Aral station"), it just has no
    # marker on the map.
    point.lat, point.lon = geocode(db, point.address, _settings_for(request))
    db.commit()
    notify_change(festival_id, "transport")
    return RedirectResponse(f"/festivals/{festival_id}/transport", status_code=303)


@router.post("/festivals/{festival_id}/transport/meeting-points/{point_id}/cars")
async def set_meeting_point_cars(
    request: Request,
    festival_id: int,
    point_id: int,
    car_ids: list[int] = Form(default=[]),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_writable(require_membership(db, user, festival_id))
    if not membership.is_admin:
        raise HTTPException(status_code=403, detail="admins only")
    point = _meeting_point_of(db, festival_id, point_id)
    transport.set_meeting_point_cars(db, point=point, car_ids=car_ids, actor=user)
    notify_change(festival_id, "transport")
    return RedirectResponse(f"/festivals/{festival_id}/transport", status_code=303)


@router.post("/festivals/{festival_id}/transport/meeting-points/{point_id}/delete")
async def delete_meeting_point(
    request: Request,
    festival_id: int,
    point_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_writable(require_membership(db, user, festival_id))
    if not membership.is_admin:
        raise HTTPException(status_code=403, detail="admins only")
    transport.delete_meeting_point(
        db, point=_meeting_point_of(db, festival_id, point_id), actor=user
    )
    notify_change(festival_id, "transport")
    return RedirectResponse(f"/festivals/{festival_id}/transport", status_code=303)
