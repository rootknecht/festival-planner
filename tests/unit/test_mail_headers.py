"""The header section of outgoing mail must be well formed.

A delivered invitation came back from posteo's amavis with "BAD HEADER
SECTION, Missing required header field: Date". The mail arrived, which is
exactly why this needs a test rather than a watchful eye: a malformed header
section costs spam score at the receivers that matter and nothing in the
application notices.
"""

import datetime as dt
import email.utils
import smtplib

import pytest

from app.config import Settings
from app.services.mail import Message, SmtpMailer


@pytest.fixture()
def captured(monkeypatch) -> dict:
    """Send through a fake SMTP and hand back the composed message."""
    box: dict = {}

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            pass

        def send_message(self, email_message):
            box["email"] = email_message

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return box


def send(captured: dict, **message_fields):
    settings = Settings(
        _env_file=None,
        secret_key="x" * 32,
        smtp_host="relay.example.org",
        smtp_from="noreply@example.org",
    )
    assert SmtpMailer(settings).send(Message("a@b.c", "s", "b", **message_fields)) is True
    return captured["email"]


def test_every_mail_carries_a_date(captured) -> None:
    sent = send(captured)
    assert sent["Date"], "RFC 5322 requires a Date; amavis flags its absence"
    parsed = email.utils.parsedate_to_datetime(sent["Date"])
    assert abs((dt.datetime.now(dt.UTC) - parsed.astimezone(dt.UTC)).total_seconds()) < 300


def test_every_mail_carries_a_message_id_on_the_sending_domain(captured) -> None:
    sent = send(captured)
    message_id = sent["Message-ID"]
    assert message_id.startswith("<") and message_id.endswith(">")
    # Derived from the sender, so it never leaks the container's host name.
    assert message_id.endswith("@example.org>")


def test_the_message_id_follows_a_per_festival_sender(captured) -> None:
    sent = send(captured, from_="noreply@oiboyz.org")
    assert sent["From"] == "noreply@oiboyz.org"
    assert sent["Message-ID"].endswith("@oiboyz.org>")


def test_two_mails_do_not_share_a_message_id(captured) -> None:
    first = send(captured)["Message-ID"]
    second = send(captured)["Message-ID"]
    assert first != second
