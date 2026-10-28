"""Car sharing: cars, seats and stops.

The central rule is that one person sits in exactly one car. Enforcing it in
one place here means the drag-and-drop UI, the no-JavaScript fallback and any
future client all inherit it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models.festival import Participant
from app.models.notification import NotificationKind, Tool
from app.models.transport import (
    CAR_COLORS,
    MAX_SEATS,
    MIN_SEATS,
    Car,
    CarSeat,
    CarStop,
    MeetingPoint,
    MeetingPointCar,
)
from app.models.user import User
from app.services.bringlist import log
from app.services.notifications import notify_participant
from app.translations import N_


class SeatRefused(HTTPException):
    def __init__(self, detail: str) -> None:
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)


@dataclass
class MapPoint:
    label: str
    lat: float
    lon: float
    kind: str


def cars_of(db: DbSession, festival_id: int) -> list[Car]:
    return list(
        db.scalars(
            select(Car).where(Car.festival_id == festival_id).order_by(Car.id)
        )
    )


def seated_participant_ids(db: DbSession, festival_id: int) -> set[int]:
    drivers = set(
        db.scalars(
            select(Car.driver_participant_id).where(
                Car.festival_id == festival_id, Car.driver_participant_id.is_not(None)
            )
        )
    )
    passengers = set(
        db.scalars(
            select(CarSeat.participant_id)
            .join(Car, Car.id == CarSeat.car_id)
            .where(Car.festival_id == festival_id)
        )
    )
    return drivers | passengers


def unassigned(db: DbSession, festival_id: int) -> list[Participant]:
    taken = seated_participant_ids(db, festival_id)
    return [
        participant
        for participant in db.scalars(
            select(Participant)
            .where(Participant.festival_id == festival_id)
            .order_by(func.lower(Participant.display_name))
        )
        if participant.id not in taken
    ]


def add_car(
    db: DbSession,
    *,
    festival_id: int,
    color: str,
    seats_total: int,
    driver: Participant | None,
    actor: User,
    departure_on: dt.date | None = None,
) -> Car:
    if color not in CAR_COLORS:
        color = CAR_COLORS[0]
    seats_total = max(MIN_SEATS, min(MAX_SEATS, int(seats_total)))

    if driver is not None and driver.id in seated_participant_ids(db, festival_id):
        raise SeatRefused("that person is already travelling in another car")

    car = Car(
        festival_id=festival_id,
        color=color,
        seats_total=seats_total,
        driver_participant_id=driver.id if driver else None,
        departure_on=departure_on,
        created_by=actor.id,
    )
    db.add(car)
    db.flush()
    log(db, festival_id, "car", car.id, actor, f"added {car.label}")
    db.commit()
    return car


def edit_car(
    db: DbSession,
    *,
    car: Car,
    color: str | None = None,
    seats_total: int | None = None,
    departure_on: dt.date | None = None,
    clear_departure_on: bool = False,
    actor: User,
) -> Car:
    """Change a car after the fact. Shrinking below the people in it is refused.

    A silent kick-out would be the alternative, and losing your seat without
    being told is worse than the edit failing.
    """
    changes: list[str] = []
    if color is not None and color in CAR_COLORS and color != car.color:
        changes.append(f"colour {car.color} -> {color}")
        car.color = color
    if seats_total is not None:
        wanted = max(MIN_SEATS, min(MAX_SEATS, int(seats_total)))
        if wanted != car.seats_total:
            if wanted < car.occupied:
                raise SeatRefused(
                    "that would leave fewer seats than there are people in the car"
                )
            changes.append(f"seats {car.seats_total} -> {wanted}")
            car.seats_total = wanted
    if clear_departure_on:
        if car.departure_on is not None:
            changes.append("departure cleared")
            car.departure_on = None
    elif departure_on is not None and departure_on != car.departure_on:
        changes.append(f"departure {departure_on.isoformat()}")
        car.departure_on = departure_on
    if changes:
        log(db, car.festival_id, "car", car.id, actor, "; ".join(changes))
    db.commit()
    return car


def delete_car(db: DbSession, car: Car, actor: User) -> None:
    log(db, car.festival_id, "car", car.id, actor, f"removed {car.label}")
    db.delete(car)
    db.commit()


def assign_seat(db: DbSession, *, car: Car, participant: Participant, actor: User) -> CarSeat:
    if participant.festival_id != car.festival_id:
        raise HTTPException(status_code=404, detail="not found")
    if participant.id in seated_participant_ids(db, car.festival_id):
        raise SeatRefused("that person is already travelling in another car")
    if car.free_seats <= 0:
        raise SeatRefused("that car is full")

    seat = CarSeat(car_id=car.id, participant_id=participant.id)
    db.add(seat)
    log(db, car.festival_id, "car_seat", car.id, actor,
        f"put {participant.display_name} in {car.label}")
    db.commit()

    notify_participant(
        db,
        festival_id=car.festival_id,
        participant_id=participant.id,
        kind=NotificationKind.CHANGE,
        tool=Tool.TRANSPORT,
        message_key="change.car.added",
        payload={"car": car.label},
        actor_name=actor.display_name,
        actor_user_id=actor.id,
    )
    return seat


def remove_seat(db: DbSession, *, car: Car, participant: Participant, actor: User) -> None:
    seat = db.scalar(
        select(CarSeat).where(
            CarSeat.car_id == car.id, CarSeat.participant_id == participant.id
        )
    )
    if seat is None and car.driver_participant_id != participant.id:
        raise HTTPException(status_code=404, detail="not found")

    if seat is not None:
        db.delete(seat)
    else:
        car.driver_participant_id = None
    log(db, car.festival_id, "car_seat", car.id, actor,
        f"took {participant.display_name} out of {car.label}")
    db.commit()

    notify_participant(
        db,
        festival_id=car.festival_id,
        participant_id=participant.id,
        kind=NotificationKind.CHANGE,
        tool=Tool.TRANSPORT,
        message_key="change.car.removed",
        payload={"car": car.label},
        actor_name=actor.display_name,
        actor_user_id=actor.id,
    )


def add_stop(
    db: DbSession,
    *,
    car: Car,
    address: str,
    lat: float | None,
    lon: float | None,
    participant: Participant | None,
    actor: User,
) -> CarStop:
    position = db.scalar(
        select(func.coalesce(func.max(CarStop.position), 0)).where(CarStop.car_id == car.id)
    )
    stop = CarStop(
        car_id=car.id,
        position=(position or 0) + 1,
        address=address.strip()[:500],
        lat=lat,
        lon=lon,
        participant_id=participant.id if participant else None,
    )
    db.add(stop)
    log(db, car.festival_id, "car_stop", car.id, actor, f"added a stop to {car.label}")
    db.commit()
    return stop


def delete_stop(db: DbSession, *, stop: CarStop, actor: User, festival_id: int) -> None:
    log(db, festival_id, "car_stop", stop.car_id, actor, "removed a stop")
    db.delete(stop)
    db.commit()


def map_points(
    car: Car,
    festival_lat: float | None,
    festival_lon: float | None,
    meeting_points: Sequence[MeetingPoint] = (),
) -> list[MapPoint]:
    """Departure, the car's own pickups, then any meeting point, then the site.

    A meeting point this car joins is drawn as the LAST stop before the
    festival, because that is what it is: the rendezvous happens near the
    site, after everyone has collected their own passengers. Only the points
    this car was ticked for appear on its line — a car arriving a day later
    is not routed through yesterday's meeting.
    """
    points: list[MapPoint] = []
    if car.departure_lat is not None and car.departure_lon is not None:
        points.append(
            MapPoint(car.departure_address or "start", car.departure_lat, car.departure_lon,
                     "start")
        )
    for stop in car.stops:
        if stop.lat is not None and stop.lon is not None:
            points.append(MapPoint(stop.address, stop.lat, stop.lon, "stop"))

    for point in meeting_points:
        if point.lat is None or point.lon is None:
            continue
        if car.id not in {c.id for c in point.cars}:
            continue
        label = point.name
        if point.meet_at is not None:
            label = f"{point.name} — {point.meet_at.strftime('%d.%m. %H:%M')}"
        points.append(MapPoint(label, point.lat, point.lon, "meeting"))

    if festival_lat is not None and festival_lon is not None:
        points.append(MapPoint("festival", festival_lat, festival_lon, "festival"))
    return points


# --- colour names -----------------------------------------------------------

#: Hex values for the drawn car icons and the map legend. Kept here rather than
#: in CSS because the map legend and the SVG both need the same value, and a
#: CSS variable is not readable from the Leaflet layer.
COLOR_HEX = {
    "red": "#c81f2a",
    "black": "#15161a",
    "white": "#f1efeb",
    "silver": "#c9ccd1",
    "grey": "#7c8088",
    "blue": "#2563c8",
    "green": "#2f8f4e",
    "yellow": "#e0bb28",
    "orange": "#e07a1f",
    "purple": "#7b3fbf",
}


def color_labels(translate) -> list[tuple[str, str]]:
    """(key, translated name) pairs for the colour picker, in list order.

    The English key is what the database stores; only the label is translated,
    so switching the interface language does not rewrite anybody's car.
    """
    names = {
        "red": N_("Red"),
        "black": N_("Black"),
        "white": N_("White"),
        "silver": N_("Silver"),
        "grey": N_("Grey"),
        "blue": N_("Blue"),
        "green": N_("Green"),
        "yellow": N_("Yellow"),
        "orange": N_("Orange"),
        "purple": N_("Purple"),
    }
    return [(key, translate(names[key])) for key in CAR_COLORS]


# --- meeting points ---------------------------------------------------------

MAX_MEETING_POINTS = 2


def meeting_points_of(db: DbSession, festival_id: int) -> list[MeetingPoint]:
    return list(
        db.scalars(
            select(MeetingPoint)
            .where(MeetingPoint.festival_id == festival_id)
            .order_by(MeetingPoint.position, MeetingPoint.id)
        ).all()
    )


def add_meeting_point(
    db: DbSession,
    *,
    festival_id: int,
    name: str,
    address: str,
    meet_at: dt.datetime | None,
    car_ids: list[int],
    actor: User,
) -> MeetingPoint:
    existing = meeting_points_of(db, festival_id)
    if len(existing) >= MAX_MEETING_POINTS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"at most {MAX_MEETING_POINTS} meeting points",
        )
    if not name.strip() or not address.strip():
        raise HTTPException(status_code=400, detail="name and address are required")

    point = MeetingPoint(
        festival_id=festival_id,
        name=name.strip()[:80],
        address=address.strip(),
        meet_at=meet_at,
        position=len(existing),
    )
    db.add(point)
    db.flush()
    set_meeting_point_cars(db, point=point, car_ids=car_ids, actor=actor, commit=False)
    log(db, festival_id, "meeting_point", point.id, actor, f"added meeting point {point.name}")
    db.commit()
    return point


def set_meeting_point_cars(
    db: DbSession,
    *,
    point: MeetingPoint,
    car_ids: list[int],
    actor: User,
    commit: bool = True,
) -> MeetingPoint:
    """Replace the car selection wholesale.

    A car id from another festival is dropped rather than refused: the form is
    rendered from this festival's cars, so a foreign id means a tampered or
    stale request, and silently ignoring it cannot leak the other festival's
    existence.
    """
    allowed = {car.id for car in cars_of(db, point.festival_id)}
    wanted = {cid for cid in car_ids if cid in allowed}
    current = point.car_ids

    for link in list(point.cars):
        if link.car_id not in wanted:
            db.delete(link)
    for car_id in wanted - current:
        db.add(MeetingPointCar(meeting_point_id=point.id, car_id=car_id))

    if wanted != current:
        log(
            db,
            point.festival_id,
            "meeting_point",
            point.id,
            actor,
            f"{point.name}: {len(wanted)} of {len(allowed)} cars expected",
        )
    if commit:
        db.commit()
    return point


def delete_meeting_point(db: DbSession, *, point: MeetingPoint, actor: User) -> None:
    log(
        db,
        point.festival_id,
        "meeting_point",
        point.id,
        actor,
        f"removed meeting point {point.name}",
    )
    db.delete(point)
    db.commit()
