"""Regression tests for plan 001, repair round 1, L1: an email that is longer than any valid
one after strip and lowercase is throttled under a hashed key that always fits the table."""

import hashlib

from fragancia_api.modules.identity.application.commands.log_in import LoginResult
from fragancia_api.shared.kernel import DomainError, Err, Result
from tests.unit.identity.conftest import PASSWORD, POLICY, Identity

IP = "203.0.113.7"
IP_KEY = f"ip:{IP}"
THROTTLE_KEY_COLUMN_LENGTH = 330  # identity.login_throttle.key String(330)

# Lowercasing "İ" (U+0130) gives two characters, so 320 of them (the request bound) become 640.
LONG_EMAIL = "İ" * 320


def _hashed_key(email: str) -> str:
    return f"email-sha256:{hashlib.sha256(email.strip().lower().encode()).hexdigest()}"


async def _attempt(identity: Identity, email: str) -> Result[LoginResult, DomainError]:
    return await identity.log_in.execute(email, PASSWORD, ip=IP, user_agent="tests")


async def test_an_email_longer_than_254_after_lowercasing_is_counted_under_a_hashed_key(
    identity: Identity,
) -> None:
    identity.add_user()

    result = await _attempt(identity, LONG_EMAIL)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_INVALID_CREDENTIALS"
    key = _hashed_key(LONG_EMAIL)
    assert identity.throttle.counts[key][0] == 1  # the attempt is counted
    assert len(key) < THROTTLE_KEY_COLUMN_LENGTH
    assert not any(k.startswith("email:") for k in identity.throttle.counts)


async def test_an_over_long_email_is_throttled_like_any_other(identity: Identity) -> None:
    for _ in range(POLICY.email_max_attempts):
        await _attempt(identity, LONG_EMAIL)

    blocked = await _attempt(identity, LONG_EMAIL)

    assert isinstance(blocked, Err)
    assert blocked.error.code == "IDENTITY_TOO_MANY_ATTEMPTS"


async def test_an_over_long_email_checks_the_dummy_hash(identity: Identity) -> None:
    await _attempt(identity, LONG_EMAIL)

    assert identity.hasher.verified_hashes == [identity.hasher.dummy_hash]


async def test_an_email_of_exactly_254_characters_keeps_the_plain_key(identity: Identity) -> None:
    email = "a" * 241 + "@example.test"

    await _attempt(identity, email)

    assert len(email) == 254
    assert list(identity.throttle.counts) == [f"email:{email}", IP_KEY]


async def test_the_hashed_key_ignores_casing_and_padding(identity: Identity) -> None:
    await _attempt(identity, LONG_EMAIL)
    await _attempt(identity, f"  {LONG_EMAIL}  ")

    assert identity.throttle.counts[_hashed_key(LONG_EMAIL)][0] == 2
