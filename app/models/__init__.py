"""Model package.

Importing this package must register every table on ``Base.metadata`` so that
Alembic autogeneration sees the full schema.
"""

from __future__ import annotations

from app.models.password_reset import PasswordReset
from app.models.user import LoginAttempt, User, UserSession

__all__ = ["LoginAttempt", "PasswordReset", "User", "UserSession"]
