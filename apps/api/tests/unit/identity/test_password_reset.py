from datetime import timedelta

from fragancia_api.modules.identity.application.commands.log_in import email_throttle_key
from fragancia_api.modules.identity.domain.events import PasswordResetRequested
from fragancia_api.modules.identity.domain.password_reset import PasswordReset
from fragancia_api.modules.identity.domain.user import Role
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.identity.conftest import LINKS, PASSWORD, POLICY, Identity

OWNER = "owner@example.test"
IP = "203.0.113.7"
NEW_PASSWORD = "a brand new passphrase"


async def _request(identity: Identity, email: str = OWNER, ip: str | None = IP) -> bool:
    result = await identity.request_reset.execute(email, ip=ip)
    return isinstance(result, Ok)


async def _mailed_token(identity: Identity) -> str:
    """Deliver the last reset event as the worker would and read the token from the email."""
    event = next(
        e for e in reversed(identity.events.published) if isinstance(e, PasswordResetRequested)
    )
    await identity.send_reset_email(identity.message_of(event))
    return identity.email.sent[-1].body.split("#token=")[1].split()[0]


def _resets(identity: Identity) -> list[PasswordReset]:
    return list(identity.resets.by_id.values())


# --- RequestPasswordReset --------------------------------------------------------------------


async def test_requesting_for_an_active_user_stores_a_reset_and_publishes_its_event(
    identity: Identity,
) -> None:
    user = identity.add_user()

    assert await _request(identity)

    [reset] = _resets(identity)
    assert reset.user_id == user.id
    assert reset.expires_at == identity.clock.now() + LINKS.reset_ttl
    assert reset.token_hash is None
    [event] = identity.events.published
    assert isinstance(event, PasswordResetRequested)
    assert event.reset_id == reset.id


async def test_the_email_is_matched_like_a_login_email(identity: Identity) -> None:
    identity.add_user()

    assert await _request(identity, email="  OWNER@Example.TEST ")

    assert len(_resets(identity)) == 1


async def test_an_unknown_email_answers_ok_and_creates_nothing(identity: Identity) -> None:
    identity.add_user()

    assert await _request(identity, email="nobody@example.test")

    assert _resets(identity) == []
    assert identity.events.published == []


async def test_a_malformed_email_answers_ok_and_creates_nothing(identity: Identity) -> None:
    assert await _request(identity, email="no-at")

    assert _resets(identity) == []
    assert identity.events.published == []


async def test_an_inactive_user_answers_ok_and_creates_nothing(identity: Identity) -> None:
    identity.add_user(active=False)

    assert await _request(identity)

    assert _resets(identity) == []
    assert identity.events.published == []


async def test_requesting_again_cancels_the_open_reset(identity: Identity) -> None:
    identity.add_user()
    await _request(identity)
    [first] = _resets(identity)

    await _request(identity)

    assert first.cancelled_at == identity.clock.now()
    assert len(_resets(identity)) == 2
    assert len([r for r in _resets(identity) if r.is_pending(identity.clock.now())]) == 1


async def test_requests_are_counted_under_their_own_reset_keys(identity: Identity) -> None:
    identity.add_user()

    await _request(identity)

    assert identity.throttle.counts[f"reset-{email_throttle_key(OWNER)}"][0] == 1
    assert identity.throttle.counts[f"reset-ip:{IP}"][0] == 1
    assert f"email:{OWNER}" not in identity.throttle.counts  # not the login keys
    assert f"ip:{IP}" not in identity.throttle.counts


async def test_an_unknown_ip_is_counted_under_a_shared_reset_key(identity: Identity) -> None:
    await _request(identity, ip=None)

    assert identity.throttle.counts["reset-ip:unknown"][0] == 1


async def test_the_request_above_the_email_limit_is_rejected_and_sends_nothing_new(
    identity: Identity,
) -> None:
    identity.add_user()
    for _ in range(POLICY.email_max_attempts):
        assert await _request(identity)
    stored = len(_resets(identity))

    result = await identity.request_reset.execute(OWNER, ip=IP)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_TOO_MANY_ATTEMPTS"
    assert len(_resets(identity)) == stored


async def test_the_ip_limit_applies_to_unknown_emails_too(identity: Identity) -> None:
    for number in range(POLICY.ip_max_attempts):
        assert await _request(identity, email=f"guess{number}@example.test")

    result = await identity.request_reset.execute("another@example.test", ip=IP)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_TOO_MANY_ATTEMPTS"


async def test_a_reset_request_does_not_consume_the_login_allowance(identity: Identity) -> None:
    identity.add_user()
    for _ in range(POLICY.email_max_attempts):
        await _request(identity)

    assert isinstance(await identity.log_in.execute(OWNER, PASSWORD, ip=IP, user_agent="t"), Ok)


# --- ResetPassword ---------------------------------------------------------------------------


async def test_resetting_changes_the_password_and_uses_the_reset(identity: Identity) -> None:
    user = identity.add_user()
    await _request(identity)
    token = await _mailed_token(identity)
    identity.clock.current += timedelta(minutes=5)

    result = await identity.reset_password.execute(token, NEW_PASSWORD)

    assert isinstance(result, Ok)
    assert user.password_hash == identity.hasher.encode(NEW_PASSWORD)
    assert user.password_changed_at == identity.clock.now()
    [reset] = _resets(identity)
    assert reset.used_at == identity.clock.now()
    assert isinstance(await identity.log_in.execute(OWNER, NEW_PASSWORD, ip=IP, user_agent="t"), Ok)
    old = await identity.log_in.execute(OWNER, PASSWORD, ip=IP, user_agent="t")
    assert isinstance(old, Err)


async def test_resetting_closes_every_session_of_the_user_and_only_theirs(
    identity: Identity,
) -> None:
    identity.add_user()
    other = identity.add_user("staff@example.test", role=Role.STAFF)
    first = await identity.sign_in()
    second = await identity.sign_in()
    other_login = await identity.sign_in("staff@example.test")
    await _request(identity)
    token = await _mailed_token(identity)

    await identity.reset_password.execute(token, NEW_PASSWORD)

    assert await identity.resolver.resolve(first.token) is None
    assert await identity.resolver.resolve(second.token) is None
    actor = await identity.resolver.resolve(other_login.token)
    assert actor is not None and actor.id == str(other.id)


async def test_a_link_works_once(identity: Identity) -> None:
    identity.add_user()
    await _request(identity)
    token = await _mailed_token(identity)
    await identity.reset_password.execute(token, NEW_PASSWORD)

    again = await identity.reset_password.execute(token, "yet another passphrase")

    assert isinstance(again, Err)
    assert again.error.code == "IDENTITY_LINK_INVALID"


async def test_an_unknown_token_is_link_invalid(identity: Identity) -> None:
    identity.add_user()

    result = await identity.reset_password.execute("not-a-token", NEW_PASSWORD)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_LINK_INVALID"


async def test_an_expired_link_is_link_invalid(identity: Identity) -> None:
    user = identity.add_user()
    await _request(identity)
    token = await _mailed_token(identity)
    identity.clock.current += LINKS.reset_ttl

    result = await identity.reset_password.execute(token, NEW_PASSWORD)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_LINK_INVALID"
    assert user.password_hash == identity.hasher.encode(PASSWORD)


async def test_a_link_one_second_before_the_expiry_still_works(identity: Identity) -> None:
    identity.add_user()
    await _request(identity)
    token = await _mailed_token(identity)
    identity.clock.current += LINKS.reset_ttl - timedelta(seconds=1)

    assert isinstance(await identity.reset_password.execute(token, NEW_PASSWORD), Ok)


async def test_a_second_request_kills_the_first_link_and_the_second_one_works(
    identity: Identity,
) -> None:
    identity.add_user()
    await _request(identity)
    first_token = await _mailed_token(identity)
    await _request(identity)
    second_token = await _mailed_token(identity)

    first = await identity.reset_password.execute(first_token, NEW_PASSWORD)
    second = await identity.reset_password.execute(second_token, NEW_PASSWORD)

    assert isinstance(first, Err)
    assert first.error.code == "IDENTITY_LINK_INVALID"
    assert isinstance(second, Ok)


async def test_a_weak_password_is_rejected_and_the_link_stays_usable(identity: Identity) -> None:
    identity.add_user()
    await _request(identity)
    token = await _mailed_token(identity)

    weak = await identity.reset_password.execute(token, "short")

    assert isinstance(weak, Err)
    assert weak.error.code == "IDENTITY_PASSWORD_TOO_WEAK"
    assert isinstance(await identity.reset_password.execute(token, NEW_PASSWORD), Ok)


async def test_a_user_deactivated_after_the_request_gets_link_invalid(identity: Identity) -> None:
    user = identity.add_user()
    await _request(identity)
    token = await _mailed_token(identity)
    user.deactivate()

    result = await identity.reset_password.execute(token, NEW_PASSWORD)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_LINK_INVALID"
    assert user.password_hash == identity.hasher.encode(PASSWORD)


async def test_a_reset_whose_user_is_gone_gets_link_invalid(identity: Identity) -> None:
    user = identity.add_user()
    await _request(identity)
    token = await _mailed_token(identity)
    del identity.users.by_id[user.id]

    result = await identity.reset_password.execute(token, NEW_PASSWORD)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_LINK_INVALID"


async def test_the_reset_belongs_to_the_user_it_was_requested_for(identity: Identity) -> None:
    identity.add_user()
    other = identity.add_user("staff@example.test", role=Role.STAFF)
    await _request(identity)
    token = await _mailed_token(identity)

    await identity.reset_password.execute(token, NEW_PASSWORD)

    assert other.password_hash == identity.hasher.encode(PASSWORD)
