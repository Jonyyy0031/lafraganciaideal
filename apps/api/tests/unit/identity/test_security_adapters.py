import re

from fragancia_api.modules.identity.infrastructure.argon2_password_hasher import (
    Argon2PasswordHasher,
)
from fragancia_api.modules.identity.infrastructure.secure_session_tokens import SecureSessionTokens

hasher = Argon2PasswordHasher()


def test_a_password_verifies_against_its_own_argon2id_hash() -> None:
    password_hash = hasher.hash("correct horse battery")

    assert password_hash.startswith("$argon2id$")
    assert hasher.verify(password_hash, "correct horse battery")


def test_a_different_password_does_not_verify_and_does_not_raise() -> None:
    assert not hasher.verify(hasher.hash("correct horse battery"), "wrong horse battery")


def test_the_same_password_hashes_differently_each_time() -> None:
    assert hasher.hash("correct horse battery") != hasher.hash("correct horse battery")


def test_an_unreadable_hash_does_not_verify_and_does_not_raise() -> None:
    assert not hasher.verify("not-a-hash", "correct horse battery")
    assert not hasher.verify("", "correct horse battery")


def test_the_dummy_hash_is_a_real_hash_that_matches_nothing_typed() -> None:
    assert hasher.dummy_hash.startswith("$argon2id$")
    assert not hasher.verify(hasher.dummy_hash, "correct horse battery")


def test_tokens_are_url_safe_random_and_unique() -> None:
    tokens = SecureSessionTokens()

    issued = {tokens.new() for _ in range(50)}

    assert len(issued) == 50
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{43}", token) for token in issued)  # 256 bits


def test_the_digest_is_a_stable_sha256_hex_that_differs_from_the_token() -> None:
    tokens = SecureSessionTokens()
    token = tokens.new()

    digest = tokens.digest(token)

    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest == tokens.digest(token)
    assert digest != token
    assert digest != tokens.digest(tokens.new())
    # SHA-256 of "abc", a published test vector
    assert tokens.digest("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
