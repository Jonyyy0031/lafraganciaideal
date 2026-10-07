"""Outbox subscribers that mint an emailed link's token and send the email.

They run in the worker, inside the relay's transaction: `TransactionRunner.run` joins it, so a
failed send rolls back the stored digest and the event is retried (with a fresh token). Delivery
is at-least-once, so every handler skips records that are no longer pending.
"""

from uuid import UUID

from fragancia_api.modules.identity.application.policy import AccountLinks
from fragancia_api.modules.identity.application.ports import SessionTokens
from fragancia_api.modules.identity.domain.repositories import (
    InvitationRepository,
    PasswordResetRepository,
    UserRepository,
)
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.email import EmailMessage, EmailSender
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result

INVITATION_SUBJECT = "Te invitaron al panel de La Fragancia Ideal"
INVITATION_BODY = (
    "Hola {name}:\n\n"
    "Te invitaron a administrar la tienda La Fragancia Ideal. Para crear tu contraseña, "
    "abre este enlace (vence en {hours} horas y solo funciona una vez):\n\n"
    "{url}\n\n"
    "Si no esperabas este correo, ignóralo.\n"
)

RESET_SUBJECT = "Restablece tu contraseña de La Fragancia Ideal"
RESET_BODY = (
    "Hola {name}:\n\n"
    "Recibimos una solicitud para restablecer tu contraseña. Abre este enlace "
    "(vence en {minutes} minutos y solo funciona una vez):\n\n"
    "{url}\n\n"
    "Al cambiarla se cerrarán todas tus sesiones abiertas. Si no la pediste, ignora este "
    "correo: tu contraseña no cambia.\n"
)


class SendInvitationEmail:
    def __init__(
        self,
        *,
        invitations: InvitationRepository,
        tokens: SessionTokens,
        email: EmailSender,
        transactions: TransactionRunner,
        clock: Clock,
        links: AccountLinks,
    ) -> None:
        self._invitations = invitations
        self._tokens = tokens
        self._email = email
        self._transactions = transactions
        self._clock = clock
        self._links = links

    async def __call__(self, message: EventMessage) -> None:
        invitation_id = UUID(str(message.payload["invitation_id"]))

        async def work() -> Result[None, DomainError]:
            invitation = await self._invitations.get(invitation_id, for_update=True)
            if invitation is None or not invitation.is_pending(self._clock.now()):
                return Ok(None)  # revoked, accepted or expired: nothing to send
            token = self._tokens.new()
            invitation.attach_token(self._tokens.digest(token))
            await self._invitations.save(invitation)
            hours = int(self._links.invitation_ttl.total_seconds() // 3600)
            await self._email.send(
                EmailMessage(
                    to=invitation.email.value,
                    subject=INVITATION_SUBJECT,
                    body=INVITATION_BODY.format(
                        name=invitation.name.value,
                        hours=hours,
                        url=self._links.invitation_url(token),
                    ),
                )
            )
            return Ok(None)

        match await self._transactions.run(work):
            case Err(error):
                raise RuntimeError(error.code)
            case Ok(_):
                return


class SendPasswordResetEmail:
    def __init__(
        self,
        *,
        resets: PasswordResetRepository,
        users: UserRepository,
        tokens: SessionTokens,
        email: EmailSender,
        transactions: TransactionRunner,
        clock: Clock,
        links: AccountLinks,
    ) -> None:
        self._resets = resets
        self._users = users
        self._tokens = tokens
        self._email = email
        self._transactions = transactions
        self._clock = clock
        self._links = links

    async def __call__(self, message: EventMessage) -> None:
        reset_id = UUID(str(message.payload["reset_id"]))

        async def work() -> Result[None, DomainError]:
            reset = await self._resets.get(reset_id)
            if reset is None:
                return Ok(None)
            # Lock order: user, then reset (same as DeactivateUser and ResetPassword).
            user = await self._users.get_for_update(reset.user_id)
            if user is None or not user.is_active:
                return Ok(None)
            reset = await self._resets.get(reset_id, for_update=True)
            if reset is None or not reset.is_pending(self._clock.now()):
                return Ok(None)
            token = self._tokens.new()
            reset.attach_token(self._tokens.digest(token))
            await self._resets.save(reset)
            minutes = int(self._links.reset_ttl.total_seconds() // 60)
            await self._email.send(
                EmailMessage(
                    to=user.email.value,
                    subject=RESET_SUBJECT,
                    body=RESET_BODY.format(
                        name=user.name.value,
                        minutes=minutes,
                        url=self._links.reset_url(token),
                    ),
                )
            )
            return Ok(None)

        match await self._transactions.run(work):
            case Err(error):
                raise RuntimeError(error.code)
            case Ok(_):
                return
