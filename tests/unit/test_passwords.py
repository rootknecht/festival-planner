from app.services.passwords import hash_password, needs_rehash, verify_password


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
