"""Who-brings-what routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session as DbSession

from app.deps import get_db, require_user
from app.models.bringlist import BringRow
from app.models.festival import Participant
from app.models.user import User
from app.realtime.hub import notify_change
from app.services import bringlist, notifications
from app.services.access import require_admin, require_membership, require_writable
from app.templating import render

router = APIRouter(tags=["bringlist"])


def _page(request: Request, db: DbSession, user: User, festival_id: int, **extra) -> Response:
    membership = require_membership(db, user, festival_id)
    participants = bringlist.participants_of(db, festival_id)
    rows = bringlist.build_table(db, festival_id)
    bringing_ids = {
        participant_id
        for row in rows
        for participant_id, cell in row.cells.items()
        if cell and cell.amount
    }
    bringers = [p for p in participants if p.id in bringing_ids]
    mine = next((p for p in participants if p.user_id == user.id), None)
    return render(
        request,
        "tools/bringlist.html",
        {
            "festival": membership.festival,
            "membership": membership,
            "badges": notifications.badges(db, user.id, festival_id),
            "active_tool": "bringlist",
            "participants": participants,
            # Only people who actually bring something appear as rows; the
            # rest are named once underneath so nobody is forgotten.
            "bringers": bringers,
            "not_bringing": [p for p in participants if p.id not in bringing_ids],
            "my_participant_id": mine.id if mine else None,
            "rows": rows,
            "all_units": sorted({unit for row in rows for unit in row.units}),
            "changes": bringlist.recent_changes(db, festival_id),
            **extra,
        },
    )


@router.get("/festivals/{festival_id}/bringlist")
async def table(
    request: Request,
    festival_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    # Opening the table clears its red dot: you have now seen the changes.
    membership = require_membership(db, user, festival_id)
    from app.models.notification import Tool

    notifications.clear_tool(db, user.id, festival_id, Tool.BRINGLIST)
    return _page(request, db, user, membership.festival.id, error=None)


@router.post("/festivals/{festival_id}/bringlist/rows")
async def add_row(
    request: Request,
    festival_id: int,
    label: str = Form(...),
    units: str = Form(default=""),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_writable(require_membership(db, user, festival_id))
    if not label.strip():
        return _page(request, db, user, festival_id, error="A row needs a name.")
    if len(bringlist.build_table(db, festival_id)) >= bringlist.MAX_ROWS_PER_FESTIVAL:
        raise HTTPException(status_code=409, detail="this table has enough rows")
    bringlist.add_row(db, membership.festival.id, label, units, user)
    notify_change(festival_id, "bringlist")
    return RedirectResponse(f"/festivals/{festival_id}/bringlist", status_code=303)


@router.post("/festivals/{festival_id}/bringlist/rows/{row_id}/delete")
async def delete_row(
    request: Request,
    festival_id: int,
    row_id: int,
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    # Anyone may edit a cell, but only an admin may delete a whole row —
    # one wrong click would otherwise wipe everybody's entries.
    require_writable(require_admin(db, user, festival_id))
    row = db.get(BringRow, row_id)
    if row is None or row.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    bringlist.delete_row(db, row, user)
    notify_change(festival_id, "bringlist")
    return RedirectResponse(f"/festivals/{festival_id}/bringlist", status_code=303)


@router.post("/festivals/{festival_id}/bringlist/cells")
async def set_cell(
    request: Request,
    festival_id: int,
    row_id: int = Form(...),
    participant_id: int = Form(...),
    amount: str = Form(default=""),
    unit: str = Form(default=""),
    user: User = Depends(require_user),
    db: DbSession = Depends(get_db),
) -> Response:
    membership = require_writable(require_membership(db, user, festival_id))
    row = db.get(BringRow, row_id)
    participant = db.get(Participant, participant_id)
    if row is None or row.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")
    if participant is None or participant.festival_id != festival_id:
        raise HTTPException(status_code=404, detail="not found")

    # Assigning an item to SOMEBODY ELSE is an admin's job now. Previously
    # anyone could write into anyone's column, which is how you end up
    # responsible for a pavilion you never agreed to. Your own column stays
    # yours, and placeholders (no account) can only be filled in by an admin
    # because nobody else can act for them.
    if not membership.is_admin and participant.user_id != user.id:
        raise HTTPException(
            status_code=403, detail="only an admin can enter this for somebody else"
        )

    try:
        value = float((amount or "0").replace(",", "."))
    except ValueError:
        return _page(request, db, user, festival_id, error="That is not a number.")

    bringlist.set_cell(
        db, row=row, participant=participant, amount=value, unit=unit or None, actor=user
    )
    notify_change(festival_id, "bringlist")
    return RedirectResponse(f"/festivals/{festival_id}/bringlist", status_code=303)
