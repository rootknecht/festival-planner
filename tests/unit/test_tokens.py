import time

import pytest

from app.services.tokens import (
    TokenExpired,
    TokenInvalid,
    generate_token,
    hash_token,
    verify_token,
)


def test_generated_tokens_are_long_and_unique() -> None:
    first = generate_token()
    second = generate_token()
    assert first != second
    assert len(first) >= 32


def test_hash_is_stable_and_hides_the_token() -> None:
    token = generate_token()
    assert hash_token(token) == hash_token(token)
    assert token not in hash_token(token)


def test_verify_accepts_a_matching_unexpired_token() -> None:
    token = generate_token()
    verify_token(token, hash_token(token), expires_at=time.time() + 60)


def test_verify_rejects_a_wrong_token() -> None:
    with pytest.raises(TokenInvalid):
        verify_token(generate_token(), hash_token(generate_token()), expires_at=time.time() + 60)


def test_verify_rejects_an_expired_token() -> None:
    token = generate_token()
    with pytest.raises(TokenExpired):
        verify_token(token, hash_token(token), expires_at=time.time() - 1)
