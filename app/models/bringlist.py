"""The who-brings-what table and the change log behind it."""

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
from app.models.user import utcnow


class BringRow(Base):
    """One thing to bring: "Pavilion", "Beer", "Camping table"."""

    __tablename__ = "bring_row"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    festival_id: Mapped[int] = mapped_column(
        ForeignKey("festival.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    # Comma-separated unit list chosen by whoever created the row, e.g.
    # "L,cans,crates". Empty means the row counts plain numbers.
    units: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    cells: Mapped[list[BringCell]] = relationship(
        back_populates="row", cascade="all, delete-orphan"
    )

    @property
    def unit_list(self) -> list[str]:
        return [unit.strip() for unit in self.units.split(",") if unit.strip()]


class BringCell(Base):
    """What one participant brings for one row."""

    __tablename__ = "bring_cell"
    __table_args__ = (UniqueConstraint("row_id", "participant_id", name="uq_bring_cell"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    row_id: Mapped[int] = mapped_column(
        ForeignKey("bring_row.id", ondelete="CASCADE"), nullable=False, index=True
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participant.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    row: Mapped[BringRow] = relationship(back_populates="cells")


class ChangeLog(Base):
    """Who changed what, so an accidental edit can be traced to a person."""

    __tablename__ = "change_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    festival_id: Mapped[int] = mapped_column(
        ForeignKey("festival.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    actor_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
