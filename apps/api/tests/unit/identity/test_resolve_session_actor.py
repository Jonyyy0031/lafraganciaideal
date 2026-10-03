from datetime import timedelta

from fragancia_api.modules.identity.application.policy import TOUCH_INTERVAL
from fragancia_api.modules.identity.domain.session import Session
from fragancia_api.shared.application.actor import Actor
from tests.unit.identity.conftest import Identity


async def _session_of(identity: Identity, token: str) -> Session:
    session = await identity.sessions.get_by_token_hash(identity.tokens.digest(token))
    assert session is not None
    return session


async def test_a_valid_token_resolves_to_its_active_user_as_an_admin(identity: Identity) -> None:
    user = identity.add_user()
    login = await identity.sign_in()
    session = await _session_of(identity, login.token)

    actor = await identity.resolver.resolve(login.token)

    assert actor == Actor(id=str(user.id), is_admin=True, session_id=session.id)


async def test_an_unknown_token_resolves_to_nobody(identity: Identity) -> None:
    identity.add_user()
    await identity.sign_in()

    assert await identity.resolver.resolve("not-a-token") is None


async def test_the_digest_not_the_token_is_what_is_looked_up(identity: Identity) -> None:
    identity.add_user()
    login = await identity.sign_in()

    assert await identity.resolver.resolve(identity.tokens.digest(login.token)) is None


async def test_a_revoked_session_resolves_to_nobody(identity: Identity) -> None:
    identity.add_user()
    login = await identity.sign_in()
    (await _session_of(identity, login.token)).revoke(identity.clock.now())

    assert await identity.resolver.resolve(login.token) is None


async def test_an_idle_session_resolves_to_nobody(identity: Identity) -> None:
    identity.add_user()
    login = await identity.sign_in()
    identity.clock.current += identity.policy.session_idle

    assert await identity.resolver.resolve(login.token) is None


async def test_a_session_past_its_absolute_lifetime_resolves_to_nobody_however_active(
    identity: Identity,
) -> None:
    identity.add_user()
    login = await identity.sign_in()
    step = identity.policy.session_idle - timedelta(minutes=1)
    while identity.clock.now() + step < login.expires_at:
        identity.clock.current += step
        assert await identity.resolver.resolve(login.token) is not None  # keeps it alive
    identity.clock.current = login.expires_at

    assert await identity.resolver.resolve(login.token) is None


async def test_an_inactive_user_resolves_to_nobody_even_with_a_valid_session(
    identity: Identity,
) -> None:
    user = identity.add_user()
    login = await identity.sign_in()
    user.is_active = False

    assert await identity.resolver.resolve(login.token) is None


async def test_a_session_whose_user_is_gone_resolves_to_nobody(identity: Identity) -> None:
    user = identity.add_user()
    login = await identity.sign_in()
    del identity.users.by_id[user.id]

    assert await identity.resolver.resolve(login.token) is None


async def test_last_seen_is_not_written_within_the_touch_interval(identity: Identity) -> None:
    identity.add_user()
    login = await identity.sign_in()
    session = await _session_of(identity, login.token)
    opened_at = session.last_seen_at
    identity.clock.current += TOUCH_INTERVAL - timedelta(seconds=1)

    await identity.resolver.resolve(login.token)

    assert session.last_seen_at == opened_at


async def test_last_seen_is_refreshed_once_the_touch_interval_has_passed(
    identity: Identity,
) -> None:
    identity.add_user()
    login = await identity.sign_in()
    session = await _session_of(identity, login.token)
    identity.clock.current += TOUCH_INTERVAL

    await identity.resolver.resolve(login.token)

    assert session.last_seen_at == identity.clock.now()


async def test_activity_extends_the_idle_deadline(identity: Identity) -> None:
    identity.add_user()
    login = await identity.sign_in()
    identity.clock.current += identity.policy.session_idle - timedelta(minutes=1)
    assert await identity.resolver.resolve(login.token) is not None  # touches the session

    identity.clock.current += identity.policy.session_idle - timedelta(minutes=1)

    assert await identity.resolver.resolve(login.token) is not None
