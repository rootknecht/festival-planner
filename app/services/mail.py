"""Outbound mail.

The application must stay fully usable when no SMTP relay is configured: in
that case mail is captured instead of sent, and the UI offers copy-paste links.
Losing an invitation silently is worse than admitting there is no mailer.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class Message:
    to: str
    subject: str
    body: str
    #: Per-message sender identity. Empty means "use the instance default":
    #: a festival can send under its own domain without every caller having
    #: to know which one that is.
    from_: str = ""
    reply_to: str = ""


class Mailer:
    """Interface shared by the real sender and the capture sink."""

    def send(self, message: Message) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    @property
    def enabled(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class CaptureMailer(Mailer):
    """Keeps messages in memory. Used in development and in every test."""

    sent: list[Message] = field(default_factory=list)

    def send(self, message: Message) -> bool:
        self.sent.append(message)
        logger.info("mail captured (no SMTP configured): to=%s subject=%s",
                    message.to, message.subject)
        return False

    @property
    def enabled(self) -> bool:
        return False

    def clear(self) -> None:
        self.sent.clear()


@dataclass
class SmtpMailer(Mailer):
    settings: Settings

    def send(self, message: Message) -> bool:
        email = EmailMessage()
        email["From"] = message.from_ or self.settings.smtp_from
        email["To"] = message.to
        email["Subject"] = message.subject
        # People DO reply to these mails — "when do we leave?" to an invite is
        # the most natural thing in the world. Without this it lands nowhere.
        reply_to = message.reply_to or self.settings.smtp_reply_to
        if reply_to:
            email["Reply-To"] = reply_to
        # RFC 5322 requires a Date, and nothing upstream adds one: a receiving provider's
        # amavis flagged a delivered invitation with "BAD HEADER SECTION,
        # Missing required header field: Date". It arrives either way, but a
        # malformed header section is free spam score at the receivers that
        # matter. Message-ID is not required but is what lets a mail client
        # thread a reply and a receiver suppress a duplicate; it is derived
        # from the sender's own domain so it never leaks the host name.
        email["Date"] = formatdate(localtime=True)
        sender = parseaddr(email["From"])[1]
        domain = sender.rpartition("@")[2] or None
        email["Message-ID"] = make_msgid(domain=domain)
        email.set_content(message.body)

        context = ssl.create_default_context()
        try:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=20) as s:
                if self.settings.smtp_starttls:
                    s.starttls(context=context)
                if self.settings.smtp_user:
                    s.login(self.settings.smtp_user, self.settings.smtp_password)
                s.send_message(email)
            return True
        except (OSError, smtplib.SMTPException) as exc:
            # Never raise into a request: the caller falls back to showing the
            # link, which keeps the invite usable.
            logger.warning("sending mail to %s failed: %s", message.to, exc)
            return False

    @property
    def enabled(self) -> bool:
        return True


_capture = CaptureMailer()


def get_mailer(settings: Settings | None = None) -> Mailer:
    settings = settings or get_settings()
    if settings.mail_enabled:
        return SmtpMailer(settings)
    return _capture


def capture_sink() -> CaptureMailer:
    """The process-wide capture sink, for tests and the dev UI."""
    return _capture
