"""Expenses: adding costs, splitting them, and the settlement handshake."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session as DbSession

from app.deps import get_db, require_user
from app.models.expense import Expense, ExpenseShare, SplitMode
from app.models.festival import Participant
from app.models.notification import Tool
from app.models.user import User
from app.realtime.hub import notify_change
from app.services import expenses, notifications
from app.services.access import require_membership, require_writable
from app.templating import render

router = APIRouter(tags=["expenses"])


def _page(request: Request, db: DbSession, user: User, festival_id: int, **extra) -> Response:
    membership = require_membership(db, user, festival_id)
    participants = expenses.participants_of(db, festival_id)
    mine = next((p for p in participants if p.user_id == user.id), None)
    return render(
        request,
        "tools/expenses.html",
        {
            "festival": membership.festival,
            "membership": membership,
            "badges": notifications.badges(db, user.id, festival_id),
            "active_tool": "expenses",
            "participants": participants,
            "my_participant_id": mine.id if mine else None,
            "expenses": expenses.expenses_of(db, festival_id),
            "balance": expenses.balance_for(db, festival_id, mine.id if mine else None),
            "totals": expenses.totals_by_participant(db, festival_id),
            **extra,
        },
    )


def _participant(db: DbSession, festival_id: int, participant_id: int) -> Participant:
    participant = db.get(Participant, participant_id)
    if participant is None or participant.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    return participant


def _share(db: DbSession, festival_id: int, share_id: int) -> ExpenseShare:
    share = db.get(ExpenseShare, share_id)
    if share is None:
        raise HTTPException(status_code=404, detail="not found")
    expense = db.get(Expense, share.expense_id)
    if expense is None or expense.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    return share


def _my_participant_id(db: DbSession, festival_id: int, user: User) -> int | None:
    mine = next(
        (p for p in expenses.participants_of(db, festival_id) if p.user_id == user.id), None
    )
    return mine.id if mine else None


@router.get("/festivals/{festival_id}/expenses")
async def overview(
    request: Request,
    festival_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    require_membership(db, user, festival_id)
    notifications.clear_tool(db, user.id, festival_id, Tool.EXPENSES)
    return _page(request, db, user, festival_id, error=None)


@router.post("/festivals/{festival_id}/expenses")
async def add(
    request: Request,
    festival_id: int,
    description: str = Form(default=""),
    amount: str = Form(default=""),
    payer_participant_id: int = Form(...),
    split_mode: str = Form(default="equal"),
    share_ids: list[int] = Form(default=[]),
    exact_participant_ids: list[int] = Form(default=[]),
    exact_amounts: list[str] = Form(default=[]),
    note: str = Form(default=""),
    spent_on: str = Form(default=""),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    """Anyone may add a cost — but only under their own name.

    Entering a cost on somebody else's behalf would let one person create a
    debt the other never agreed to, which is the one thing a shared expense
    list must not allow. Admins are no exception here: this is money.
    """
    require_writable(require_membership(db, user, festival_id))
    payer = _participant(db, festival_id, payer_participant_id)
    mine = _my_participant_id(db, festival_id, user)
    if payer.id != mine:
        raise HTTPException(
            status_code=403, detail="you can only record a cost under your own name"
        )

    try:
        mode = SplitMode(split_mode)
    except ValueError:
        mode = SplitMode.EQUAL

    exact: dict[int, int] = {}
    if mode is SplitMode.EXACT:
        for pid, raw in zip(exact_participant_ids, exact_amounts, strict=False):
            text = (raw or "").strip()
            if not text:
                continue
            try:
                exact[pid] = expenses.parse_amount(text)
            except expenses.ExpenseRefused:
                return _page(
                    request, db, user, festival_id, error="One of the amounts is not a number."
                )

    when: dt.date | None = None
    if spent_on.strip():
        try:
            when = dt.date.fromisoformat(spent_on.strip())
        except ValueError:
            when = None

    try:
        expenses.add_expense(
            db,
            festival_id=festival_id,
            payer=payer,
            description=description,
            amount_cents=expenses.parse_amount(amount),
            split_mode=mode,
            share_ids=share_ids,
            exact_cents=exact,
            note=note,
            spent_on=when,
            actor=user,
        )
    except expenses.ExpenseRefused as exc:
        return _page(request, db, user, festival_id, error=exc.detail)

    notify_change(festival_id, "expenses")
    return RedirectResponse(f"/festivals/{festival_id}/expenses", status_code=303)


@router.post("/festivals/{festival_id}/expenses/{expense_id}/delete")
async def delete(
    request: Request,
    festival_id: int,
    expense_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    """The payer or an admin may remove a cost."""
    membership = require_writable(require_membership(db, user, festival_id))
    expense = db.get(Expense, expense_id)
    if expense is None or expense.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    mine = _my_participant_id(db, festival_id, user)
    if not membership.is_admin and expense.payer_participant_id != mine:
        raise HTTPException(status_code=403, detail="only the payer or an admin")
    expenses.delete_expense(db, expense=expense, actor=user)
    notify_change(festival_id, "expenses")
    return RedirectResponse(f"/festivals/{festival_id}/expenses", status_code=303)


@router.post("/festivals/{festival_id}/expenses/shares/{share_id}/paid")
async def mark_paid(
    request: Request,
    festival_id: int,
    share_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    """Step one of the handshake, performed by whoever owes the money."""
    membership = require_writable(require_membership(db, user, festival_id))
    share = _share(db, festival_id, share_id)
    mine = _my_participant_id(db, festival_id, user)
    # An admin may act for a placeholder, who has no account and therefore no
    # way to press the button themselves.
    acting_for_placeholder = membership.is_admin and share.participant.user_id is None
    if share.participant_id != mine and not acting_for_placeholder:
        raise HTTPException(status_code=403, detail="only the person who owes this")
    try:
        expenses.mark_paid(db, share=share, actor=user)
    except expenses.ExpenseRefused as exc:
        return _page(request, db, user, festival_id, error=exc.detail)
    notify_change(festival_id, "expenses")
    return RedirectResponse(f"/festivals/{festival_id}/expenses", status_code=303)


@router.post("/festivals/{festival_id}/expenses/shares/{share_id}/received")
async def confirm_received(
    request: Request,
    festival_id: int,
    share_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    """Step two, and ONLY the person who is owed the money may do it.

    This is the whole point of the handshake: one person cannot declare their
    own debt settled.
    """
    membership = require_writable(require_membership(db, user, festival_id))
    share = _share(db, festival_id, share_id)
    expense = share.expense
    mine = _my_participant_id(db, festival_id, user)
    acting_for_placeholder = membership.is_admin and expense.payer.user_id is None
    if expense.payer_participant_id != mine and not acting_for_placeholder:
        raise HTTPException(status_code=403, detail="only the person who is owed this")
    try:
        expenses.confirm_received(db, share=share, actor=user)
    except expenses.ExpenseRefused as exc:
        return _page(request, db, user, festival_id, error=exc.detail)
    notify_change(festival_id, "expenses")
    return RedirectResponse(f"/festivals/{festival_id}/expenses", status_code=303)
