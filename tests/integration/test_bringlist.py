"""M4: the who-brings-what table — totals, permissions, notifications, log."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, reset_engine_for_tests
from app.main import create_app
from app.models.bringlist import BringCell, BringRow, ChangeLog
from app.models.festival import Participant
from app.models.user import User
from app.security.csrf import CSRF_COOKIE
from app.services import notifications
from app.services.mail import capture_sink
from app.services.passwords import hash_password

PASSWORD = "a-long-enough-password"


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    reset_engine_for_tests(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with session_factory() as db:
        for email, name in (("owner@example.org", "Owner"), ("friend@example.org", "Friend")):
            db.add(User(email=email, password_hash=hash_password(PASSWORD), display_name=name))
        db.commit()
    capture_sink().clear()
    return session_factory


@pytest.fixture()
def settings() -> Settings:
    return Settings(_env_file=None, environment="test", base_url="https://example.org")


def client_for(settings: Settings, email: str) -> TestClient:
    client = TestClient(create_app(settings), follow_redirects=False)
    client.get("/login")
    token = client.cookies[CSRF_COOKIE]
    client.post("/login", data={"email": email, "password": PASSWORD, "csrf_token": token})
    return client


def csrf(client: TestClient) -> str:
    return client.cookies[CSRF_COOKIE]


@pytest.fixture()
def festival(factory, settings):
    owner = client_for(settings, "owner@example.org")
    response = owner.post("/festivals", data={"name": "Wacken", "csrf_token": csrf(owner)})
    festival_id = int(response.headers["location"].rsplit("/", 1)[1])
    owner.post(
        f"/festivals/{festival_id}/invites",
        data={"email": "friend@example.org", "csrf_token": csrf(owner)},
    )
    token = capture_sink().sent[-1].body.split("/invite/")[1].split()[0]
    joiner = TestClient(create_app(settings), follow_redirects=False)
    joiner.get(f"/invite/{token}")
    joiner.post(f"/invite/{token}", data={"csrf_token": joiner.cookies[CSRF_COOKIE]})
    friend = client_for(settings, "friend@example.org")
    return festival_id, owner, friend


def add_row(client: TestClient, festival_id: int, label: str, units: str = "") -> int:
    response = client.post(
        f"/festivals/{festival_id}/bringlist/rows",
        data={"label": label, "units": units, "csrf_token": csrf(client)},
    )
    assert response.status_code == 303
    return response.status_code


def set_cell(client, festival_id, row_id, participant_id, amount, unit=""):
    return client.post(
        f"/festivals/{festival_id}/bringlist/cells",
        data={
            "row_id": row_id,
            "participant_id": participant_id,
            "amount": amount,
            "unit": unit,
            "csrf_token": csrf(client),
        },
    )


def ids(factory, festival_id):
    with factory() as db:
        rows = db.scalars(select(BringRow).where(BringRow.festival_id == festival_id)).all()
        people = db.scalars(
            select(Participant).where(Participant.festival_id == festival_id)
        ).all()
    return rows, people


def test_the_table_starts_empty_and_takes_a_first_row(festival, factory) -> None:
    festival_id, owner, _ = festival
    page = owner.get(f"/festivals/{festival_id}/bringlist")
    assert page.status_code == 200
    assert "Nothing on the list yet" in page.text

    add_row(owner, festival_id, "Pavilion")
    page = owner.get(f"/festivals/{festival_id}/bringlist")
    assert "Pavilion" in page.text


def test_participants_are_columns_including_placeholders(festival, factory) -> None:
    festival_id, owner, _ = festival
    owner.post(
        f"/festivals/{festival_id}/participants",
        data={"display_name": "Ghost", "csrf_token": csrf(owner)},
    )
    add_row(owner, festival_id, "Beer", "L,cans")
    page = owner.get(f"/festivals/{festival_id}/bringlist").text
    assert "Owner" in page and "Friend" in page and "Ghost" in page


def test_totals_add_up_per_unit(festival, factory) -> None:
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Beer", "L,cans")
    rows, people = ids(factory, festival_id)
    row = rows[0]
    owner_p = next(p for p in people if p.display_name == "Owner")
    friend_p = next(p for p in people if p.display_name == "Friend")

    set_cell(owner, festival_id, row.id, owner_p.id, "80", "L")
    set_cell(friend, festival_id, row.id, friend_p.id, "120", "cans")

    page = owner.get(f"/festivals/{festival_id}/bringlist").text
    assert "80 L + 120 cans" in page


def test_a_row_without_units_sums_plain_numbers(festival, factory) -> None:
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Camping table")
    rows, people = ids(factory, festival_id)
    owner_p = next(p for p in people if p.display_name == "Owner")
    friend_p = next(p for p in people if p.display_name == "Friend")
    set_cell(owner, festival_id, rows[0].id, owner_p.id, "1")
    set_cell(friend, festival_id, rows[0].id, friend_p.id, "2")
    assert ">3<" in owner.get(f"/festivals/{festival_id}/bringlist").text.replace(" ", "")


def test_you_may_fill_in_your_own_cell(festival, factory) -> None:
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Firewood")
    rows, people = ids(factory, festival_id)
    friend_p = next(p for p in people if p.display_name == "Friend")
    assert set_cell(friend, festival_id, rows[0].id, friend_p.id, "3").status_code == 303


def test_a_member_cannot_put_an_item_on_somebody_else(festival, factory) -> None:
    """Otherwise you discover you are responsible for a pavilion you never
    agreed to, which is how this rule got requested."""
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Firewood")
    rows, people = ids(factory, festival_id)
    owner_p = next(p for p in people if p.display_name == "Owner")
    assert set_cell(friend, festival_id, rows[0].id, owner_p.id, "3").status_code == 403


def test_an_admin_may_assign_an_item_to_anybody(festival, factory) -> None:
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Firewood")
    rows, people = ids(factory, festival_id)
    friend_p = next(p for p in people if p.display_name == "Friend")
    assert set_cell(owner, festival_id, rows[0].id, friend_p.id, "3").status_code == 303


def test_an_admin_gets_a_way_to_enter_something_for_somebody_else(festival, factory) -> None:
    """The table alone cannot offer this.

    Somebody who brings nothing yet has no row to click, and a placeholder
    has no account at all, so without this form an admin can only fill in
    people who already appear in the table.
    """
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Firewood")
    rows, people = ids(factory, festival_id)
    friend_p = next(p for p in people if p.display_name == "Friend")

    page = owner.get(f"/festivals/{festival_id}/bringlist").text
    form = page.split('id="for-who"')[1].split("</select>")[0]
    assert "Add for somebody" in page
    # Everyone is offered, including people with no row in the table yet.
    assert f'value="{friend_p.id}"' in form
    assert "Friend" in form


def test_a_member_is_not_offered_the_assign_to_others_form(festival, factory) -> None:
    """A control nobody may use must not be on the page at all."""
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Firewood")
    page = friend.get(f"/festivals/{festival_id}/bringlist").text
    assert "Add for somebody" not in page
    assert 'id="for-who"' not in page


def test_an_admin_can_enter_for_a_placeholder_who_has_no_account(festival, factory) -> None:
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Firewood")
    owner.post(
        f"/festivals/{festival_id}/participants",
        data={"display_name": "Benne", "csrf_token": csrf(owner)},
    )
    rows, people = ids(factory, festival_id)
    placeholder = next(p for p in people if p.display_name == "Benne")
    assert placeholder.user_id is None

    assert set_cell(owner, festival_id, rows[0].id, placeholder.id, "2").status_code == 303
    page = owner.get(f"/festivals/{festival_id}/bringlist").text
    table = page.split("<tbody>")[1].split("</tbody>")[0]
    assert "Benne" in table


def test_an_archived_festival_offers_nobody_the_form(festival, factory) -> None:
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Firewood")
    owner.post(f"/festivals/{festival_id}/archive", data={"csrf_token": csrf(owner)})
    page = owner.get(f"/festivals/{festival_id}/bringlist").text
    assert 'id="for-who"' not in page


def test_only_people_who_bring_something_appear_in_the_table(festival, factory) -> None:
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Firewood")
    rows, people = ids(factory, festival_id)
    owner_p = next(p for p in people if p.display_name == "Owner")
    set_cell(owner, festival_id, rows[0].id, owner_p.id, "4")

    page = owner.get(f"/festivals/{festival_id}/bringlist").text
    table = page.split("<tbody>")[1].split("</tbody>")[0]
    assert "Owner" in table
    assert "Friend" not in table           # brings nothing yet -> no row
    assert "Not bringing anything yet" in page
    assert "Friend" in page                # but still named, so nobody is lost


def test_items_are_the_columns_and_people_the_rows(festival, factory) -> None:
    """The axes are swapped: twenty columns of names was unusable on a phone."""
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Firewood")
    rows, people = ids(factory, festival_id)
    owner_p = next(p for p in people if p.display_name == "Owner")
    set_cell(owner, festival_id, rows[0].id, owner_p.id, "4")

    page = owner.get(f"/festivals/{festival_id}/bringlist").text
    header = page.split("<thead>")[1].split("</thead>")[0]
    assert "Firewood" in header            # item in the header row
    assert "Owner" not in header           # people are no longer columns


def test_only_an_admin_may_delete_a_row(festival, factory) -> None:
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Gazebo")
    rows, _ = ids(factory, festival_id)
    refused = friend.post(
        f"/festivals/{festival_id}/bringlist/rows/{rows[0].id}/delete",
        data={"csrf_token": csrf(friend)},
    )
    assert refused.status_code == 403
    allowed = owner.post(
        f"/festivals/{festival_id}/bringlist/rows/{rows[0].id}/delete",
        data={"csrf_token": csrf(owner)},
    )
    assert allowed.status_code == 303
    with factory() as db:
        assert db.scalars(select(BringRow)).all() == []


def test_changing_someone_elses_cell_notifies_them(festival, factory) -> None:
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Beer", "L")
    rows, people = ids(factory, festival_id)
    friend_p = next(p for p in people if p.display_name == "Friend")

    set_cell(owner, festival_id, rows[0].id, friend_p.id, "20", "L")

    with factory() as db:
        friend_user = db.scalar(select(User).where(User.email == "friend@example.org"))
        blocks = notifications.open_for(db, friend_user.id, festival_id)
    assert len(blocks) == 1
    assert blocks[0].message_key == "change.bring.assigned"

    page = friend.get(f"/festivals/{festival_id}").text
    assert "Owner noted that you bring Beer." in page


def test_editing_your_own_cell_does_not_notify_you(festival, factory) -> None:
    festival_id, owner, _ = festival
    add_row(owner, festival_id, "Beer", "L")
    rows, people = ids(factory, festival_id)
    owner_p = next(p for p in people if p.display_name == "Owner")
    set_cell(owner, festival_id, rows[0].id, owner_p.id, "10", "L")
    with factory() as db:
        owner_user = db.scalar(select(User).where(User.email == "owner@example.org"))
        assert notifications.open_for(db, owner_user.id, festival_id) == []


def test_clearing_a_cell_notifies_with_the_cleared_message(festival, factory) -> None:
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Beer", "L")
    rows, people = ids(factory, festival_id)
    friend_p = next(p for p in people if p.display_name == "Friend")
    set_cell(owner, festival_id, rows[0].id, friend_p.id, "20", "L")
    friend.get(f"/festivals/{festival_id}/bringlist")  # clears the dot
    set_cell(owner, festival_id, rows[0].id, friend_p.id, "0", "L")
    with factory() as db:
        friend_user = db.scalar(select(User).where(User.email == "friend@example.org"))
        blocks = notifications.open_for(db, friend_user.id, festival_id)
    assert [b.message_key for b in blocks] == ["change.bring.cleared"]


def test_opening_the_table_clears_its_red_dot(festival, factory) -> None:
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Beer", "L")
    rows, people = ids(factory, festival_id)
    friend_p = next(p for p in people if p.display_name == "Friend")
    set_cell(owner, festival_id, rows[0].id, friend_p.id, "20", "L")

    with factory() as db:
        friend_user = db.scalar(select(User).where(User.email == "friend@example.org"))
        assert notifications.badges(db, friend_user.id, festival_id).get("bringlist") == 1

    friend.get(f"/festivals/{festival_id}/bringlist")

    with factory() as db:
        friend_user = db.scalar(select(User).where(User.email == "friend@example.org"))
        assert notifications.badges(db, friend_user.id, festival_id).get("bringlist") is None


def test_every_change_is_logged_with_its_author(festival, factory) -> None:
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Beer", "L")
    rows, people = ids(factory, festival_id)
    friend_p = next(p for p in people if p.display_name == "Friend")
    set_cell(friend, festival_id, rows[0].id, friend_p.id, "20", "L")

    with factory() as db:
        entries = db.scalars(select(ChangeLog).order_by(ChangeLog.id)).all()
    assert entries[0].actor_name == "Owner"
    assert "added row" in entries[0].summary
    assert entries[-1].actor_name == "Friend"

    page = owner.get(f"/festivals/{festival_id}/bringlist").text
    assert "Recent changes" in page


def test_a_bad_amount_is_refused_without_touching_the_cell(festival, factory) -> None:
    festival_id, owner, _ = festival
    add_row(owner, festival_id, "Beer", "L")
    rows, people = ids(factory, festival_id)
    owner_p = next(p for p in people if p.display_name == "Owner")
    response = set_cell(owner, festival_id, rows[0].id, owner_p.id, "a lot", "L")
    assert response.status_code == 200
    assert "not a number" in response.text
    with factory() as db:
        assert db.scalars(select(BringCell)).all() == []


def test_a_unit_outside_the_rows_list_is_not_accepted(festival, factory) -> None:
    festival_id, owner, _ = festival
    add_row(owner, festival_id, "Beer", "L,cans")
    rows, people = ids(factory, festival_id)
    owner_p = next(p for p in people if p.display_name == "Owner")
    set_cell(owner, festival_id, rows[0].id, owner_p.id, "5", "barrels")
    with factory() as db:
        cell = db.scalars(select(BringCell)).one()
    assert cell.unit == "L"


def test_a_stranger_cannot_see_or_change_the_table(factory, settings, festival) -> None:
    festival_id, owner, _ = festival
    add_row(owner, festival_id, "Beer", "L")
    rows, people = ids(factory, festival_id)
    with factory() as db:
        db.add(
            User(
                email="stranger@example.org",
                password_hash=hash_password(PASSWORD),
                display_name="Stranger",
            )
        )
        db.commit()
    stranger = client_for(settings, "stranger@example.org")
    assert stranger.get(f"/festivals/{festival_id}/bringlist").status_code == 404
    assert set_cell(stranger, festival_id, rows[0].id, people[0].id, "1", "L").status_code == 404


def test_an_archived_festival_freezes_the_table(festival, factory) -> None:
    festival_id, owner, _ = festival
    add_row(owner, festival_id, "Beer", "L")
    rows, people = ids(factory, festival_id)
    owner.post(f"/festivals/{festival_id}/archive", data={"csrf_token": csrf(owner)})

    assert owner.get(f"/festivals/{festival_id}/bringlist").status_code == 200
    assert set_cell(owner, festival_id, rows[0].id, people[0].id, "5", "L").status_code == 409
    assert owner.post(
        f"/festivals/{festival_id}/bringlist/rows",
        data={"label": "Late idea", "csrf_token": csrf(owner)},
    ).status_code == 409


def test_cells_cannot_be_written_across_festivals(festival, factory, settings) -> None:
    festival_id, owner, _ = festival
    add_row(owner, festival_id, "Beer", "L")
    other = owner.post("/festivals", data={"name": "Hellfest", "csrf_token": csrf(owner)})
    other_id = int(other.headers["location"].rsplit("/", 1)[1])
    rows, people = ids(factory, festival_id)
    # row from festival A, submitted against festival B
    response = set_cell(owner, other_id, rows[0].id, people[0].id, "5", "L")
    assert response.status_code == 404


def test_an_admin_can_rename_an_item_and_change_its_units(festival, factory) -> None:
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Bier", "L")
    rows, people = ids(factory, festival_id)
    owner_p = next(p for p in people if p.display_name == "Owner")
    set_cell(owner, festival_id, rows[0].id, owner_p.id, "20")

    response = owner.post(
        f"/festivals/{festival_id}/bringlist/rows/{rows[0].id}/edit",
        data={"label": "Beer", "units": "L, crates", "csrf_token": csrf(owner)},
    )
    assert response.status_code == 303
    with factory() as db:
        row = db.get(BringRow, rows[0].id)
        assert row.label == "Beer"
        assert row.unit_list == ["L", "crates"]
        # The amounts survive a unit change: the admin meant to fix the list,
        # not to wipe what everybody had already entered.
        cell = db.scalars(select(BringCell).where(BringCell.row_id == row.id)).one()
        assert cell.amount == 20


def test_a_member_cannot_rename_an_item(festival, factory) -> None:
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Bier", "L")
    rows, _ = ids(factory, festival_id)
    refused = friend.post(
        f"/festivals/{festival_id}/bringlist/rows/{rows[0].id}/edit",
        data={"label": "Beer", "units": "", "csrf_token": csrf(friend)},
    )
    assert refused.status_code == 403


def test_an_item_cannot_be_renamed_to_nothing(festival, factory) -> None:
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Bier", "L")
    rows, _ = ids(factory, festival_id)
    response = owner.post(
        f"/festivals/{festival_id}/bringlist/rows/{rows[0].id}/edit",
        data={"label": "   ", "units": "", "csrf_token": csrf(owner)},
    )
    assert response.status_code == 200
    with factory() as db:
        assert db.get(BringRow, rows[0].id).label == "Bier"


def test_the_table_no_longer_shows_who_last_touched_a_cell(festival, factory) -> None:
    """Requested: the per-cell "by <name>" note was clutter; the change log
    below the table still records it."""
    festival_id, owner, friend = festival
    add_row(owner, festival_id, "Firewood")
    rows, people = ids(factory, festival_id)
    owner_p = next(p for p in people if p.display_name == "Owner")
    set_cell(owner, festival_id, rows[0].id, owner_p.id, "3")

    page = owner.get(f"/festivals/{festival_id}/bringlist").text
    table = page.split("<tbody>")[1].split("</tbody>")[0]
    assert "hint--tiny" not in table
