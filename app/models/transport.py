"""Cars, seats, pickup stops and the geocoding cache."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.festival import Participant
from app.models.user import utcnow

#: Built-in icon set — no uploads, so no file validation, no EXIF, no quota.
CAR_ICONS = ("hatchback", "estate", "van", "bus", "camper", "motorbike")
CAR_COLORS = ("red", "black", "white", "blue", "green", "yellow", "orange", "purple")
MIN_SEATS = 1
MAX_SEATS = 5


class Car(Base):
    __tablename__ = "car"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    festival_id: Mapped[int] = mapped_column(
        ForeignKey("festival.id", ondelete="CASCADE"), nullable=False, index=True
    )
    icon: Mapped[str] = mapped_column(String(24), nullable=False, default="hatchback")
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="red")
    #: Total seats INCLUDING the driver, 1–5.
    seats_total: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    driver_participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("participant.id", ondelete="SET NULL"), nullable=True
    )
    departure_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    departure_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    departure_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
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
        return f"{self.color} {self.icon} ({who})"

    @property
    def occupied(self) -> int:
        return (1 if self.driver_participant_id else 0) + len(self.seats)

    @property
    def free_seats(self) -> int:
        return max(0, self.seats_total - self.occupied)


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
