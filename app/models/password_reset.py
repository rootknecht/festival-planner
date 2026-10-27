"""Password reset tokens."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.user import utcnow


class PasswordReset(Base):
    __tablename__ = "password_reset"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
