"""Application settings.

Every value is environment driven so the same code runs unchanged in
development and on a server. Nothing secret ever has a usable default in
production: a missing SECRET_KEY must stop the process, not silently weaken it.
"""

from __future__ import annotations

import secrets
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = "development"
    secret_key: str = ""
    database_url: str = "sqlite+pysqlite:///./data/festival.db"
    base_url: str = "http://localhost:8000"

    # Session and token lifetimes (seconds)
    session_lifetime: int = 60 * 60 * 24 * 30
    invite_lifetime: int = 60 * 60 * 24 * 7
    reset_lifetime: int = 60 * 60 * 2

    # Login throttling
    login_max_failures: int = 5
    login_lockout_seconds: int = 900

    # Outbound mail. Empty host means "no mail": the UI then offers
    # copy-paste invite links instead of silently losing invitations.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    #: Where replies should go. The From address may well be a noreply on a
    #: domain with no MX at all (authenticating a domain for sending does not
    #: give it a mailbox), in which case a reply would bounce into nothing.
    smtp_reply_to: str = ""
    smtp_starttls: bool = True

    # Nominatim geocoding (usage-policy compliant defaults)
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    nominatim_user_agent: str = "festival-planner (self-hosted)"
    nominatim_min_interval: float = 1.0

    # Web Push
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    # A PEM private key is multi-line and therefore cannot live in an env file
    # at all: both shell `source` and systemd's EnvironmentFile are line-based
    # and choke on "-----BEGIN PRIVATE KEY-----". Point at the file instead.
    vapid_private_key_file: str = ""
    vapid_subject: str = ""

    default_language: str = "en"
    default_theme: str = "dark"
    data_dir: str = Field(default="./data")

    @model_validator(mode="after")
    def _check_secrets(self) -> Settings:
        if not self.secret_key:
            if self.environment == "production":
                raise ValueError(
                    "SECRET_KEY must be set explicitly in production "
                    "(environment variable FP_SECRET_KEY)"
                )
            # Development and tests get a throwaway key per process.
            object.__setattr__(self, "secret_key", secrets.token_urlsafe(32))
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def vapid_private(self) -> str:
        """The private key as pywebpush wants it: a PEM path or a raw key."""
        return self.vapid_private_key_file or self.vapid_private_key

    @property
    def mail_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings, loading them on first use."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
