"""Shared expenses: who paid for what, who owes whom, and settling up.

DESIGN NOTES

Amounts are stored as INTEGER CENTS, never as floats. 0.1 + 0.2 is not 0.3 in
binary floating point, and a group that splits a 100 euro crate of beer three
ways and then settles up will find the rounding error; cents make the arithmetic
exact and the split remainder explicit.

A share is a row per person per expense, written once when the expense is
created or edited. The alternative — computing shares on the fly from a split
rule — breaks the moment somebody joins or leaves the festival after the
expense was entered: the old crate of beer must stay split the way it was
agreed, not silently re-divided among the current participants.

Settling is a TWO-STEP HANDSHAKE, as requested: the payer marks a share paid,
and only the person who is owed the money confirms receipt. Nobody can
unilaterally declare a debt gone, which is the one thing that makes a shared
expense list usable between friends.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.festival import Participant
from app.models.user import utcnow

#: Cap on a single expense, in cents. A festival trip is not a mortgage, and a
#: typo of a few extra zeros is far more likely than a genuine six-figure cost.
MAX_AMOUNT_CENTS = 1_000_000


class SplitMode(enum.StrEnum):
    EQUAL = "equal"  # everybody in the festival
    SELECTION = "selection"  # a chosen subset
    EXACT = "exact"  # an explicit amount per person


class ShareState(enum.StrEnum):
    """Where a single debt stands.

    OPEN -> PAID -> CONFIRMED, and only forward. The payer moves OPEN to PAID;
    only the receiver moves PAID to CONFIRMED. A CONFIRMED share leaves the
    balance for good.
    """

    OPEN = "open"
    PAID = "paid"
    CONFIRMED = "confirmed"


class Expense(Base):
    __tablename__ = "expense"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    festival_id: Mapped[int] = mapped_column(
        ForeignKey("festival.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Who actually spent the money. Always a participant of this festival, so
    #: a placeholder (somebody without an account) can be the payer too — one
    #: person often pays for a friend who is not online.
    payer_participant_id: Mapped[int] = mapped_column(
        ForeignKey("participant.id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(String(160), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    split_mode: Mapped[SplitMode] = mapped_column(
        Enum(SplitMode, native_enum=False, length=16), nullable=False, default=SplitMode.EQUAL
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    spent_on: Mapped[dt.date | None] = mapped_column(nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    payer: Mapped[Participant] = relationship(foreign_keys=[payer_participant_id])
    shares: Mapped[list[ExpenseShare]] = relationship(
        back_populates="expense", cascade="all, delete-orphan"
    )

    @property
    def amount(self) -> float:
        return self.amount_cents / 100

    @property
    def is_locked(self) -> bool:
        """True once any share has left OPEN.

        A share somebody marked paid is half of a completed handshake.
        Re-dividing it would change the number the other person is about to
        confirm, so the payer and the split freeze at that moment, while the
        description, date, note and total stay editable.

        The payer's OWN share does not count: it is created CONFIRMED on day
        one, because nobody owes themselves anything. Counting it would have
        locked every expense from birth.
        """
        return any(
            share.state is not ShareState.OPEN
            for share in self.shares
            if share.participant_id != self.payer_participant_id
        )

    @property
    def settled(self) -> bool:
        """True when nothing is outstanding any more."""
        return all(
            share.state == ShareState.CONFIRMED or share.participant_id == self.payer_participant_id
            for share in self.shares
        )


class ExpenseShare(Base):
    """What one person owes for one expense, and how far settling has got."""

    __tablename__ = "expense_share"
    __table_args__ = (
        UniqueConstraint("expense_id", "participant_id", name="uq_expense_share"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    expense_id: Mapped[int] = mapped_column(
        ForeignKey("expense.id", ondelete="CASCADE"), nullable=False, index=True
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participant.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[ShareState] = mapped_column(
        Enum(ShareState, native_enum=False, length=16), nullable=False, default=ShareState.OPEN
    )
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    expense: Mapped[Expense] = relationship(back_populates="shares")
    participant: Mapped[Participant] = relationship()

    @property
    def amount(self) -> float:
        return self.amount_cents / 100

    @property
    def is_open(self) -> bool:
        return self.state == ShareState.OPEN

    @property
    def awaits_confirmation(self) -> bool:
        return self.state == ShareState.PAID
