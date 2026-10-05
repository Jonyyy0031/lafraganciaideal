from datetime import UTC, datetime, timedelta
from uuid import UUID

from fragancia_api.modules.identity.domain.session import Session

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
IDLE = timedelta(minutes=120)
MAX_AGE = timedelta(hours=12)
USER = UUID(int=7)


def _open(**overrides: object) -> Session:
    values: dict[str, object] = {
        "now": NOW,
        "max_age": MAX_AGE,
        "user_agent": "agent",
        "ip": "203.0.113.7",
    }
    values.update(overrides)
    return Session.open(USER, "digest", **values)  # type: ignore[arg-type]


def test_a_new_session_expires_after_the_absolute_lifetime() -> None:
    session = _open()

    assert session.user_id == USER
    assert session.token_hash == "digest"
    assert (session.created_at, session.last_seen_at) == (NOW, NOW)
    assert session.expires_at == NOW + MAX_AGE
    assert session.revoked_at is None
    assert (session.user_agent, session.ip) == ("agent", "203.0.113.7")


def test_the_user_agent_is_truncated_to_255_characters() -> None:
    assert _open(user_agent="x" * 300).user_agent == "x" * 255


def test_a_missing_user_agent_and_ip_stay_empty() -> None:
    session = _open(user_agent=None, ip=None)
    assert (session.user_agent, session.ip) == (None, None)


def test_a_fresh_session_is_valid() -> None:
    assert _open().is_valid(NOW, IDLE)


def test_a_session_is_valid_until_the_idle_deadline_and_not_at_it() -> None:
    session = _open()

    assert session.is_valid(NOW + IDLE - timedelta(seconds=1), IDLE)
    assert not session.is_valid(NOW + IDLE, IDLE)


def test_touching_a_session_moves_the_idle_deadline() -> None:
    session = _open()
    session.touch(NOW + timedelta(minutes=100))

    assert session.is_valid(NOW + timedelta(minutes=219), IDLE)
    assert not session.is_valid(NOW + timedelta(minutes=220), IDLE)


def test_a_session_is_not_valid_at_its_absolute_expiry_however_active() -> None:
    session = _open()
    session.touch(MAX_AGE + NOW - timedelta(seconds=1))

    assert session.is_valid(NOW + MAX_AGE - timedelta(seconds=1), IDLE)
    assert not session.is_valid(NOW + MAX_AGE, IDLE)


def test_a_revoked_session_is_not_valid() -> None:
    session = _open()
    session.revoke(NOW)

    assert not session.is_valid(NOW, IDLE)


def test_revoking_twice_keeps_the_first_time() -> None:
    session = _open()
    session.revoke(NOW)
    session.revoke(NOW + timedelta(hours=1))

    assert session.revoked_at == NOW
