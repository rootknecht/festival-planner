"""Expense splitting, balances and the settlement handshake."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models.expense import (
    MAX_AMOUNT_CENTS,
    Expense,
    ExpenseShare,
    ShareState,
    SplitMode,
)
from app.models.festival import Participant
from app.models.notification import NotificationKind, Tool
from app.models.user import User, utcnow
from app.services.bringlist import log
from app.services.notifications import notify_participant


class ExpenseRefused(HTTPException):
    def __init__(self, detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> None:
        super().__init__(status_code=code, detail=detail)


def parse_amount(raw: str) -> int:
    """Read a human amount into cents.

    Accepts both "12,50" and "12.50" because a German keyboard produces the
    comma and the HTML number input produces the dot, and a group typing on
    phones will produce both within the same evening.
    """
    text = (raw or "").strip().replace(" ", "").replace("€", "")
    if not text:
        raise ExpenseRefused("an amount is required")
    text = text.replace(",", ".")
    try:
        cents = int(round(float(text) * 100))
    except ValueError as exc:
        raise ExpenseRefused("that is not an amount") from exc
    if cents <= 0:
        raise ExpenseRefused("the amount must be greater than zero")
    if cents > MAX_AMOUNT_CENTS:
        raise ExpenseRefused("that amount looks like a typo")
    return cents


def split_equally(total_cents: int, participant_ids: list[int]) -> dict[int, int]:
    """Divide cents as evenly as cents allow.

    The remainder is handed out one cent at a time rather than dropped, so the
    shares always add up to the total exactly — otherwise a 10 euro bill split
    three ways loses a cent and the balances never reach zero.
    """
    if not participant_ids:
        raise ExpenseRefused("nobody to split this between")
    base, remainder = divmod(total_cents, len(participant_ids))
    shares = {pid: base for pid in participant_ids}
    for pid in participant_ids[:remainder]:
        shares[pid] += 1
    return shares


def participants_of(db: DbSession, festival_id: int) -> list[Participant]:
    return list(
        db.scalars(
            select(Participant)
            .where(Participant.festival_id == festival_id)
            .order_by(func.lower(Participant.display_name))
        ).all()
    )


def expenses_of(db: DbSession, festival_id: int) -> list[Expense]:
    return list(
        db.scalars(
            select(Expense)
            .where(Expense.festival_id == festival_id)
            .order_by(Expense.created_at.desc(), Expense.id.desc())
        ).all()
    )


def add_expense(
    db: DbSession,
    *,
    festival_id: int,
    payer: Participant,
    description: str,
    amount_cents: int,
    split_mode: SplitMode,
    share_ids: list[int],
    exact_cents: dict[int, int] | None,
    note: str | None,
    spent_on: dt.date | None,
    actor: User,
) -> Expense:
    description = (description or "").strip()
    if not description:
        raise ExpenseRefused("a description is required")

    everyone = {p.id: p for p in participants_of(db, festival_id)}
    if payer.id not in everyone:
        raise ExpenseRefused("the payer is not in this festival")

    if split_mode is SplitMode.EQUAL:
        chosen = list(everyone)
        amounts = split_equally(amount_cents, chosen)
    elif split_mode is SplitMode.SELECTION:
        chosen = [pid for pid in share_ids if pid in everyone]
        if not chosen:
            raise ExpenseRefused("pick at least one person to split this between")
        amounts = split_equally(amount_cents, chosen)
    else:
        amounts = {
            pid: cents
            for pid, cents in (exact_cents or {}).items()
            if pid in everyone and cents > 0
        }
        if not amounts:
            raise ExpenseRefused("enter at least one amount")
        total = sum(amounts.values())
        if total != amount_cents:
            # Refusing beats silently adjusting: a mismatch means the person
            # mistyped either the total or a share, and only they know which.
            raise ExpenseRefused(
                "the individual amounts add up to "
                f"{total / 100:.2f} instead of {amount_cents / 100:.2f}"
            )

    expense = Expense(
        festival_id=festival_id,
        payer_participant_id=payer.id,
        description=description[:160],
        amount_cents=amount_cents,
        split_mode=split_mode,
        note=(note or "").strip() or None,
        spent_on=spent_on,
        created_by=actor.id,
    )
    db.add(expense)
    db.flush()

    for pid, cents in amounts.items():
        # The payer's own share is recorded but starts CONFIRMED: they do not
        # owe themselves anything, and keeping the row means the split stays
        # visible and auditable.
        state = ShareState.CONFIRMED if pid == payer.id else ShareState.OPEN
        db.add(
            ExpenseShare(
                expense_id=expense.id,
                participant_id=pid,
                amount_cents=cents,
                state=state,
                confirmed_at=utcnow() if state is ShareState.CONFIRMED else None,
            )
        )

    log(
        db,
        festival_id,
        "expense",
        expense.id,
        actor,
        f"added {description} ({amount_cents / 100:.2f})",
    )
    db.commit()

    for pid, cents in amounts.items():
        if pid == payer.id:
            continue
        notify_participant(
            db,
            festival_id=festival_id,
            participant_id=pid,
            kind=NotificationKind.CHANGE,
            tool=Tool.EXPENSES,
            message_key="change.expense.added",
            payload={"what": description, "amount": f"{cents / 100:.2f}"},
            actor_name=payer.display_name,
            actor_user_id=actor.id,
        )
    return expense


def delete_expense(db: DbSession, *, expense: Expense, actor: User) -> None:
    """Remove an expense entirely.

    Allowed even part-settled: the usual reason for deleting is that it was
    entered twice or on the wrong person, and leaving a wrong debt in the
    balance is worse than losing the handshake state on a mistake.
    """
    log(
        db,
        expense.festival_id,
        "expense",
        expense.id,
        actor,
        f"removed {expense.description}",
    )
    db.delete(expense)
    db.commit()


def mark_paid(db: DbSession, *, share: ExpenseShare, actor: User) -> ExpenseShare:
    """Step one of the handshake: the person who owes says they have paid."""
    if share.state is ShareState.CONFIRMED:
        raise ExpenseRefused("this one is already settled", status.HTTP_409_CONFLICT)
    share.state = ShareState.PAID
    share.paid_at = utcnow()
    expense = share.expense
    log(
        db,
        expense.festival_id,
        "expense_share",
        share.id,
        actor,
        f"marked {expense.description} as paid",
    )
    db.commit()

    # Tell the person who is owed the money — they have to confirm, and an
    # unseen "paid" claim would stall the handshake forever.
    notify_participant(
        db,
        festival_id=expense.festival_id,
        participant_id=expense.payer_participant_id,
        kind=NotificationKind.CHANGE,
        tool=Tool.EXPENSES,
        message_key="change.expense.paid",
        payload={"what": expense.description, "amount": f"{share.amount:.2f}"},
        actor_name=share.participant.display_name,
        actor_user_id=actor.id,
    )
    return share


def confirm_received(db: DbSession, *, share: ExpenseShare, actor: User) -> ExpenseShare:
    """Step two: only the person owed the money can close it out."""
    if share.state is ShareState.OPEN:
        raise ExpenseRefused("this has not been paid yet", status.HTTP_409_CONFLICT)
    if share.state is ShareState.CONFIRMED:
        return share
    share.state = ShareState.CONFIRMED
    share.confirmed_at = utcnow()
    expense = share.expense
    log(
        db,
        expense.festival_id,
        "expense_share",
        share.id,
        actor,
        f"confirmed receipt for {expense.description}",
    )
    db.commit()

    notify_participant(
        db,
        festival_id=expense.festival_id,
        participant_id=share.participant_id,
        kind=NotificationKind.CHANGE,
        tool=Tool.EXPENSES,
        message_key="change.expense.received",
        payload={"what": expense.description, "amount": f"{share.amount:.2f}"},
        actor_name=expense.payer.display_name,
        actor_user_id=actor.id,
    )
    return share


@dataclass(frozen=True)
class Debt:
    """One outstanding amount between two people."""

    share_id: int
    other_name: str
    other_participant_id: int
    what: str
    cents: int
    state: ShareState

    @property
    def amount(self) -> float:
        return self.cents / 100


@dataclass(frozen=True)
class Balance:
    """What one person owes and is owed, with the details behind both."""

    owes: list[Debt]
    owed: list[Debt]

    @property
    def owes_cents(self) -> int:
        return sum(d.cents for d in self.owes)

    @property
    def owed_cents(self) -> int:
        return sum(d.cents for d in self.owed)

    @property
    def net_cents(self) -> int:
        return self.owed_cents - self.owes_cents

    @property
    def has_anything(self) -> bool:
        return bool(self.owes or self.owed)


def balance_for(db: DbSession, festival_id: int, participant_id: int | None) -> Balance:
    """Everything still outstanding for one person, both directions.

    A share counts as outstanding until it is CONFIRMED: money claimed as sent
    but not yet acknowledged is still somebody's problem, so PAID shares stay
    in the list, marked as waiting.
    """
    if participant_id is None:
        return Balance(owes=[], owed=[])

    rows = db.scalars(
        select(ExpenseShare)
        .join(Expense, ExpenseShare.expense_id == Expense.id)
        .where(
            Expense.festival_id == festival_id,
            ExpenseShare.state != ShareState.CONFIRMED,
        )
    ).all()

    owes: list[Debt] = []
    owed: list[Debt] = []
    for share in rows:
        expense = share.expense
        if share.participant_id == participant_id:
            owes.append(
                Debt(
                    share_id=share.id,
                    other_name=expense.payer.display_name,
                    other_participant_id=expense.payer_participant_id,
                    what=expense.description,
                    cents=share.amount_cents,
                    state=share.state,
                )
            )
        elif expense.payer_participant_id == participant_id:
            owed.append(
                Debt(
                    share_id=share.id,
                    other_name=share.participant.display_name,
                    other_participant_id=share.participant_id,
                    what=expense.description,
                    cents=share.amount_cents,
                    state=share.state,
                )
            )
    return Balance(owes=owes, owed=owed)


def totals_by_participant(db: DbSession, festival_id: int) -> dict[int, int]:
    """Net position per person in cents: positive means they are owed money."""
    result: dict[int, int] = {}
    for expense in expenses_of(db, festival_id):
        for share in expense.shares:
            if share.state is ShareState.CONFIRMED:
                continue
            result[share.participant_id] = (
                result.get(share.participant_id, 0) - share.amount_cents
            )
            result[expense.payer_participant_id] = (
                result.get(expense.payer_participant_id, 0) + share.amount_cents
            )
    return result
