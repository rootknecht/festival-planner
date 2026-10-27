"""Building and editing the who-brings-what table.

Participants are the columns, sorted alphabetically and including placeholders;
rows are the things to bring. A cell is a number plus an optional unit taken
from the row's own unit list, which is what makes the result column able to
total "80 L + 120 cans" instead of concatenating free text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models.bringlist import BringCell, BringRow, ChangeLog
from app.models.festival import Participant
from app.models.notification import NotificationKind, Tool
from app.models.user import User, utcnow
from app.services.notifications import notify_participant

MAX_ROWS_PER_FESTIVAL = 200


@dataclass
class Cell:
    participant_id: int
    amount: float = 0.0
    unit: str | None = None
    updated_by_name: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.amount == 0

    @property
    def display(self) -> str:
        if self.is_empty:
            return ""
        amount = int(self.amount) if float(self.amount).is_integer() else self.amount
        return f"{amount} {self.unit}".strip() if self.unit else str(amount)


@dataclass
class Row:
    id: int
    label: str
    units: list[str]
    cells: dict[int, Cell] = field(default_factory=dict)

    @property
    def total(self) -> str:
        """Total per unit: '80 L + 120 cans'. Unitless rows sum plainly."""
        sums: dict[str, float] = {}
        for cell in self.cells.values():
            if cell.is_empty:
                continue
            sums[cell.unit or ""] = sums.get(cell.unit or "", 0.0) + cell.amount
        parts = []
        for unit, value in sorted(sums.items()):
            number = int(value) if float(value).is_integer() else round(value, 2)
            parts.append(f"{number} {unit}".strip())
        return " + ".join(parts)


def participants_of(db: DbSession, festival_id: int) -> list[Participant]:
    return list(
        db.scalars(
            select(Participant)
            .where(Participant.festival_id == festival_id)
            .order_by(func.lower(Participant.display_name))
        )
    )


def build_table(db: DbSession, festival_id: int) -> list[Row]:
    rows = list(
        db.scalars(
            select(BringRow)
            .where(BringRow.festival_id == festival_id)
            .order_by(BringRow.position, BringRow.id)
        )
    )
    by_id = {row.id: Row(id=row.id, label=row.label, units=row.unit_list) for row in rows}
    if not by_id:
        return []
    cells = db.execute(
        select(BringCell, User.display_name)
        .outerjoin(User, User.id == BringCell.updated_by)
        .where(BringCell.row_id.in_(by_id.keys()))
    ).all()
    for cell, updated_by_name in cells:
        by_id[cell.row_id].cells[cell.participant_id] = Cell(
            participant_id=cell.participant_id,
            amount=cell.amount,
            unit=cell.unit,
            updated_by_name=updated_by_name,
        )
    return [by_id[row.id] for row in rows]


def log(db: DbSession, festival_id: int, entity: str, entity_id: int | None,
        actor: User, summary: str) -> None:
    db.add(
        ChangeLog(
            festival_id=festival_id,
            entity=entity,
            entity_id=entity_id,
            actor_id=actor.id,
            actor_name=actor.display_name,
            summary=summary,
        )
    )


def add_row(db: DbSession, festival_id: int, label: str, units: str, actor: User) -> BringRow:
    position = db.scalar(
        select(func.coalesce(func.max(BringRow.position), 0)).where(
            BringRow.festival_id == festival_id
        )
    )
    row = BringRow(
        festival_id=festival_id,
        label=label.strip()[:120],
        units=",".join(u.strip()[:40] for u in units.split(",") if u.strip())[:200],
        position=(position or 0) + 1,
        created_by=actor.id,
    )
    db.add(row)
    db.flush()
    log(db, festival_id, "bring_row", row.id, actor, f"added row “{row.label}”")
    db.commit()
    return row


def delete_row(db: DbSession, row: BringRow, actor: User) -> None:
    log(db, row.festival_id, "bring_row", row.id, actor, f"deleted row “{row.label}”")
    db.delete(row)
    db.commit()


def set_cell(
    db: DbSession,
    *,
    row: BringRow,
    participant: Participant,
    amount: float,
    unit: str | None,
    actor: User,
) -> BringCell:
    """Write one cell, log it, and tell the affected person if it was not them."""
    allowed = row.unit_list
    unit = unit if unit in allowed else (allowed[0] if allowed and amount else None)
    amount = max(0.0, float(amount))

    cell = db.scalar(
        select(BringCell).where(
            BringCell.row_id == row.id, BringCell.participant_id == participant.id
        )
    )
    previous = cell.amount if cell else 0.0

    if cell is None:
        cell = BringCell(row_id=row.id, participant_id=participant.id)
        db.add(cell)
    cell.amount = amount
    cell.unit = unit
    cell.updated_by = actor.id
    cell.updated_at = utcnow()

    if amount == 0:
        key = "change.bring.cleared"
        summary = f"cleared {participant.display_name}'s entry for “{row.label}”"
    elif previous == 0:
        key = "change.bring.assigned"
        summary = f"set {participant.display_name} to bring {cell.unit or ''} for “{row.label}”"
    else:
        key = "change.bring.changed"
        summary = f"changed {participant.display_name}'s amount for “{row.label}”"
    log(db, row.festival_id, "bring_cell", cell.id, actor, summary.strip())
    db.commit()

    notify_participant(
        db,
        festival_id=row.festival_id,
        participant_id=participant.id,
        kind=NotificationKind.CHANGE,
        tool=Tool.BRINGLIST,
        message_key=key,
        payload={"item": row.label},
        actor_name=actor.display_name,
        actor_user_id=actor.id,
    )
    return cell


def recent_changes(db: DbSession, festival_id: int, limit: int = 15) -> list[ChangeLog]:
    return list(
        db.scalars(
            select(ChangeLog)
            .where(ChangeLog.festival_id == festival_id)
            .order_by(ChangeLog.created_at.desc(), ChangeLog.id.desc())
            .limit(limit)
        )
    )
