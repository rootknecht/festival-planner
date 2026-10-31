"""Cars, seats, pickup stops and the geocoding cache."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.festival import Participant
from app.models.user import utcnow

#: Colour names are TRANSLATED at display time (see app/services/transport.py).
#: Storing the English key keeps the database language-neutral — a German label
#: written into the rows would have to be migrated the day someone adds French.
CAR_COLORS = (
    "red",
    "black",
    "white",
    "silver",
    "grey",
    "blue",
    "green",
    "yellow",
    "orange",
    "purple",
)
MIN_SEATS = 1
MAX_SEATS = 9
#: Car size is expressed by the LENGTH of the drawn icon rather than by a
#: separate "type" field: a 2-seater and an 8-seater look different because they
#: ARE different, and one less thing to pick when adding a car.
SEAT_WIDTH_PX = 16
ICON_BASE_PX = 34


class Car(Base):
    __tablename__ = "car"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    festival_id: Mapped[int] = mapped_column(
        ForeignKey("festival.id", ondelete="CASCADE"), nullable=False, index=True
    )
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="red")
    #: Total seats INCLUDING the driver.
    seats_total: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    driver_participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("participant.id", ondelete="SET NULL"), nullable=True
    )
    departure_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    departure_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    departure_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: When this car leaves. Date and time are kept together because "Friday"
    #: without an hour is useless for arranging a pickup, and a time without a
    #: date is ambiguous when people travel on different days.
    #: The DAY the car leaves. Date only, deliberately: an hour agreed weeks
    #: in advance is fiction, and a wrong time is worse than no time because
    #: somebody plans around it. The hour gets settled in chat the night
    #: before; the meeting points carry the real clock times.
    departure_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    driver: Mapped[Participant | None] = relationship(foreign_keys=[driver_participant_id])
    seats: Mapped[list[CarSeat]] = relationship(
        back_populates="car", cascade="all, delete-orphan"
    )
    stops: Mapped[list[CarStop]] = relationship(
        back_populates="car", cascade="all, delete-orphan", order_by="CarStop.position"
    )

    @property
    def label(self) -> str:
        who = self.driver.display_name if self.driver else "no driver"
        return f"{self.color} car ({who})"

    @property
    def occupied(self) -> int:
        return (1 if self.driver_participant_id else 0) + len(self.seats)

    @property
    def free_seats(self) -> int:
        return max(0, self.seats_total - self.occupied)

    @property
    def is_full(self) -> bool:
        return self.free_seats == 0

    @property
    def icon_width_px(self) -> int:
        """Drawn width, so a bigger car looks bigger in the list."""
        return ICON_BASE_PX + SEAT_WIDTH_PX * max(0, self.seats_total - 1)


class Train(Base):
    """A train connection somebody is taking, and who else is on it.

    Deliberately NOT a Car with a different icon. A train has no driver, no
    colour, no pickups and no route to draw on the map; it has a number and a
    station, and above all it has no seat limit worth modelling — a group does
    not run out of room on a train the way it runs out of room in a car. The
    shared shapes are only "people travelling together", which is the list of
    riders.

    Times are optional on purpose. A connection is worth sharing the moment
    you know the train and the station; forcing an arrival time would stop
    people entering what they already know.
    """

    __tablename__ = "train"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    festival_id: Mapped[int] = mapped_column(
        ForeignKey("festival.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Free text, not a number: "ICE 574", "RE 7", "NJ 420" are all normal.
    number: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    departure_station: Mapped[str] = mapped_column(Text, nullable=False, default="")
    arrival_station: Mapped[str] = mapped_column(Text, nullable=False, default="")
    departure_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    #: Unlike a car's departure, a train's time IS knowable in advance — it is
    #: printed on the ticket — so it is offered, but stays optional.
    departure_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    arrival_time: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    riders: Mapped[list[TrainRider]] = relationship(
        back_populates="train", cascade="all, delete-orphan"
    )
    changes: Mapped[list[TrainChange]] = relationship(
        back_populates="train",
        cascade="all, delete-orphan",
        order_by="TrainChange.position",
    )

    @property
    def label(self) -> str:
        parts = [part for part in (self.number, self.departure_station) if part]
        return " from ".join(parts) if len(parts) == 2 else (parts[0] if parts else "train")

    @property
    def occupied(self) -> int:
        return len(self.riders)


class TrainChange(Base):
    """Where this journey changes trains, in order.

    A separate row rather than more columns on Train: a journey can have two
    or three changes, and "change 1 station / change 1 time / change 2 …"
    would be a schema that has to grow every time somebody books a worse
    connection.

    The onward train number lives here because that is what a change IS —
    "Hannover, 11:42, on to ICE 596" is one fact, and splitting it across the
    card would make it unreadable.
    """

    __tablename__ = "train_change"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    train_id: Mapped[int] = mapped_column(
        ForeignKey("train.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    station: Mapped[str] = mapped_column(Text, nullable=False)
    #: Optional, like every other time here: the station is the useful part.
    change_at: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    onward_number: Mapped[str] = mapped_column(String(40), nullable=False, default="")

    train: Mapped[Train] = relationship(back_populates="changes")


class TrainRider(Base):
    """Somebody travelling on this connection. No seat count: a train has room."""

    __tablename__ = "train_rider"
    __table_args__ = (UniqueConstraint("train_id", "participant_id", name="uq_train_rider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    train_id: Mapped[int] = mapped_column(
        ForeignKey("train.id", ondelete="CASCADE"), nullable=False, index=True
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participant.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    train: Mapped[Train] = relationship(back_populates="riders")
    participant: Mapped[Participant] = relationship()


class CarSeat(Base):
    """A passenger seat. The driver is not a seat row — they are the car's driver."""

    __tablename__ = "car_seat"
    __table_args__ = (UniqueConstraint("car_id", "participant_id", name="uq_car_seat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    car_id: Mapped[int] = mapped_column(
        ForeignKey("car.id", ondelete="CASCADE"), nullable=False, index=True
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participant.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    car: Mapped[Car] = relationship(back_populates="seats")
    participant: Mapped[Participant] = relationship()


class CarStop(Base):
    """A pickup on the way, in order."""

    __tablename__ = "car_stop"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    car_id: Mapped[int] = mapped_column(
        ForeignKey("car.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("participant.id", ondelete="SET NULL"), nullable=True
    )

    car: Mapped[Car] = relationship(back_populates="stops")
    participant: Mapped[Participant | None] = relationship()


class MeetingPoint(Base):
    """A shared gathering spot on the way, usually just before the festival.

    Why this is not simply a CarStop on every car: cars do not all join. Some
    people arrive a day later and have no reason to detour to a meeting point
    that happened yesterday. So an admin defines the point once and then picks
    which cars are expected there — "select all", then deselect the latecomers.

    Capped at MAX_MEETING_POINTS by the route layer, not by the schema: the cap
    is a product decision ("one before the motorway, one at the gate"), and a
    database constraint would make raising it a migration.
    """

    __tablename__ = "meeting_point"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    festival_id: Mapped[int] = mapped_column(
        ForeignKey("festival.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Optional: a meeting point with no agreed time is a place, not a plan,
    #: but forcing one would block creating the point before the time is known.
    meet_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    cars: Mapped[list[MeetingPointCar]] = relationship(
        back_populates="meeting_point", cascade="all, delete-orphan"
    )

    @property
    def car_ids(self) -> set[int]:
        return {link.car_id for link in self.cars}


class MeetingPointCar(Base):
    """Which cars are expected at a meeting point."""

    __tablename__ = "meeting_point_car"
    __table_args__ = (
        UniqueConstraint("meeting_point_id", "car_id", name="uq_meeting_point_car"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_point_id: Mapped[int] = mapped_column(
        ForeignKey("meeting_point.id", ondelete="CASCADE"), nullable=False, index=True
    )
    car_id: Mapped[int] = mapped_column(
        ForeignKey("car.id", ondelete="CASCADE"), nullable=False, index=True
    )

    meeting_point: Mapped[MeetingPoint] = relationship(back_populates="cars")
    car: Mapped[Car] = relationship()


class GeocodeCache(Base):
    """Addresses already resolved, so the public geocoder is asked once.

    Required by the Nominatim usage policy and simply polite: the same five
    home addresses would otherwise be looked up on every page view.
    """

    __tablename__ = "geocode_cache"

    query_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
