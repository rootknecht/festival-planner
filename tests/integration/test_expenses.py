"""Expenses: splitting, rounding, the settlement handshake and who may do what."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, reset_engine_for_tests
from app.main import create_app
from app.models.expense import Expense, ExpenseShare, ShareState
from app.models.festival import Participant
from app.models.user import User
from app.security.csrf import CSRF_COOKIE
from app.services import expenses as service
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
        for email, name in (
            ("owner@example.org", "Owner"),
            ("kai@example.org", "Kai"),
            ("anna@example.org", "Anna"),
        ):
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


def join(owner, settings, festival_id, email):
    owner.post(
        f"/festivals/{festival_id}/invites",
        data={"email": email, "csrf_token": csrf(owner)},
    )
    token = capture_sink().sent[-1].body.split("/invite/")[1].split()[0]
    member = client_for(settings, email)
    member.get(f"/invite/{token}")
    member.post(f"/invite/{token}", data={"csrf_token": csrf(member)})
    return client_for(settings, email)


@pytest.fixture()
def trip(factory, settings):
    """A festival with three real accounts: Owner (admin), Kai and Anna."""
    owner = client_for(settings, "owner@example.org")
    created = owner.post("/festivals", data={"name": "Wacken", "csrf_token": csrf(owner)})
    festival_id = int(created.headers["location"].rsplit("/", 1)[1])
    kai = join(owner, settings, festival_id, "kai@example.org")
    anna = join(owner, settings, festival_id, "anna@example.org")
    return festival_id, owner, kai, anna


def who(factory, festival_id) -> dict[str, Participant]:
    with factory() as db:
        rows = db.scalars(
            select(Participant).where(Participant.festival_id == festival_id)
        ).all()
    return {row.display_name: row for row in rows}


def add_cost(client, festival_id, payer_id, amount="30,00", description="Beer",
             split_mode="equal", share_ids=None, exact=None, note=""):
    data = {
        "csrf_token": csrf(client),
        "payer_participant_id": str(payer_id),
        "description": description,
        "amount": amount,
        "split_mode": split_mode,
        "note": note,
    }
    if share_ids is not None:
        data["share_ids"] = [str(i) for i in share_ids]
    if exact is not None:
        data["exact_participant_ids"] = [str(pid) for pid, _ in exact]
        data["exact_amounts"] = [value for _, value in exact]
    return client.post(f"/festivals/{festival_id}/expenses", data=data)


def shares_of(factory, description="Beer") -> list[ExpenseShare]:
    with factory() as db:
        expense = db.scalars(select(Expense).where(Expense.description == description)).one()
        return list(
            db.scalars(
                select(ExpenseShare).where(ExpenseShare.expense_id == expense.id)
            ).all()
        )


# --- arithmetic -------------------------------------------------------------


def test_amounts_are_parsed_from_both_comma_and_dot() -> None:
    """A German keyboard gives the comma, an HTML number input gives the dot."""
    assert service.parse_amount("12,50") == 1250
    assert service.parse_amount("12.50") == 1250
    assert service.parse_amount(" 7 € ") == 700


def test_a_nonsense_amount_is_refused() -> None:
    for bad in ("", "lots", "0", "-5"):
        with pytest.raises(service.ExpenseRefused):
            service.parse_amount(bad)


def test_an_absurd_amount_is_refused_as_a_typo() -> None:
    with pytest.raises(service.ExpenseRefused):
        service.parse_amount("99999999")


def test_the_split_remainder_is_handed_out_not_dropped() -> None:
    """10 euro between three must still add up to 10 euro."""
    shares = service.split_equally(1000, [1, 2, 3])
    assert sum(shares.values()) == 1000
    assert sorted(shares.values()) == [333, 333, 334]


def test_money_never_goes_through_a_float() -> None:
    """0.1 + 0.2 != 0.3 in binary, so a cents-based split must stay exact."""
    shares = service.split_equally(10, [1, 2, 3])
    assert sum(shares.values()) == 10


# --- adding a cost ----------------------------------------------------------


def test_anyone_can_add_a_cost_under_their_own_name(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    assert add_cost(kai, festival_id, people["Kai"].id).status_code == 303
    with factory() as db:
        expense = db.scalars(select(Expense)).one()
        assert expense.payer_participant_id == people["Kai"].id
        assert expense.amount_cents == 3000


def test_nobody_can_record_a_cost_on_somebody_else(trip, factory) -> None:
    """Otherwise one person could invent a debt the other never agreed to."""
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    assert add_cost(kai, festival_id, people["Anna"].id).status_code == 403
    # Not even an admin: this is money.
    assert add_cost(owner, festival_id, people["Anna"].id).status_code == 403


def test_an_equal_split_covers_everyone_and_the_payer_owes_nothing(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    add_cost(kai, festival_id, people["Kai"].id, amount="30,00")

    shares = shares_of(factory)
    assert len(shares) == 3
    assert sum(s.amount_cents for s in shares) == 3000
    payer = next(s for s in shares if s.participant_id == people["Kai"].id)
    assert payer.state is ShareState.CONFIRMED  # you do not owe yourself
    assert all(s.state is ShareState.OPEN for s in shares if s is not payer)


def test_a_selection_split_only_charges_the_chosen(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    add_cost(
        kai,
        festival_id,
        people["Kai"].id,
        amount="20,00",
        split_mode="selection",
        share_ids=[people["Kai"].id, people["Anna"].id],
    )
    shares = shares_of(factory)
    assert {s.participant_id for s in shares} == {people["Kai"].id, people["Anna"].id}
    assert all(s.amount_cents == 1000 for s in shares)


def test_an_exact_split_must_add_up(trip, factory) -> None:
    """A mismatch means a typo, and only the person typing knows which number
    is wrong — so it is refused rather than quietly adjusted."""
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    response = add_cost(
        kai,
        festival_id,
        people["Kai"].id,
        amount="30,00",
        split_mode="exact",
        exact=[(people["Kai"].id, "10,00"), (people["Anna"].id, "5,00")],
    )
    assert response.status_code == 200
    assert "add up to" in response.text
    with factory() as db:
        assert db.scalars(select(Expense)).all() == []


def test_an_exact_split_that_adds_up_is_accepted(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    assert add_cost(
        kai,
        festival_id,
        people["Kai"].id,
        amount="30,00",
        split_mode="exact",
        exact=[
            (people["Kai"].id, "10,00"),
            (people["Anna"].id, "15,00"),
            (people["Owner"].id, "5,00"),
        ],
    ).status_code == 303
    shares = shares_of(factory)
    assert sum(s.amount_cents for s in shares) == 3000
    assert {s.amount_cents for s in shares} == {1000, 1500, 500}


def test_a_cost_without_a_description_is_refused(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    response = add_cost(kai, festival_id, people["Kai"].id, description="  ")
    assert response.status_code == 200
    with factory() as db:
        assert db.scalars(select(Expense)).all() == []


# --- the settlement handshake ----------------------------------------------


def _open_share(factory, participant_id) -> int:
    for share in shares_of(factory):
        if share.participant_id == participant_id:
            return share.id
    raise AssertionError("no share for that participant")


def test_the_person_who_owes_marks_it_paid_and_it_is_not_settled_yet(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    add_cost(kai, festival_id, people["Kai"].id, amount="30,00")
    share_id = _open_share(factory, people["Anna"].id)

    assert anna.post(
        f"/festivals/{festival_id}/expenses/shares/{share_id}/paid",
        data={"csrf_token": csrf(anna)},
    ).status_code == 303

    with factory() as db:
        share = db.get(ExpenseShare, share_id)
        assert share.state is ShareState.PAID
        assert share.paid_at is not None
    # Still counted: a claim of payment is not a receipt.
    with factory() as db:
        balance = service.balance_for(db, festival_id, people["Anna"].id)
    assert balance.owes_cents == 1000


def test_only_the_receiver_can_confirm(trip, factory) -> None:
    """The one rule that makes this usable between friends: you cannot declare
    your own debt gone."""
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    add_cost(kai, festival_id, people["Kai"].id, amount="30,00")
    share_id = _open_share(factory, people["Anna"].id)
    anna.post(
        f"/festivals/{festival_id}/expenses/shares/{share_id}/paid",
        data={"csrf_token": csrf(anna)},
    )

    refused = anna.post(
        f"/festivals/{festival_id}/expenses/shares/{share_id}/received",
        data={"csrf_token": csrf(anna)},
    )
    assert refused.status_code == 403
    # An unrelated member cannot either.
    assert owner.post(
        f"/festivals/{festival_id}/expenses/shares/{share_id}/received",
        data={"csrf_token": csrf(owner)},
    ).status_code == 403

    accepted = kai.post(
        f"/festivals/{festival_id}/expenses/shares/{share_id}/received",
        data={"csrf_token": csrf(kai)},
    )
    assert accepted.status_code == 303
    with factory() as db:
        assert db.get(ExpenseShare, share_id).state is ShareState.CONFIRMED


def test_a_confirmed_share_leaves_the_balance(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    add_cost(kai, festival_id, people["Kai"].id, amount="30,00")
    share_id = _open_share(factory, people["Anna"].id)
    anna.post(f"/festivals/{festival_id}/expenses/shares/{share_id}/paid",
              data={"csrf_token": csrf(anna)})
    kai.post(f"/festivals/{festival_id}/expenses/shares/{share_id}/received",
             data={"csrf_token": csrf(kai)})

    with factory() as db:
        anna_balance = service.balance_for(db, festival_id, people["Anna"].id)
        kai_balance = service.balance_for(db, festival_id, people["Kai"].id)
    assert anna_balance.owes_cents == 0
    assert kai_balance.owed_cents == 1000  # the owner's share is still open


def test_confirming_cannot_skip_the_paid_step(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    add_cost(kai, festival_id, people["Kai"].id, amount="30,00")
    share_id = _open_share(factory, people["Anna"].id)
    response = kai.post(
        f"/festivals/{festival_id}/expenses/shares/{share_id}/received",
        data={"csrf_token": csrf(kai)},
    )
    assert response.status_code == 200
    assert "not been paid" in response.text


def test_only_the_debtor_can_say_they_paid(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    add_cost(kai, festival_id, people["Kai"].id, amount="30,00")
    share_id = _open_share(factory, people["Anna"].id)
    assert kai.post(
        f"/festivals/{festival_id}/expenses/shares/{share_id}/paid",
        data={"csrf_token": csrf(kai)},
    ).status_code == 403


def test_a_settled_share_is_greyed_out_in_the_list(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    add_cost(kai, festival_id, people["Kai"].id, amount="30,00")
    share_id = _open_share(factory, people["Anna"].id)
    anna.post(f"/festivals/{festival_id}/expenses/shares/{share_id}/paid",
              data={"csrf_token": csrf(anna)})
    kai.post(f"/festivals/{festival_id}/expenses/shares/{share_id}/received",
             data={"csrf_token": csrf(kai)})

    page = kai.get(f"/festivals/{festival_id}/expenses").text
    assert "share--confirmed" in page


# --- notifications and the dashboard ---------------------------------------


def test_adding_a_cost_notifies_the_others_but_not_the_payer(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    add_cost(kai, festival_id, people["Kai"].id, amount="30,00", description="Firewood")

    assert "Firewood" in anna.get(f"/festivals/{festival_id}").text
    assert "Firewood" in owner.get(f"/festivals/{festival_id}").text
    assert "Firewood" not in kai.get(f"/festivals/{festival_id}").text


def test_paying_notifies_the_person_who_is_owed(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    add_cost(kai, festival_id, people["Kai"].id, amount="30,00")
    share_id = _open_share(factory, people["Anna"].id)
    anna.post(f"/festivals/{festival_id}/expenses/shares/{share_id}/paid",
              data={"csrf_token": csrf(anna)})

    dashboard = kai.get(f"/festivals/{festival_id}").text
    assert "paid you" in dashboard


def test_the_expenses_icon_carries_a_dot_until_opened(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    add_cost(kai, festival_id, people["Kai"].id)

    before = anna.get(f"/festivals/{festival_id}").text
    assert f'href="/festivals/{festival_id}/expenses"' in before
    anna.get(f"/festivals/{festival_id}/expenses")
    after = anna.get(f"/festivals/{festival_id}").text
    assert after.count("class=\"dot\"") < before.count("class=\"dot\"")


def test_the_balance_shows_both_directions(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    add_cost(kai, festival_id, people["Kai"].id, amount="30,00", description="Beer")
    add_cost(anna, festival_id, people["Anna"].id, amount="15,00", description="Ice")

    page = kai.get(f"/festivals/{festival_id}/expenses").text
    assert "You owe" in page       # Kai owes Anna for the ice
    assert "Owed to you" in page   # and is owed for the beer


# --- deleting ---------------------------------------------------------------


def test_the_payer_can_delete_their_own_cost(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    add_cost(kai, festival_id, people["Kai"].id)
    with factory() as db:
        expense_id = db.scalars(select(Expense)).one().id
    assert kai.post(
        f"/festivals/{festival_id}/expenses/{expense_id}/delete",
        data={"csrf_token": csrf(kai)},
    ).status_code == 303
    with factory() as db:
        assert db.scalars(select(Expense)).all() == []
        assert db.scalars(select(ExpenseShare)).all() == []  # shares cascade


def test_a_bystander_cannot_delete_somebody_elses_cost(trip, factory) -> None:
    festival_id, owner, kai, anna = trip
    people = who(factory, festival_id)
    add_cost(kai, festival_id, people["Kai"].id)
    with factory() as db:
        expense_id = db.scalars(select(Expense)).one().id
    assert anna.post(
        f"/festivals/{festival_id}/expenses/{expense_id}/delete",
        data={"csrf_token": csrf(anna)},
    ).status_code == 403


def test_a_stranger_sees_nothing_of_the_expenses(trip, factory, settings) -> None:
    """404, not 403: the existence of the festival must not be confirmed."""
    festival_id, owner, kai, anna = trip
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
    assert stranger.get(f"/festivals/{festival_id}/expenses").status_code == 404
