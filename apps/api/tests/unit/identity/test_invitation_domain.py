from datetime import UTC, datetime, timedelta
from uuid import UUID

from fragancia_api.modules.identity.domain.events import InvitationIssued
from fragancia_api.modules.identity.domain.invitation import Invitation
from fragancia_api.modules.identity.domain.user import DisplayName, Email

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
TTL = timedelta(hours=72)
INVITER = UUID(int=7)


def _issue() -> Invitation:
    return Invitation.issue(
        Email("staff@example.test"), DisplayName("Staff Uno"), INVITER, now=NOW, ttl=TTL
    )


def test_issuing_sets_the_expiry_and_has_no_token_yet() -> None:
    invitation = _issue()

    assert invitation.created_at == NOW
    assert invitation.expires_at == NOW + TTL
    assert invitation.token_hash is None
    assert invitation.invited_by == INVITER
    assert (invitation.accepted_at, invitation.revoked_at) == (None, None)


def test_issuing_records_one_event_that_carries_only_the_id() -> None:
    invitation = _issue()

    [event] = invitation.pull_events()
    assert isinstance(event, InvitationIssued)
    assert event.name == "identity.invitation.issued"
    assert event.payload() == {"invitation_id": str(invitation.id)}
    assert invitation.pull_events() == []  # recorded once


def test_a_new_invitation_is_pending() -> None:
    assert _issue().is_pending(NOW)


def test_it_is_pending_one_second_before_the_expiry_and_not_at_the_expiry() -> None:
    invitation = _issue()

    assert invitation.is_pending(invitation.expires_at - timedelta(seconds=1))
    assert not invitation.is_pending(invitation.expires_at)
    assert not invitation.is_pending(invitation.expires_at + timedelta(seconds=1))


def test_attaching_a_token_stores_the_digest() -> None:
    invitation = _issue()

    invitation.attach_token("digest:abc")

    assert invitation.token_hash == "digest:abc"


def test_accepting_ends_the_pending_state() -> None:
    invitation = _issue()

    invitation.accept(NOW + timedelta(hours=1))

    assert invitation.accepted_at == NOW + timedelta(hours=1)
    assert not invitation.is_pending(NOW + timedelta(hours=1))


def test_revoking_ends_the_pending_state() -> None:
    invitation = _issue()

    invitation.revoke(NOW + timedelta(hours=1))

    assert invitation.revoked_at == NOW + timedelta(hours=1)
    assert not invitation.is_pending(NOW + timedelta(hours=1))


def test_revoking_twice_keeps_the_first_time() -> None:
    invitation = _issue()
    invitation.revoke(NOW + timedelta(hours=1))

    invitation.revoke(NOW + timedelta(hours=2))

    assert invitation.revoked_at == NOW + timedelta(hours=1)


def test_an_accepted_invitation_cannot_be_revoked() -> None:
    invitation = _issue()
    invitation.accept(NOW)

    invitation.revoke(NOW + timedelta(hours=1))

    assert invitation.revoked_at is None


def test_a_revoked_invitation_cannot_be_accepted() -> None:
    invitation = _issue()
    invitation.revoke(NOW)

    invitation.accept(NOW + timedelta(hours=1))

    assert invitation.accepted_at is None


def test_accepting_twice_keeps_the_first_time() -> None:
    invitation = _issue()
    invitation.accept(NOW)

    invitation.accept(NOW + timedelta(hours=1))

    assert invitation.accepted_at == NOW
