"""Car sharing: cars, seats and stops.

The central rule is that one person sits in exactly one car. Enforcing it in
one place here means the drag-and-drop UI, the no-JavaScript fallback and any
future client all inherit it.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models.festival import Participant
from app.models.notification import NotificationKind, Tool
from app.models.transport import (
    CAR_COLORS,
    CAR_ICONS,
    MAX_SEATS,
    MIN_SEATS,
    Car,
    CarSeat,
    CarStop,
)
from app.models.user import User
from app.services.bringlist import log
from app.services.notifications import notify_participant


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
    icon: str,
    color: str,
    seats_total: int,
    driver: Participant | None,
    actor: User,
) -> Car:
    if icon not in CAR_ICONS:
        icon = CAR_ICONS[0]
    if color not in CAR_COLORS:
        color = CAR_COLORS[0]
    seats_total = max(MIN_SEATS, min(MAX_SEATS, int(seats_total)))

    if driver is not None and driver.id in seated_participant_ids(db, festival_id):
        raise SeatRefused("that person is already travelling in another car")

    car = Car(
        festival_id=festival_id,
        icon=icon,
        color=color,
        seats_total=seats_total,
        driver_participant_id=driver.id if driver else None,
        created_by=actor.id,
    )
    db.add(car)
    db.flush()
    log(db, festival_id, "car", car.id, actor, f"added {car.label}")
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


def map_points(car: Car, festival_lat: float | None, festival_lon: float | None) -> list[MapPoint]:
    """Departure, then the stops in order, then the festival."""
    points: list[MapPoint] = []
    if car.departure_lat is not None and car.departure_lon is not None:
        points.append(
            MapPoint(car.departure_address or "start", car.departure_lat, car.departure_lon,
                     "start")
        )
    for stop in car.stops:
        if stop.lat is not None and stop.lon is not None:
            points.append(MapPoint(stop.address, stop.lat, stop.lon, "stop"))
    if festival_lat is not None and festival_lon is not None:
        points.append(MapPoint("festival", festival_lat, festival_lon, "festival"))
    return points
