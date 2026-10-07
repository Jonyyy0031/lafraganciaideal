from datetime import UTC, datetime, timedelta
from uuid import UUID

from fragancia_api.modules.identity.domain.events import PasswordResetRequested
from fragancia_api.modules.identity.domain.password_reset import PasswordReset

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
TTL = timedelta(minutes=60)
USER = UUID(int=5)


def _request() -> PasswordReset:
    return PasswordReset.request(USER, now=NOW, ttl=TTL)


def test_requesting_sets_the_expiry_and_has_no_token_yet() -> None:
    reset = _request()

    assert (reset.user_id, reset.created_at, reset.expires_at) == (USER, NOW, NOW + TTL)
    assert reset.token_hash is None
    assert (reset.used_at, reset.cancelled_at) == (None, None)


def test_requesting_records_one_event_that_carries_only_the_id() -> None:
    reset = _request()

    [event] = reset.pull_events()
    assert isinstance(event, PasswordResetRequested)
    assert event.name == "identity.password_reset.requested"
    assert event.payload() == {"reset_id": str(reset.id)}
    assert reset.pull_events() == []


def test_it_is_pending_one_second_before_the_expiry_and_not_at_the_expiry() -> None:
    reset = _request()

    assert reset.is_pending(NOW)
    assert reset.is_pending(reset.expires_at - timedelta(seconds=1))
    assert not reset.is_pending(reset.expires_at)


def test_attaching_a_token_stores_the_digest() -> None:
    reset = _request()

    reset.attach_token("digest:abc")

    assert reset.token_hash == "digest:abc"


def test_using_it_ends_the_pending_state_and_keeps_the_first_time() -> None:
    reset = _request()
    reset.use(NOW + timedelta(minutes=1))

    reset.use(NOW + timedelta(minutes=2))

    assert reset.used_at == NOW + timedelta(minutes=1)
    assert not reset.is_pending(NOW + timedelta(minutes=2))


def test_a_cancelled_reset_cannot_be_used() -> None:
    reset = _request()
    reset.cancelled_at = NOW

    reset.use(NOW + timedelta(minutes=1))

    assert reset.used_at is None
    assert not reset.is_pending(NOW + timedelta(minutes=1))
