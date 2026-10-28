import pytest

from app.config import Settings


def test_development_defaults_are_usable() -> None:
    settings = Settings(_env_file=None)
    assert settings.environment == "development"
    assert settings.secret_key  # a throwaway key is generated for development
    assert settings.database_url.startswith("sqlite")


def test_production_requires_an_explicit_secret_key() -> None:
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(_env_file=None, environment="production")


def test_production_accepts_an_explicit_secret_key() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        secret_key="x" * 32,
        base_url="https://example.org",
    )
    assert settings.is_production is True
    assert settings.secret_key == "x" * 32


def test_a_reply_to_is_set_on_outgoing_mail(monkeypatch) -> None:
    """The From address can be a noreply on a domain with no mailbox, so a
    reply has to be routed somewhere a human reads."""
    import smtplib

    from app.config import Settings
    from app.services.mail import Message, SmtpMailer

    captured: dict = {}

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

        def send_message(self, email):
            captured["from"] = email["From"]
            captured["reply_to"] = email["Reply-To"]

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    settings = Settings(
        secret_key="x" * 32,
        smtp_host="relay.example.org",
        smtp_from="noreply@example.org",
        smtp_reply_to="human@example.net",
    )
    assert SmtpMailer(settings).send(Message("a@b.c", "s", "b")) is True
    assert captured["from"] == "noreply@example.org"
    assert captured["reply_to"] == "human@example.net"


def test_without_a_reply_to_no_header_is_added(monkeypatch) -> None:
    import smtplib

    from app.config import Settings
    from app.services.mail import Message, SmtpMailer

    captured: dict = {}

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

        def send_message(self, email):
            captured["reply_to"] = email["Reply-To"]

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    settings = Settings(
        secret_key="x" * 32, smtp_host="relay.example.org", smtp_from="a@b.c"
    )
    SmtpMailer(settings).send(Message("a@b.c", "s", "b"))
    assert captured["reply_to"] is None
