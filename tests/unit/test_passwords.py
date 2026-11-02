import pytest

from app.services.passwords import (
    MIN_PASSWORD_LENGTH,
    PasswordTooShort,
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
)


def test_the_minimum_length_is_eight() -> None:
    # Pinned on purpose: the policy is a product decision, not an accident of
    # whatever the constant happens to say.
    assert MIN_PASSWORD_LENGTH == 8


def test_a_password_of_exactly_the_minimum_is_accepted() -> None:
    validate_password("a" * MIN_PASSWORD_LENGTH)


def test_one_character_short_is_refused() -> None:
    with pytest.raises(PasswordTooShort):
        validate_password("a" * (MIN_PASSWORD_LENGTH - 1))


def test_hash_is_argon2id_and_not_the_password() -> None:
    digest = hash_password("correct horse battery staple")
    assert digest.startswith("$argon2id$")
    assert "correct horse" not in digest


def test_verify_accepts_the_right_password() -> None:
    digest = hash_password("s3cret-passphrase")
    assert verify_password(digest, "s3cret-passphrase") is True


def test_verify_rejects_a_wrong_password() -> None:
    digest = hash_password("s3cret-passphrase")
    assert verify_password(digest, "s3cret-passphrasf") is False


def test_verify_rejects_garbage_without_raising() -> None:
    assert verify_password("not-a-hash", "anything") is False


def test_two_hashes_of_one_password_differ() -> None:
    assert hash_password("same") != hash_password("same")


def test_needs_rehash_is_false_for_a_fresh_hash() -> None:
    assert needs_rehash(hash_password("fresh")) is False
