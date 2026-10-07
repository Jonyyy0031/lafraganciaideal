from datetime import timedelta
from uuid import UUID

from fragancia_api.modules.identity.domain.events import InvitationIssued
from fragancia_api.modules.identity.domain.user import Email, Role
from fragancia_api.modules.identity.infrastructure.in_memory import PlainTextPasswordHasher
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.identity.conftest import LINKS, PASSWORD, Identity

INVITER = UUID(int=1)
STAFF = "staff@example.test"


class CountingHasher(PlainTextPasswordHasher):
    """Records every password it was asked to hash."""

    def __init__(self) -> None:
        super().__init__()
        self.hashed: list[str] = []

    async def hash(self, password: str) -> str:
        self.hashed.append(password)
        return await super().hash(password)


async def _invite(identity: Identity, email: str = STAFF, name: str = "Staff Uno") -> UUID:
    result = await identity.invite_user.execute(INVITER, email, name)
    assert isinstance(result, Ok), result
    return result.value


async def _mailed_token(identity: Identity, invitation_id: UUID) -> str:
    """Deliver the invitation event as the worker would and read the token from the email."""
    event = next(
        e
        for e in reversed(identity.events.published)
        if isinstance(e, InvitationIssued) and e.invitation_id == invitation_id
    )
    await identity.send_invitation_email(identity.message_of(event))
    body = identity.email.sent[-1].body
    return body.split("#token=")[1].split()[0]


# --- InviteUser ------------------------------------------------------------------------------


async def test_inviting_stores_a_pending_invitation_and_publishes_its_event(
    identity: Identity,
) -> None:
    invitation_id = await _invite(identity)

    invitation = identity.invitations.by_id[invitation_id]
    assert (invitation.email, invitation.invited_by) == (Email(STAFF), INVITER)
    assert invitation.expires_at == identity.clock.now() + LINKS.invitation_ttl
    assert invitation.token_hash is None  # the token is minted when the email is sent
    [event] = identity.events.published
    assert isinstance(event, InvitationIssued)
    assert event.invitation_id == invitation_id


async def test_the_email_is_normalized_like_a_user_email(identity: Identity) -> None:
    invitation_id = await _invite(identity, email="  Staff@Example.TEST ")

    assert identity.invitations.by_id[invitation_id].email == Email(STAFF)


async def test_an_invalid_email_is_rejected_before_anything_is_written(
    identity: Identity,
) -> None:
    result = await identity.invite_user.execute(INVITER, "no-at", "Staff Uno")

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_EMAIL_INVALID"
    assert identity.invitations.by_id == {}
    assert identity.events.published == []


async def test_an_invalid_name_is_rejected_before_anything_is_written(identity: Identity) -> None:
    result = await identity.invite_user.execute(INVITER, STAFF, "x")

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_NAME_INVALID"
    assert identity.invitations.by_id == {}


async def test_the_email_error_wins_when_both_email_and_name_are_invalid(
    identity: Identity,
) -> None:
    result = await identity.invite_user.execute(INVITER, "no-at", "x")

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_EMAIL_INVALID"


async def test_an_email_that_already_has_a_user_is_taken(identity: Identity) -> None:
    identity.add_user(STAFF, role=Role.STAFF)

    result = await identity.invite_user.execute(INVITER, STAFF, "Staff Uno")

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_EMAIL_TAKEN"
    assert identity.invitations.by_id == {}
    assert identity.events.published == []


async def test_an_inactive_user_still_makes_the_email_taken(identity: Identity) -> None:
    identity.add_user(STAFF, role=Role.STAFF, active=False)

    result = await identity.invite_user.execute(INVITER, STAFF, "Staff Uno")

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_EMAIL_TAKEN"


async def test_inviting_again_revokes_the_open_invitation_for_that_email(
    identity: Identity,
) -> None:
    first = await _invite(identity)

    second = await _invite(identity)

    assert identity.invitations.by_id[first].revoked_at == identity.clock.now()
    assert identity.invitations.by_id[second].is_pending(identity.clock.now())
    assert first != second


async def test_inviting_another_email_leaves_other_invitations_alone(identity: Identity) -> None:
    first = await _invite(identity)

    await _invite(identity, email="other@example.test")

    assert identity.invitations.by_id[first].is_pending(identity.clock.now())


# --- RevokeInvitation ------------------------------------------------------------------------


async def test_revoking_a_pending_invitation_ends_it(identity: Identity) -> None:
    invitation_id = await _invite(identity)

    result = await identity.revoke_invitation.execute(invitation_id)

    assert isinstance(result, Ok)
    assert identity.invitations.by_id[invitation_id].revoked_at == identity.clock.now()


async def test_revoking_an_unknown_invitation_is_not_found(identity: Identity) -> None:
    result = await identity.revoke_invitation.execute(UUID(int=99))

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_INVITATION_NOT_FOUND"


async def test_revoking_twice_is_not_found_the_second_time(identity: Identity) -> None:
    invitation_id = await _invite(identity)
    await identity.revoke_invitation.execute(invitation_id)

    result = await identity.revoke_invitation.execute(invitation_id)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_INVITATION_NOT_FOUND"


async def test_an_expired_invitation_cannot_be_revoked(identity: Identity) -> None:
    invitation_id = await _invite(identity)
    identity.clock.current += LINKS.invitation_ttl

    result = await identity.revoke_invitation.execute(invitation_id)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_INVITATION_NOT_FOUND"


async def test_an_accepted_invitation_cannot_be_revoked(identity: Identity) -> None:
    invitation_id = await _invite(identity)
    token = await _mailed_token(identity, invitation_id)
    await identity.accept_invitation.execute(token, PASSWORD)

    result = await identity.revoke_invitation.execute(invitation_id)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_INVITATION_NOT_FOUND"


# --- AcceptInvitation ------------------------------------------------------------------------


async def test_accepting_creates_an_active_staff_user_who_can_sign_in(
    identity: Identity,
) -> None:
    invitation_id = await _invite(identity, name="Staff Uno")
    token = await _mailed_token(identity, invitation_id)

    result = await identity.accept_invitation.execute(token, PASSWORD)

    assert isinstance(result, Ok)
    user = identity.users.by_id[result.value]
    assert (user.email, user.name.value, user.role, user.is_active) == (
        Email(STAFF),
        "Staff Uno",
        Role.STAFF,
        True,
    )
    assert user.password_hash == identity.hasher.encode(PASSWORD)
    assert identity.invitations.by_id[invitation_id].accepted_at == identity.clock.now()
    login = await identity.sign_in(STAFF)
    assert login.user_id == user.id


async def test_a_link_works_once(identity: Identity) -> None:
    token = await _mailed_token(identity, await _invite(identity))
    await identity.accept_invitation.execute(token, PASSWORD)

    again = await identity.accept_invitation.execute(token, PASSWORD)

    assert isinstance(again, Err)
    assert again.error.code == "IDENTITY_LINK_INVALID"
    assert len(identity.users.by_id) == 1


async def test_an_unknown_token_is_rejected_without_hashing() -> None:
    hasher = CountingHasher()
    identity = Identity(hasher=hasher)

    result = await identity.accept_invitation.execute("not-a-token", PASSWORD)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_LINK_INVALID"
    assert hasher.hashed == []


async def test_an_expired_link_is_rejected_without_hashing() -> None:
    hasher = CountingHasher()
    identity = Identity(hasher=hasher)
    token = await _mailed_token(identity, await _invite(identity))
    identity.clock.current += LINKS.invitation_ttl

    result = await identity.accept_invitation.execute(token, PASSWORD)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_LINK_INVALID"
    assert hasher.hashed == []
    assert identity.users.by_id == {}


async def test_a_link_one_second_before_the_expiry_still_works(identity: Identity) -> None:
    token = await _mailed_token(identity, await _invite(identity))
    identity.clock.current += LINKS.invitation_ttl - timedelta(seconds=1)

    result = await identity.accept_invitation.execute(token, PASSWORD)

    assert isinstance(result, Ok)


async def test_a_revoked_link_is_rejected_without_hashing() -> None:
    hasher = CountingHasher()
    identity = Identity(hasher=hasher)
    invitation_id = await _invite(identity)
    token = await _mailed_token(identity, invitation_id)
    await identity.revoke_invitation.execute(invitation_id)

    result = await identity.accept_invitation.execute(token, PASSWORD)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_LINK_INVALID"
    assert hasher.hashed == []


async def test_inviting_again_kills_the_first_link_and_the_second_one_works(
    identity: Identity,
) -> None:
    first_id = await _invite(identity)
    first_token = await _mailed_token(identity, first_id)
    second_id = await _invite(identity)
    second_token = await _mailed_token(identity, second_id)

    first = await identity.accept_invitation.execute(first_token, PASSWORD)
    second = await identity.accept_invitation.execute(second_token, PASSWORD)

    assert isinstance(first, Err)
    assert first.error.code == "IDENTITY_LINK_INVALID"
    assert isinstance(second, Ok)


async def test_a_weak_password_is_rejected_before_the_token_is_looked_up(
    identity: Identity,
) -> None:
    token = await _mailed_token(identity, await _invite(identity))

    result = await identity.accept_invitation.execute(token, "short")

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_PASSWORD_TOO_WEAK"
    assert identity.users.by_id == {}
    # the link is still good afterwards
    assert isinstance(await identity.accept_invitation.execute(token, PASSWORD), Ok)


async def test_accepting_when_the_email_became_a_user_meanwhile_is_email_taken(
    identity: Identity,
) -> None:
    token = await _mailed_token(identity, await _invite(identity))
    identity.add_user(STAFF, role=Role.STAFF)

    result = await identity.accept_invitation.execute(token, PASSWORD)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_EMAIL_TAKEN"
