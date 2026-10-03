from dataclasses import replace
from datetime import timedelta

from fragancia_api.modules.identity.application.commands.log_in import LoginResult
from fragancia_api.modules.identity.domain.user import Role
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result
from tests.unit.identity.conftest import PASSWORD, POLICY, Identity

OWNER = "owner@example.test"
IP = "203.0.113.7"
EMAIL_KEY = f"email:{OWNER}"
IP_KEY = f"ip:{IP}"


async def _attempt(
    identity: Identity, email: str = OWNER, password: str = PASSWORD, ip: str | None = IP
) -> Result[LoginResult, DomainError]:
    return await identity.log_in.execute(email, password, ip=ip, user_agent="tests")


async def test_signs_in_and_opens_a_session_stored_by_digest(identity: Identity) -> None:
    user = identity.add_user()

    result = await _attempt(identity)

    assert isinstance(result, Ok)
    login = result.value
    assert (login.token, login.user_id) == ("token-1", user.id)
    [session] = identity.sessions.by_id.values()
    assert session.token_hash == "digest:token-1"  # the token itself is never stored
    assert (session.user_id, session.ip, session.user_agent) == (user.id, IP, "tests")
    assert login.expires_at == session.expires_at == identity.clock.now() + POLICY.session_max_age
    assert session.last_seen_at == identity.clock.now()


async def test_the_email_is_trimmed_and_case_insensitive(identity: Identity) -> None:
    identity.add_user()

    signed_in = await _attempt(identity, email="  OWNER@Example.TEST ")
    failed = await _attempt(identity, email="  OWNER@Example.TEST ", password="not the password")

    assert isinstance(signed_in, Ok)
    assert isinstance(failed, Err)
    assert identity.throttle.counts[EMAIL_KEY][0] == 1  # one key, whatever the casing


async def test_a_wrong_password_is_invalid_credentials_and_opens_no_session(
    identity: Identity,
) -> None:
    user = identity.add_user()

    result = await _attempt(identity, password="not the password")

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_INVALID_CREDENTIALS"
    assert identity.sessions.by_id == {}
    assert identity.hasher.verified_hashes == [user.password_hash]


async def test_an_unknown_email_gets_the_same_error_and_checks_the_dummy_hash(
    identity: Identity,
) -> None:
    identity.add_user()
    wrong_password = await _attempt(identity, password="not the password")

    unknown = await _attempt(identity, email="nobody@example.test")

    assert isinstance(wrong_password, Err) and isinstance(unknown, Err)
    assert unknown.error == wrong_password.error
    assert identity.hasher.verified_hashes[-1] == identity.hasher.dummy_hash
    assert identity.sessions.by_id == {}


async def test_a_malformed_email_gets_the_same_error_and_checks_the_dummy_hash(
    identity: Identity,
) -> None:
    identity.add_user()

    result = await _attempt(identity, email="no-at")

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_INVALID_CREDENTIALS"
    assert identity.hasher.verified_hashes == [identity.hasher.dummy_hash]


async def test_an_inactive_user_cannot_sign_in_even_with_the_right_password(
    identity: Identity,
) -> None:
    identity.add_user(active=False)

    result = await _attempt(identity)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_INVALID_CREDENTIALS"
    assert identity.sessions.by_id == {}


async def test_failed_attempts_stay_counted(identity: Identity) -> None:
    identity.add_user()

    for _ in range(3):
        await _attempt(identity, password="not the password")

    assert identity.throttle.counts[EMAIL_KEY][0] == 3
    assert identity.throttle.counts[IP_KEY][0] == 3


async def test_the_attempt_above_the_email_limit_is_rejected_before_the_password_is_checked(
    identity: Identity,
) -> None:
    identity.add_user()
    for _ in range(POLICY.email_max_attempts):
        await _attempt(identity, password="not the password")
    verified_before = len(identity.hasher.verified_hashes)

    result = await _attempt(identity)  # right password, but one attempt too many

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_TOO_MANY_ATTEMPTS"
    assert identity.sessions.by_id == {}
    assert len(identity.hasher.verified_hashes) == verified_before  # nothing was verified


async def test_the_last_attempt_within_the_email_limit_still_works(identity: Identity) -> None:
    identity.add_user()
    for _ in range(POLICY.email_max_attempts - 1):
        await _attempt(identity, password="not the password")

    result = await _attempt(identity)

    assert isinstance(result, Ok)


async def test_a_blocked_email_does_not_block_another_email_from_the_same_ip(
    identity: Identity,
) -> None:
    identity.add_user()
    identity.add_user("staff@example.test", role=Role.STAFF)
    for _ in range(POLICY.email_max_attempts + 1):
        await _attempt(identity, password="not the password")

    other = await _attempt(identity, email="staff@example.test")

    assert isinstance(other, Ok)


async def test_the_ip_limit_blocks_every_email_from_that_ip_but_not_other_ips(
    identity: Identity,
) -> None:
    identity.add_user()
    for number in range(POLICY.ip_max_attempts):
        await _attempt(identity, email=f"guess{number}@example.test")

    blocked = await _attempt(identity)
    elsewhere = await _attempt(identity, ip="198.51.100.9")

    assert isinstance(blocked, Err)
    assert blocked.error.code == "IDENTITY_TOO_MANY_ATTEMPTS"
    assert isinstance(elsewhere, Ok)


async def test_a_successful_sign_in_clears_the_email_counter_and_gives_the_ip_attempt_back(
    identity: Identity,
) -> None:
    identity.add_user()
    for _ in range(2):
        await _attempt(identity, password="not the password")

    await _attempt(identity)

    assert EMAIL_KEY not in identity.throttle.counts
    assert identity.throttle.counts[IP_KEY][0] == 2  # 3 hits, 1 given back


async def test_after_a_successful_sign_in_the_email_has_its_full_allowance_again() -> None:
    identity = Identity(policy=replace(POLICY, ip_max_attempts=50))  # keep the IP out of the way
    identity.add_user()
    for _ in range(POLICY.email_max_attempts - 1):
        await _attempt(identity, password="not the password")
    await _attempt(identity)

    for _ in range(POLICY.email_max_attempts - 1):
        await _attempt(identity, password="not the password")
    result = await _attempt(identity)

    assert isinstance(result, Ok)


async def test_the_counters_start_over_when_the_window_is_over(identity: Identity) -> None:
    identity.add_user()
    for _ in range(POLICY.email_max_attempts):
        await _attempt(identity, password="not the password")
    identity.clock.current += POLICY.throttle_window

    result = await _attempt(identity)

    assert isinstance(result, Ok)


async def test_the_window_still_blocks_one_second_before_it_is_over(identity: Identity) -> None:
    identity.add_user()
    for _ in range(POLICY.email_max_attempts):
        await _attempt(identity, password="not the password")
    identity.clock.current += POLICY.throttle_window - timedelta(seconds=1)

    result = await _attempt(identity)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_TOO_MANY_ATTEMPTS"


async def test_an_unknown_ip_is_counted_under_a_shared_key(identity: Identity) -> None:
    identity.add_user()

    await _attempt(identity, password="not the password", ip=None)

    assert identity.throttle.counts["ip:unknown"][0] == 1
