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


def _amounts_for(
    *,
    mode: SplitMode,
    amount_cents: int,
    everyone: dict[int, Participant],
    share_ids: list[int],
    exact_cents: dict[int, int] | None,
) -> dict[int, int]:
    """Who owes how much, for one split. Shared by creating and editing.

    One implementation rather than two, so an edited expense divides by
    exactly the same rules as a new one — including the cent-by-cent
    remainder, which is what makes the shares add up to the total.
    """
    if mode is SplitMode.EQUAL:
        return split_equally(amount_cents, list(everyone))
    if mode is SplitMode.SELECTION:
        chosen = [pid for pid in share_ids if pid in everyone]
        if not chosen:
            raise ExpenseRefused("pick at least one person to split this between")
        return split_equally(amount_cents, chosen)

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
    return amounts


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

    amounts = _amounts_for(
        mode=split_mode,
        amount_cents=amount_cents,
        everyone=everyone,
        share_ids=share_ids,
        exact_cents=exact_cents,
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

    @property
    def waiting(self) -> bool:
        """Claimed as paid, not yet acknowledged by the other side."""
        return self.state is ShareState.PAID


@dataclass(frozen=True)
class PersonTotal:
    """What stands between you and one other person, netted."""

    participant_id: int
    name: str
    cents: int           # positive: they owe you. negative: you owe them.
    items: list[str]
    waiting: bool        # at least one side has claimed payment, unconfirmed

    @property
    def euro(self) -> str:
        return f"{abs(self.cents) / 100:.2f}".replace(".", ",")


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

    @property
    def by_person(self) -> list[PersonTotal]:
        """One line per person: "69,75 € from Benne (beer, power station)".

        Several debts with the same person collapse into one figure, because
        that is how the money actually moves — one transfer, not one per
        crate of beer. The items stay in the line so the number can be
        checked without opening anything.

        Debts in BOTH directions with the same person net off: if Benne owes
        you 80 and you owe Benne 10, you are owed 70 and should not be shown
        two lines that invite two transfers.
        """
        totals: dict[int, dict] = {}
        for debt in self.owed:
            entry = totals.setdefault(
                debt.other_participant_id,
                {"name": debt.other_name, "cents": 0, "what": [], "waiting": False},
            )
            entry["cents"] += debt.cents
            entry["what"].append(debt.what)
            entry["waiting"] = entry["waiting"] or debt.waiting
        for debt in self.owes:
            entry = totals.setdefault(
                debt.other_participant_id,
                {"name": debt.other_name, "cents": 0, "what": [], "waiting": False},
            )
            entry["cents"] -= debt.cents
            entry["what"].append(debt.what)
            entry["waiting"] = entry["waiting"] or debt.waiting

        lines = [
            PersonTotal(
                participant_id=pid,
                name=entry["name"],
                cents=entry["cents"],
                items=_dedupe(entry["what"]),
                waiting=entry["waiting"],
            )
            for pid, entry in totals.items()
            if entry["cents"] != 0 or entry["waiting"]
        ]
        # Largest amounts first, in both directions: the big ones are the
        # ones worth chasing.
        lines.sort(key=lambda line: (-abs(line.cents), line.name.lower()))
        return lines


def _dedupe(items: list[str]) -> list[str]:
    """Keep order, drop repeats — two beer runs should not read "beer, beer"."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


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


def is_locked(expense: Expense) -> bool:
    """True once any share other than the payer's has moved past OPEN.

    While every share is still OPEN the expense is just a proposal and may be
    re-divided freely. The moment one person has pressed "I paid this" the
    split has become a conversation between two people about a specific
    number, and quietly changing that number underneath them would turn a
    completed handshake into a wrong one.
    """
    return expense.is_locked


def edit_expense(
    db: DbSession,
    *,
    expense: Expense,
    description: str,
    amount_cents: int,
    note: str | None,
    spent_on: dt.date | None,
    actor: User,
    payer: Participant | None = None,
    split_mode: SplitMode | None = None,
    share_ids: list[int] | None = None,
    exact_cents: dict[int, int] | None = None,
) -> Expense:
    """Correct a cost, including who pays and how it divides.

    The full edit — payer and split included — is available while the expense
    is untouched. Once any share has been marked paid or confirmed the split
    locks and only the harmless fields (description, date, note) stay open;
    the amount may then still change, and is re-divided across the shares
    that are still open while settled ones keep the figure that was agreed.
    """
    description = (description or "").strip()
    if not description:
        raise ExpenseRefused("a description is required")

    locked = is_locked(expense)
    changes: list[str] = []

    if description != expense.description:
        changes.append(f"renamed {expense.description} -> {description}")
        expense.description = description[:160]
    if note is not None and (note.strip() or None) != expense.note:
        expense.note = note.strip() or None
        changes.append("note changed")
    if spent_on != expense.spent_on:
        expense.spent_on = spent_on
        changes.append("date changed")

    wants_respit = split_mode is not None or payer is not None
    if wants_respit and locked:
        raise ExpenseRefused(
            "someone has already marked part of this as paid, so the split "
            "cannot be changed any more"
        )

    if wants_respit:
        everyone = {p.id: p for p in participants_of(db, expense.festival_id)}
        if payer is not None:
            if payer.id not in everyone:
                raise ExpenseRefused("the payer is not in this festival")
            if payer.id != expense.payer_participant_id:
                changes.append(
                    f"payer {expense.payer.display_name} -> {payer.display_name}"
                )
                expense.payer_participant_id = payer.id

        mode = split_mode or expense.split_mode
        amounts = _amounts_for(
            mode=mode,
            amount_cents=amount_cents,
            everyone=everyone,
            share_ids=share_ids or [],
            exact_cents=exact_cents,
        )
        amounts.pop(expense.payer_participant_id, None)

        for share in list(expense.shares):
            db.delete(share)
        db.flush()
        for participant_id, cents in amounts.items():
            db.add(
                ExpenseShare(
                    expense_id=expense.id,
                    participant_id=participant_id,
                    amount_cents=cents,
                    state=ShareState.OPEN,
                )
            )
        expense.split_mode = mode
        expense.amount_cents = amount_cents
        changes.append(f"re-split, {amount_cents / 100:.2f} total")
        if changes:
            log(db, expense.festival_id, "expense", expense.id, actor, "; ".join(changes))
        db.commit()
        db.refresh(expense)
        return expense

    if amount_cents != expense.amount_cents:
        old_total = expense.amount_cents
        open_shares = [s for s in expense.shares if s.state is not ShareState.CONFIRMED]
        settled = sum(
            s.amount_cents for s in expense.shares if s.state is ShareState.CONFIRMED
        )
        remaining = amount_cents - settled
        if not open_shares or remaining < 0:
            raise ExpenseRefused(
                "too much of this is already settled to change the amount"
            )
        fresh = split_equally(remaining, [s.participant_id for s in open_shares])
        for share in open_shares:
            share.amount_cents = fresh[share.participant_id]
        changes.append(f"amount {old_total / 100:.2f} -> {amount_cents / 100:.2f}")
        expense.amount_cents = amount_cents

    if changes:
        log(db, expense.festival_id, "expense", expense.id, actor, "; ".join(changes))
    db.commit()
    return expense
