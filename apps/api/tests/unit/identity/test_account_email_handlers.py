from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest

from fragancia_api.modules.identity.application.handlers.account_emails import (
    INVITATION_SUBJECT,
    RESET_SUBJECT,
)
from fragancia_api.modules.identity.domain.events import InvitationIssued, PasswordResetRequested
from fragancia_api.modules.identity.domain.invitation import Invitation
from fragancia_api.modules.identity.domain.password_reset import PasswordReset
from fragancia_api.modules.identity.domain.user import DisplayName, Email
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.kernel import Ok
from tests.unit.identity.conftest import LINKS, Identity

STAFF = "staff@example.test"
INVITER = UUID(int=1)


async def _invited(identity: Identity) -> Invitation:
    result = await identity.invite_user.execute(INVITER, STAFF, "Staff Uno")
    assert isinstance(result, Ok), result
    return identity.invitations.by_id[result.value]


def _message(identity: Identity, event_type: type) -> EventMessage:
    event = next(e for e in identity.events.published if isinstance(e, event_type))
    return identity.message_of(event)


# --- SendInvitationEmail ---------------------------------------------------------------------


async def test_the_invitation_email_carries_the_fragment_link_and_the_copy(
    identity: Identity,
) -> None:
    await _invited(identity)

    await identity.send_invitation_email(_message(identity, InvitationIssued))

    [mail] = identity.email.sent
    assert mail.to == STAFF
    assert mail.subject == INVITATION_SUBJECT == "Te invitaron al panel de La Fragancia Ideal"
    assert mail.body == (
        "Hola Staff Uno:\n\n"
        "Te invitaron a administrar la tienda La Fragancia Ideal. Para crear tu contraseña, "
        "abre este enlace (vence en 72 horas y solo funciona una vez):\n\n"
        "http://admin.test/admin/activar-cuenta#token=token-1\n\n"
        "Si no esperabas este correo, ignóralo.\n"
    )


async def test_only_the_digest_of_the_token_is_stored(identity: Identity) -> None:
    invitation = await _invited(identity)

    await identity.send_invitation_email(_message(identity, InvitationIssued))

    assert invitation.token_hash == "digest:token-1"
    assert invitation.token_hash != "token-1"  # the raw token is never stored


async def test_the_invitation_hours_follow_the_configured_ttl() -> None:
    identity = Identity(links=replace(LINKS, invitation_ttl=timedelta(hours=24)))
    await _invited(identity)

    await identity.send_invitation_email(_message(identity, InvitationIssued))

    assert "vence en 24 horas" in identity.email.sent[0].body


async def test_a_redelivered_invitation_event_sends_a_fresh_token_and_kills_the_old_one(
    identity: Identity,
) -> None:
    invitation = await _invited(identity)
    message = _message(identity, InvitationIssued)

    await identity.send_invitation_email(message)
    await identity.send_invitation_email(message)

    assert len(identity.email.sent) == 2
    assert "token=token-1" in identity.email.sent[0].body
    assert "token=token-2" in identity.email.sent[1].body
    assert invitation.token_hash == "digest:token-2"


async def test_a_revoked_invitation_sends_nothing(identity: Identity) -> None:
    invitation = await _invited(identity)
    invitation.revoke(identity.clock.now())

    await identity.send_invitation_email(_message(identity, InvitationIssued))

    assert identity.email.sent == []
    assert invitation.token_hash is None


async def test_an_accepted_invitation_sends_nothing(identity: Identity) -> None:
    invitation = await _invited(identity)
    invitation.accept(identity.clock.now())

    await identity.send_invitation_email(_message(identity, InvitationIssued))

    assert identity.email.sent == []


async def test_an_expired_invitation_sends_nothing(identity: Identity) -> None:
    await _invited(identity)
    identity.clock.current += LINKS.invitation_ttl

    await identity.send_invitation_email(_message(identity, InvitationIssued))

    assert identity.email.sent == []


async def test_an_unknown_invitation_sends_nothing(identity: Identity) -> None:
    other = Invitation.issue(
        Email(STAFF),
        DisplayName("Staff Uno"),
        INVITER,
        now=identity.clock.now(),
        ttl=LINKS.invitation_ttl,
    )

    await identity.send_invitation_email(identity.message_of(other.pull_events()[0]))

    assert identity.email.sent == []


async def test_a_failing_sender_raises_so_the_relay_retries(identity: Identity) -> None:
    await _invited(identity)
    identity.email.fail = True

    with pytest.raises(ConnectionError, match="smtp down"):
        await identity.send_invitation_email(_message(identity, InvitationIssued))


# --- SendPasswordResetEmail ------------------------------------------------------------------


async def _requested(identity: Identity) -> PasswordReset:
    identity.add_user(STAFF, name="Staff Uno")
    await identity.request_reset.execute(STAFF, ip="203.0.113.7")
    return next(iter(identity.resets.by_id.values()))


async def test_the_reset_email_carries_the_fragment_link_and_the_copy(identity: Identity) -> None:
    await _requested(identity)

    await identity.send_reset_email(_message(identity, PasswordResetRequested))

    [mail] = identity.email.sent
    assert mail.to == STAFF
    assert mail.subject == RESET_SUBJECT == "Restablece tu contraseña de La Fragancia Ideal"
    assert mail.body == (
        "Hola Staff Uno:\n\n"
        "Recibimos una solicitud para restablecer tu contraseña. Abre este enlace "
        "(vence en 60 minutos y solo funciona una vez):\n\n"
        "http://admin.test/admin/restablecer-contrasena#token=token-1\n\n"
        "Al cambiarla se cerrarán todas tus sesiones abiertas. Si no la pediste, ignora este "
        "correo: tu contraseña no cambia.\n"
    )


async def test_only_the_digest_of_the_reset_token_is_stored(identity: Identity) -> None:
    reset = await _requested(identity)

    await identity.send_reset_email(_message(identity, PasswordResetRequested))

    assert reset.token_hash == "digest:token-1"


async def test_a_redelivered_reset_event_sends_a_fresh_token(identity: Identity) -> None:
    reset = await _requested(identity)
    message = _message(identity, PasswordResetRequested)

    await identity.send_reset_email(message)
    await identity.send_reset_email(message)

    assert len(identity.email.sent) == 2
    assert reset.token_hash == "digest:token-2"


async def test_a_cancelled_reset_sends_nothing(identity: Identity) -> None:
    reset = await _requested(identity)
    reset.cancelled_at = identity.clock.now()

    await identity.send_reset_email(_message(identity, PasswordResetRequested))

    assert identity.email.sent == []
    assert reset.token_hash is None


async def test_a_used_reset_sends_nothing(identity: Identity) -> None:
    reset = await _requested(identity)
    reset.use(identity.clock.now())

    await identity.send_reset_email(_message(identity, PasswordResetRequested))

    assert identity.email.sent == []


async def test_an_expired_reset_sends_nothing(identity: Identity) -> None:
    await _requested(identity)
    identity.clock.current += LINKS.reset_ttl

    await identity.send_reset_email(_message(identity, PasswordResetRequested))

    assert identity.email.sent == []


async def test_a_reset_of_a_user_deactivated_meanwhile_sends_nothing(identity: Identity) -> None:
    reset = await _requested(identity)
    identity.users.by_id[reset.user_id].deactivate()

    await identity.send_reset_email(_message(identity, PasswordResetRequested))

    assert identity.email.sent == []
    assert reset.token_hash is None


async def test_a_reset_whose_user_is_gone_sends_nothing(identity: Identity) -> None:
    reset = await _requested(identity)
    del identity.users.by_id[reset.user_id]

    await identity.send_reset_email(_message(identity, PasswordResetRequested))

    assert identity.email.sent == []


async def test_a_failing_sender_raises_for_resets_too(identity: Identity) -> None:
    await _requested(identity)
    identity.email.fail = True

    with pytest.raises(ConnectionError, match="smtp down"):
        await identity.send_reset_email(_message(identity, PasswordResetRequested))
