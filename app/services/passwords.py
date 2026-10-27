"""Password hashing.

argon2id with deliberately conservative parameters. The cost is checked on
every login through ``needs_rehash`` so raising it later upgrades existing
accounts transparently.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

#: ~64 MiB memory, 3 passes, 4 lanes — comfortable on a small container while
#: making offline cracking of a leaked database expensive.
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)

MIN_PASSWORD_LENGTH = 10


class PasswordTooShort(ValueError):
    """Raised when a chosen password is below the minimum length."""


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordTooShort(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters long"
        )


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Return True only for a genuine match; never raise on malformed input."""
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True
