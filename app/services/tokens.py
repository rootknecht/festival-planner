"""Single-use, time-limited tokens for invites, password resets and sessions.

Only the SHA-256 hash of a token is ever stored, so a database leak does not
hand out working invite or reset links. Comparison is constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

TOKEN_BYTES = 32


class TokenInvalid(Exception):
    """The presented token does not match the stored hash."""


class TokenExpired(Exception):
    """The presented token matched but is past its expiry."""


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, stored_hash: str, expires_at: float) -> None:
    """Raise TokenInvalid or TokenExpired; return None when the token is good."""
    if not hmac.compare_digest(hash_token(token), stored_hash):
        raise TokenInvalid("token does not match")
    if expires_at <= time.time():
        raise TokenExpired("token has expired")
