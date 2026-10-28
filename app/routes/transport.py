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
    points = transport.meeting_points_of(db, festival_id)
    routes = {
        car.id: [
            {"label": point.label, "lat": point.lat, "lon": point.lon, "kind": point.kind}
            for point in transport.map_points(car, festival.lat, festival.lon, points)
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
            "meeting_points": points,
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


def _parse_day(raw: str) -> dt.date | None:
    """Parse a plain <input type=date> value.

    Cars carry a DAY and no clock time: an hour agreed weeks in advance is
    fiction, and a wrong time is worse than none because people plan around
    it. Meeting points still take a time — that is the one moment that has
    to be exact.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return None


def _parse_when(date_raw: str, time_raw: str = "") -> dt.datetime | None:
    """Combine a <input type=date> and an <input type=time> into one moment.

    Two separate fields instead of one datetime-local, because that control
    keeps a half-entered value that the user cannot clear: the browser leaves
    a dash in place and then rejects the form as "invalid format". Reported
    from real use on the meeting-point form.

    A date with no time means midnight — "we leave on Friday" is a useful
    thing to record on its own. A time with no date means nothing, so it is
    ignored rather than guessed into today.
    """
    date_text = (date_raw or "").strip()
    time_text = (time_raw or "").strip()
    if not date_text:
        return None
    try:
        day = dt.date.fromisoformat(date_text)
    except ValueError:
        return None
    hour, minute = 0, 0
    if time_text:
        try:
            parsed = dt.time.fromisoformat(time_text)
            hour, minute = parsed.hour, parsed.minute
        except ValueError:
            hour, minute = 0, 0
    return dt.datetime.combine(day, dt.time(hour, minute))


def _set_departure_address(db: DbSession, request: Request, car: Car, address: str) -> None:
    """Store the address and geocode it, on save and never on page view.

    An address the geocoder cannot place is still kept — "the car park behind
    the church" is a perfectly good meeting instruction, it just gets no pin.
    """
    address = address.strip()
    car.departure_address = address or None
    if address:
        car.departure_lat, car.departure_lon = geocode(db, address, _settings_for(request))
    else:
        car.departure_lat = car.departure_lon = None
    db.commit()


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
    departure_date: str = Form(default=""),
    departure_address: str = Form(default=""),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_writable(require_membership(db, user, festival_id))
    driver = (
        _participant_of(db, festival_id, int(driver_participant_id))
        if driver_participant_id.strip()
        else None
    )
    car = transport.add_car(
        db,
        festival_id=festival_id,
        color=color,
        seats_total=seats_total,
        driver=driver,
        actor=user,
        departure_on=_parse_day(departure_date),
    )
    if departure_address.strip():
        _set_departure_address(db, request, car, departure_address)
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
    departure_date: str = Form(default=""),
    departure_address: str | None = Form(default=None),
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
            departure_on=_parse_day(departure_date),
            clear_departure_on=bool(clear_departure),
            actor=user,
        )
    except transport.SeatRefused as exc:
        return _page(request, db, user, festival_id, error=str(exc))
    if departure_address is not None:
        _set_departure_address(db, request, car, departure_address)
    notify_change(festival_id, "transport")
    return RedirectResponse(f"/festivals/{festival_id}/transport", status_code=303)


@router.post("/festivals/{festival_id}/transport/meeting-points")
async def add_meeting_point(
    request: Request,
    festival_id: int,
    name: str = Form(default=""),
    address: str = Form(default=""),
    meet_date: str = Form(default=""),
    meet_time: str = Form(default=""),
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
        meet_at=_parse_when(meet_date, meet_time),
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
