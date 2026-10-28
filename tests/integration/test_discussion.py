"""M6: polls, chat, forum, mentions, markup safety and the live channel."""

import datetime as dt
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base, reset_engine_for_tests
from app.main import create_app
from app.models.discussion import Message, Poll, PollOption, PollVote, Post, Topic
from app.models.user import User
from app.security.csrf import CSRF_COOKIE
from app.services import notifications
from app.services.mail import capture_sink
from app.services.markup import find_mentions, render
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
    created = owner.post("/festivals", data={"name": "Wacken", "csrf_token": csrf(owner)})
    festival_id = int(created.headers["location"].rsplit("/", 1)[1])
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


# --------------------------------------------------------------- markup ----


def test_markup_renders_only_simple_formatting() -> None:
    html = render("**bold** and *italic* and `code`")
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert "<code>code</code>" in html


class TagCollector(HTMLParser):
    """Collects the tags and attributes that actually reach the browser.

    Asserting on substrings is misleading here: escaped text like
    &lt;img onerror=...&gt; contains "onerror" but is inert. Only live tags
    and attributes matter.
    """

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attrs: list[str] = []
        self.values: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for name, value in attrs:
            self.attrs.append(name.lower())
            self.values.append((value or "").lower())


@pytest.mark.parametrize(
    "attack",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        '<a href="javascript:alert(1)">click</a>',
        "<iframe src='https://evil.example'></iframe>",
        "<style>body{display:none}</style>",
        "<div onclick='alert(1)'>x</div>",
        "[click](javascript:alert(1))",
        "<a href=\"data:text/html,<script>alert(1)</script>\">x</a>",
    ],
)
def test_markup_strips_every_attack_vector(attack: str) -> None:
    parser = TagCollector()
    parser.feed(render(attack))

    forbidden = {"script", "iframe", "style", "img", "object", "embed", "svg", "div"}
    assert forbidden.isdisjoint(parser.tags), f"dangerous tag survived: {parser.tags}"
    assert not [name for name in parser.attrs if name.startswith("on")]
    assert not [value for value in parser.values if value.startswith(("javascript:", "data:"))]


def test_links_get_safe_attributes() -> None:
    html = render("see https://example.org for details")
    assert 'rel="noopener noreferrer nofollow"' in html
    assert 'target="_blank"' in html


def test_mentions_find_names_and_everyone() -> None:
    names = ["Anna", "Anna Maria", "Kai"]
    found, everyone = find_mentions("hey @Anna Maria and @Kai, @everyone look", names)
    assert "Anna Maria" in found
    assert "Kai" in found
    assert everyone is True


def test_an_unknown_mention_matches_nobody() -> None:
    found, everyone = find_mentions("@Nobody are you there", ["Anna", "Kai"])
    assert found == set()
    assert everyone is False


# ---------------------------------------------------------------- polls ----


def create_poll(client, festival_id, question="Arrival?", options=("Friday", "Saturday"),
                mode="single", closes_on=""):
    return client.post(
        f"/festivals/{festival_id}/polls",
        data={
            "question": question,
            # One form field per option now, so a list value.
            "options": list(options) if not isinstance(options, str) else [options],
            "mode": mode,
            "closes_on": closes_on,
            "csrf_token": csrf(client),
        },
    )


def poll_id(factory) -> int:
    with factory() as db:
        return db.scalars(select(Poll)).first().id


def option_ids(factory, pid) -> list[int]:
    with factory() as db:
        poll = db.get(Poll, pid)
        return [option.id for option in poll.options]


def test_a_poll_needs_a_question_and_two_options(festival) -> None:
    festival_id, owner, _ = festival
    assert create_poll(owner, festival_id, question="  ").status_code == 400
    assert create_poll(owner, festival_id, options=["only one"]).status_code == 400


def test_creating_a_poll_asks_everyone_except_the_author(festival, factory) -> None:
    festival_id, owner, friend = festival
    create_poll(owner, festival_id)
    with factory() as db:
        owner_user = db.scalar(select(User).where(User.email == "owner@example.org"))
        friend_user = db.scalar(select(User).where(User.email == "friend@example.org"))
        assert notifications.open_for(db, owner_user.id, festival_id) == []
        assert [b.message_key for b in notifications.open_for(db, friend_user.id, festival_id)] == [
            "poll.open"
        ]


def test_voting_clears_the_block_and_counts_once(festival, factory) -> None:
    festival_id, owner, friend = festival
    create_poll(owner, festival_id)
    pid = poll_id(factory)
    options = option_ids(factory, pid)

    response = friend.post(
        f"/festivals/{festival_id}/polls/{pid}/vote",
        data={"option_id": options[0], "csrf_token": csrf(friend)},
    )
    assert response.status_code == 303
    with factory() as db:
        friend_user = db.scalar(select(User).where(User.email == "friend@example.org"))
        assert notifications.open_for(db, friend_user.id, festival_id) == []
        assert len(db.scalars(select(PollVote)).all()) == 1

    # changing the answer replaces the vote instead of adding one
    friend.post(
        f"/festivals/{festival_id}/polls/{pid}/vote",
        data={"option_id": options[1], "csrf_token": csrf(friend)},
    )
    with factory() as db:
        votes = db.scalars(select(PollVote)).all()
    assert len(votes) == 1
    assert votes[0].option_id == options[1]


def test_single_choice_keeps_only_one_option(festival, factory) -> None:
    festival_id, owner, friend = festival
    create_poll(owner, festival_id, options=["A", "B", "C"], mode="single")
    pid = poll_id(factory)
    options = option_ids(factory, pid)
    friend.post(
        f"/festivals/{festival_id}/polls/{pid}/vote",
        data={"option_id": [options[0], options[1]], "csrf_token": csrf(friend)},
    )
    with factory() as db:
        assert len(db.scalars(select(PollVote)).all()) == 1


def test_multi_choice_keeps_every_option(festival, factory) -> None:
    festival_id, owner, friend = festival
    create_poll(owner, festival_id, options=["A", "B", "C"], mode="multi")
    pid = poll_id(factory)
    options = option_ids(factory, pid)
    friend.post(
        f"/festivals/{festival_id}/polls/{pid}/vote",
        data={"option_id": [options[0], options[2]], "csrf_token": csrf(friend)},
    )
    with factory() as db:
        assert len(db.scalars(select(PollVote)).all()) == 2


def test_a_closed_poll_refuses_votes_and_clears_blocks(festival, factory) -> None:
    festival_id, owner, friend = festival
    create_poll(owner, festival_id)
    pid = poll_id(factory)
    options = option_ids(factory, pid)

    assert owner.post(
        f"/festivals/{festival_id}/polls/{pid}/close", data={"csrf_token": csrf(owner)}
    ).status_code == 303

    assert friend.post(
        f"/festivals/{festival_id}/polls/{pid}/vote",
        data={"option_id": options[0], "csrf_token": csrf(friend)},
    ).status_code == 409

    with factory() as db:
        friend_user = db.scalar(select(User).where(User.email == "friend@example.org"))
        assert notifications.open_for(db, friend_user.id, festival_id) == []


def test_a_poll_past_its_closing_date_is_closed(festival, factory) -> None:
    festival_id, owner, friend = festival
    create_poll(owner, festival_id, closes_on="2020-01-01")
    pid = poll_id(factory)
    options = option_ids(factory, pid)
    assert friend.post(
        f"/festivals/{festival_id}/polls/{pid}/vote",
        data={"option_id": options[0], "csrf_token": csrf(friend)},
    ).status_code == 409


def test_only_the_creator_or_an_admin_closes_a_poll(festival, factory) -> None:
    festival_id, owner, friend = festival
    create_poll(friend, festival_id)  # the friend asks
    pid = poll_id(factory)
    # the owner is an admin, so they may close it too
    assert owner.post(
        f"/festivals/{festival_id}/polls/{pid}/close", data={"csrf_token": csrf(owner)}
    ).status_code == 303


def test_a_plain_member_cannot_close_a_poll_they_did_not_create(festival, factory) -> None:
    festival_id, owner, friend = festival
    create_poll(owner, festival_id)
    pid = poll_id(factory)
    assert friend.post(
        f"/festivals/{festival_id}/polls/{pid}/close", data={"csrf_token": csrf(friend)}
    ).status_code == 403


def test_the_page_shows_who_has_not_voted(festival, factory) -> None:
    festival_id, owner, friend = festival
    create_poll(owner, festival_id)
    page = owner.get(f"/festivals/{festival_id}/polls").text
    assert "Still missing:" in page
    assert "Friend" in page


# ----------------------------------------------------------------- chat ----


def say(client, festival_id, text):
    return client.post(
        f"/festivals/{festival_id}/chat", data={"body": text, "csrf_token": csrf(client)}
    )


def test_a_message_appears_in_the_room(festival) -> None:
    festival_id, owner, friend = festival
    assert say(owner, festival_id, "beer is on me").status_code == 303
    assert "beer is on me" in friend.get(f"/festivals/{festival_id}/chat").text


def test_a_mention_notifies_only_that_person(festival, factory) -> None:
    festival_id, owner, friend = festival
    say(owner, festival_id, "hey @Friend can you drive?")
    with factory() as db:
        friend_user = db.scalar(select(User).where(User.email == "friend@example.org"))
        owner_user = db.scalar(select(User).where(User.email == "owner@example.org"))
        assert [b.message_key for b in notifications.open_for(db, friend_user.id, festival_id)] == [
            "mention.chat"
        ]
        assert notifications.open_for(db, owner_user.id, festival_id) == []


def test_everyone_notifies_all_other_participants(festival, factory) -> None:
    festival_id, owner, friend = festival
    say(owner, festival_id, "@everyone tickets are booked")
    with factory() as db:
        friend_user = db.scalar(select(User).where(User.email == "friend@example.org"))
        blocks = notifications.open_for(db, friend_user.id, festival_id)
    assert [b.message_key for b in blocks] == ["mention.chat.everyone"]


def test_opening_the_room_clears_chat_blocks(festival, factory) -> None:
    festival_id, owner, friend = festival
    say(owner, festival_id, "@Friend hello")
    friend.get(f"/festivals/{festival_id}/chat")
    with factory() as db:
        friend_user = db.scalar(select(User).where(User.email == "friend@example.org"))
        assert notifications.badges(db, friend_user.id, festival_id).get("chat") is None


def test_an_author_may_delete_their_own_message(festival, factory) -> None:
    festival_id, owner, _ = festival
    say(owner, festival_id, "oops")
    with factory() as db:
        message_id = db.scalars(select(Message)).first().id
    assert owner.post(
        f"/festivals/{festival_id}/chat/{message_id}/delete", data={"csrf_token": csrf(owner)}
    ).status_code == 303
    assert "oops" not in owner.get(f"/festivals/{festival_id}/chat").text


def test_a_member_cannot_delete_someone_elses_message(festival, factory) -> None:
    festival_id, owner, friend = festival
    say(owner, festival_id, "mine")
    with factory() as db:
        message_id = db.scalars(select(Message)).first().id
    assert friend.post(
        f"/festivals/{festival_id}/chat/{message_id}/delete", data={"csrf_token": csrf(friend)}
    ).status_code == 403


def test_an_admin_may_delete_any_message(festival, factory) -> None:
    festival_id, owner, friend = festival
    say(friend, festival_id, "something regrettable")
    with factory() as db:
        message_id = db.scalars(select(Message)).first().id
    assert owner.post(
        f"/festivals/{festival_id}/chat/{message_id}/delete", data={"csrf_token": csrf(owner)}
    ).status_code == 303


def test_the_edit_window_closes_after_fifteen_minutes(festival, factory) -> None:
    festival_id, owner, _ = festival
    say(owner, festival_id, "too late to fix")
    with factory() as db:
        message = db.scalars(select(Message)).first()
        message.created_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=16)
        db.commit()
        message_id = message.id
    response = owner.post(
        f"/festivals/{festival_id}/chat/{message_id}/edit",
        data={"body": "fixed", "csrf_token": csrf(owner)},
    )
    assert response.status_code == 403


def test_a_message_with_html_is_rendered_harmlessly(festival) -> None:
    festival_id, owner, _ = festival
    say(owner, festival_id, "<script>alert('x')</script> **but bold works**")
    page = owner.get(f"/festivals/{festival_id}/chat").text
    assert "<script>alert" not in page
    assert "<strong>but bold works</strong>" in page


# ---------------------------------------------------------------- forum ----


def test_a_topic_and_a_reply(festival, factory) -> None:
    festival_id, owner, friend = festival
    created = owner.post(
        f"/festivals/{festival_id}/forum",
        data={"title": "Tickets", "body": "who still needs one?", "csrf_token": csrf(owner)},
    )
    assert created.status_code == 303
    topic_url = created.headers["location"]
    topic_id = int(topic_url.rsplit("/", 1)[1])

    reply = friend.post(
        f"/festivals/{festival_id}/forum/{topic_id}/posts",
        data={"body": "i do, @Owner", "csrf_token": csrf(friend)},
    )
    assert reply.status_code == 303

    # Check the notification BEFORE the owner opens the thread: opening it is
    # what clears forum mentions.
    with factory() as db:
        owner_user = db.scalar(select(User).where(User.email == "owner@example.org"))
        assert [b.message_key for b in notifications.open_for(db, owner_user.id, festival_id)] == [
            "mention.forum"
        ]

    page = owner.get(topic_url).text
    assert "who still needs one?" in page
    assert "i do," in page

    with factory() as db:
        owner_user = db.scalar(select(User).where(User.email == "owner@example.org"))
        assert notifications.open_for(db, owner_user.id, festival_id) == []


def test_topics_are_ordered_by_last_activity(festival, factory) -> None:
    festival_id, owner, friend = festival
    owner.post(
        f"/festivals/{festival_id}/forum",
        data={"title": "First", "body": "a", "csrf_token": csrf(owner)},
    )
    second = owner.post(
        f"/festivals/{festival_id}/forum",
        data={"title": "Second", "body": "b", "csrf_token": csrf(owner)},
    )
    first_id = 1
    friend.post(
        f"/festivals/{festival_id}/forum/{first_id}/posts",
        data={"body": "bumping this", "csrf_token": csrf(friend)},
    )
    page = owner.get(f"/festivals/{festival_id}/forum").text
    assert page.index("First") < page.index("Second")
    assert second.status_code == 303


def test_only_an_admin_deletes_a_topic(festival, factory) -> None:
    festival_id, owner, friend = festival
    owner.post(
        f"/festivals/{festival_id}/forum",
        data={"title": "Doomed", "body": "x", "csrf_token": csrf(owner)},
    )
    with factory() as db:
        topic_id = db.scalars(select(Topic)).first().id
    assert friend.post(
        f"/festivals/{festival_id}/forum/{topic_id}/delete", data={"csrf_token": csrf(friend)}
    ).status_code == 403
    assert owner.post(
        f"/festivals/{festival_id}/forum/{topic_id}/delete", data={"csrf_token": csrf(owner)}
    ).status_code == 303
    with factory() as db:
        assert db.scalars(select(Post)).all() == []  # cascade removed the posts


# ----------------------------------------------------------- separation ----


def test_a_stranger_reaches_none_of_the_new_tools(factory, settings, festival) -> None:
    festival_id, owner, _ = festival
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
    for path in ("polls", "chat", "forum"):
        assert stranger.get(f"/festivals/{festival_id}/{path}").status_code == 404
    assert say(stranger, festival_id, "hello?").status_code == 404


def test_an_archived_festival_is_read_only_everywhere(festival, factory) -> None:
    festival_id, owner, _ = festival
    create_poll(owner, festival_id)
    owner.post(f"/festivals/{festival_id}/archive", data={"csrf_token": csrf(owner)})

    for path in ("polls", "chat", "forum"):
        assert owner.get(f"/festivals/{festival_id}/{path}").status_code == 200
    assert say(owner, festival_id, "one more thing").status_code == 409
    assert create_poll(owner, festival_id).status_code == 409
    assert owner.post(
        f"/festivals/{festival_id}/forum",
        data={"title": "Late", "body": "x", "csrf_token": csrf(owner)},
    ).status_code == 409


# ------------------------------------------------------------ websocket ----


def test_the_live_socket_accepts_a_participant(festival, settings) -> None:
    festival_id, owner, _ = festival
    with owner.websocket_connect(f"/festivals/{festival_id}/ws") as socket:
        assert socket is not None


def test_the_live_socket_refuses_an_anonymous_visitor(festival, settings) -> None:
    from starlette.websockets import WebSocketDisconnect

    festival_id, _, _ = festival
    anonymous = TestClient(create_app(settings))
    with pytest.raises(WebSocketDisconnect):
        with anonymous.websocket_connect(f"/festivals/{festival_id}/ws"):
            pass


def test_the_live_socket_refuses_a_non_participant(factory, settings, festival) -> None:
    from starlette.websockets import WebSocketDisconnect

    festival_id, _, _ = festival
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
    with pytest.raises(WebSocketDisconnect):
        with stranger.websocket_connect(f"/festivals/{festival_id}/ws"):
            pass


def test_a_posted_message_reaches_an_open_socket(festival) -> None:
    festival_id, owner, friend = festival
    with friend.websocket_connect(f"/festivals/{festival_id}/ws") as socket:
        say(owner, festival_id, "anybody there?")
        event = socket.receive_json()
    assert event["type"] == "message"
    assert event["author"] == "Owner"
    assert "anybody there?" in event["html"]


def test_a_socket_only_joins_its_own_festivals_channel(festival, settings) -> None:
    """Channels are per festival: a socket open on one is absent from the other."""
    from app.realtime.hub import hub

    festival_id, owner, _ = festival
    other = owner.post("/festivals", data={"name": "Hellfest", "csrf_token": csrf(owner)})
    other_id = int(other.headers["location"].rsplit("/", 1)[1])

    with owner.websocket_connect(f"/festivals/{other_id}/ws"):
        assert hub.connection_count(other_id) == 1
        assert hub.connection_count(festival_id) == 0


def test_poll_results_do_not_name_who_voted_for_what(festival, factory) -> None:
    """Requested: results show counts only.

    Who has NOT voted yet is still listed — that is the number worth chasing,
    and it says nothing about anybody's opinion.
    """
    festival_id, owner, friend = festival
    create_poll(owner, festival_id)
    pid = poll_id(factory)
    options = option_ids(factory, pid)
    owner.post(
        f"/festivals/{festival_id}/polls/{pid}/vote",
        data={"option_id": options[0], "csrf_token": csrf(owner)},
    )

    page = owner.get(f"/festivals/{festival_id}/polls").text
    before_pending = page.split("Still missing")[0]
    assert "Owner" not in before_pending      # no voter names beside the bars
    assert "Still missing" in page            # the pending list survives
    assert "Friend" in page.split("Still missing")[1]


# --- admin poll management --------------------------------------------------


def test_an_admin_can_reopen_a_closed_poll_without_losing_votes(festival, factory) -> None:
    """Closing is a judgement call, often made too early. Reopening must not
    be a reset: silently discarding answers people already gave would be a
    far worse surprise than a poll that stayed shut."""
    festival_id, owner, friend = festival
    create_poll(owner, festival_id)
    pid = poll_id(factory)
    options = option_ids(factory, pid)
    friend.post(f"/festivals/{festival_id}/polls/{pid}/vote",
                data={"option_id": options[0], "csrf_token": csrf(friend)})
    owner.post(f"/festivals/{festival_id}/polls/{pid}/close",
               data={"csrf_token": csrf(owner)})

    response = owner.post(f"/festivals/{festival_id}/polls/{pid}/reopen",
                          data={"csrf_token": csrf(owner)})
    assert response.status_code == 303
    with factory() as db:
        assert db.get(Poll, pid).closed_at is None
        assert len(db.scalars(select(PollVote)).all()) == 1


def test_reopening_asks_only_the_people_who_never_voted(festival, factory) -> None:
    """Re-notifying someone who already answered is how people learn to
    ignore the dot. The admin doing the reopening is never notified either —
    they already know, they just pressed the button."""
    festival_id, owner, friend = festival
    create_poll(owner, festival_id)
    pid = poll_id(factory)
    options = option_ids(factory, pid)
    # The OWNER votes this time, so the only outstanding voter is the friend.
    owner.post(f"/festivals/{festival_id}/polls/{pid}/vote",
               data={"option_id": options[0], "csrf_token": csrf(owner)})
    owner.post(f"/festivals/{festival_id}/polls/{pid}/close",
               data={"csrf_token": csrf(owner)})
    with factory() as db:
        friend_user = db.scalar(select(User).where(User.email == "friend@example.org"))
        # Closing cleared the block: the question was no longer answerable.
        assert notifications.open_for(db, friend_user.id, festival_id) == []

    owner.post(f"/festivals/{festival_id}/polls/{pid}/reopen",
               data={"csrf_token": csrf(owner)})
    with factory() as db:
        friend_user = db.scalar(select(User).where(User.email == "friend@example.org"))
        owner_user = db.scalar(select(User).where(User.email == "owner@example.org"))
        assert [b.message_key for b in notifications.open_for(db, friend_user.id, festival_id)] == [
            "poll.open"
        ]
        assert notifications.open_for(db, owner_user.id, festival_id) == []


def test_an_admin_can_delete_a_poll_and_its_votes(festival, factory) -> None:
    festival_id, owner, friend = festival
    create_poll(owner, festival_id)
    pid = poll_id(factory)
    options = option_ids(factory, pid)
    friend.post(f"/festivals/{festival_id}/polls/{pid}/vote",
                data={"option_id": options[0], "csrf_token": csrf(friend)})

    response = owner.post(f"/festivals/{festival_id}/polls/{pid}/delete",
                          data={"csrf_token": csrf(owner)})
    assert response.status_code == 303
    with factory() as db:
        assert db.get(Poll, pid) is None
        assert db.scalars(select(PollVote)).all() == []
        assert db.scalars(select(PollOption)).all() == []


def test_deleting_a_poll_clears_the_blocks_pointing_at_it(festival, factory) -> None:
    """A notification linking to a poll that no longer exists is a dead link
    on everybody's dashboard."""
    festival_id, owner, friend = festival
    create_poll(owner, festival_id)
    pid = poll_id(factory)
    with factory() as db:
        friend_user = db.scalar(select(User).where(User.email == "friend@example.org"))
        assert notifications.open_for(db, friend_user.id, festival_id) != []

    owner.post(f"/festivals/{festival_id}/polls/{pid}/delete",
               data={"csrf_token": csrf(owner)})
    with factory() as db:
        friend_user = db.scalar(select(User).where(User.email == "friend@example.org"))
        assert notifications.open_for(db, friend_user.id, festival_id) == []


def test_a_member_cannot_delete_a_poll_even_their_own(festival, factory) -> None:
    """Closing preserves the result; deleting destroys other people's answers.
    That is an admin decision, deliberately stricter than closing."""
    festival_id, owner, friend = festival
    create_poll(friend, festival_id)
    pid = poll_id(factory)

    refused = friend.post(f"/festivals/{festival_id}/polls/{pid}/delete",
                          data={"csrf_token": csrf(friend)})
    assert refused.status_code == 403
    with factory() as db:
        assert db.get(Poll, pid) is not None


def test_the_edit_panel_is_admin_only(festival, factory) -> None:
    festival_id, owner, friend = festival
    create_poll(owner, festival_id)
    assert "Edit polls" in owner.get(f"/festivals/{festival_id}/polls").text
    assert "Edit polls" not in friend.get(f"/festivals/{festival_id}/polls").text
