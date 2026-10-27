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

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class Message:
    to: str
    subject: str
    body: str


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
        email["From"] = self.settings.smtp_from
        email["To"] = message.to
        email["Subject"] = message.subject
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
