"""Repair round 2 of plan 002: the final transactions re-read under a lock, and every reset flow
takes the user lock first. The fakes keep no rollback and no locks, so these tests pin the ORDER
of calls and the decisions taken on a re-read; the lock itself is an integration test."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from fragancia_api.modules.identity.domain.password_reset import PasswordReset
from fragancia_api.modules.identity.domain.user import DisplayName, Email, Role, User
from fragancia_api.modules.identity.infrastructure.in_memory import (
    InMemoryInvitations,
    InMemoryPasswordResets,
    InMemoryUsers,
)
from fragancia_api.shared.infrastructure.in_memory import InMemoryTransactionRunner
from fragancia_api.shared.kernel import Err, Ok, Result
from tests.unit.identity.conftest import PASSWORD, Identity

NEW_PASSWORD = "a brand new passphrase"
STAFF = "staff@example.test"
INVITER = UUID(int=1)


class RacingTransactions(InMemoryTransactionRunner):
    """Runs `before` right before the unit of work called `target`, as a concurrent request
    committing in between would."""

    def __init__(self, target: str) -> None:
        self.target = target
        self.before: Callable[[], Awaitable[None]] | None = None

    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
        if work.__name__ == self.target and self.before is not None:
            await self.before()
        return await work()


class CallLog:
    def __init__(self) -> None:
        self.calls: list[str] = []


class LoggingUsers(InMemoryUsers):
    def __init__(self, log: CallLog) -> None:
        super().__init__()
        self._log = log

    async def get_for_update(self, user_id: UUID) -> User | None:
        self._log.calls.append("lock user")
        return await super().get_for_update(user_id)


class LoggingResets(InMemoryPasswordResets):
    def __init__(self, log: CallLog) -> None:
        super().__init__()
        self._log = log

    async def cancel_open_for_user(self, user_id: UUID, *, now: datetime) -> None:
        self._log.calls.append("cancel resets")
        await super().cancel_open_for_user(user_id, now=now)

    async def add(self, reset: PasswordReset) -> None:
        self._log.calls.append("add reset")
        await super().add(reset)


class LoggingInvitations(InMemoryInvitations):
    def __init__(self, log: CallLog, *, on_revoke: Callable[[], None] | None = None) -> None:
        super().__init__()
        self._log = log
        self._on_revoke = on_revoke

    async def revoke_open_for_email(self, email: Email, *, now: datetime) -> None:
        self._log.calls.append("revoke open")
        if self._on_revoke is not None:
            self._on_revoke()
        await super().revoke_open_for_email(email, now=now)

    async def add(self, invitation: object) -> None:
        self._log.calls.append("add invitation")
        await super().add(invitation)  # type: ignore[arg-type]


class LoggingUserLookup(InMemoryUsers):
    def __init__(self, log: CallLog) -> None:
        super().__init__()
        self._log = log

    async def get_by_email(self, email: Email) -> User | None:
        self._log.calls.append("look up user by email")
        return await super().get_by_email(email)


# --- ChangePassword: the final transaction re-reads the user under a lock -------------------


async def _open_session(identity: Identity) -> UUID:
    login = await identity.sign_in()
    session = await identity.sessions.get_by_token_hash(identity.tokens.digest(login.token))
    assert session is not None
    return session.id


async def test_a_user_deactivated_while_changing_the_password_stays_deactivated() -> None:
    transactions = RacingTransactions("work")
    identity = Identity(transactions=transactions)
    user = identity.add_user()
    current = await _open_session(identity)
    old_hash = user.password_hash

    async def deactivate_meanwhile() -> None:
        user.deactivate()

    transactions.before = deactivate_meanwhile

    result = await identity.change_password.execute(user.id, current, PASSWORD, NEW_PASSWORD)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_ACTOR_INACTIVE"
    assert user.is_active is False
    assert user.password_hash == old_hash


async def test_a_user_removed_while_changing_the_password_is_refused() -> None:
    transactions = RacingTransactions("work")
    identity = Identity(transactions=transactions)
    user = identity.add_user()
    current = await _open_session(identity)

    async def remove_meanwhile() -> None:
        del identity.users.by_id[user.id]

    transactions.before = remove_meanwhile

    result = await identity.change_password.execute(user.id, current, PASSWORD, NEW_PASSWORD)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_ACTOR_INACTIVE"


async def test_a_refused_password_change_closes_no_session_and_clears_no_throttle() -> None:
    transactions = RacingTransactions("work")
    identity = Identity(transactions=transactions)
    user = identity.add_user()
    current = await _open_session(identity)
    other = await _open_session(identity)

    async def deactivate_meanwhile() -> None:
        user.deactivate()

    transactions.before = deactivate_meanwhile

    await identity.change_password.execute(user.id, current, PASSWORD, NEW_PASSWORD)

    assert identity.sessions.by_id[other].revoked_at is None
    assert identity.throttle.counts[f"password:{user.id}"][0] == 1


async def test_an_unchanged_user_changes_the_password_through_the_locked_re_read() -> None:
    transactions = RacingTransactions("work")
    identity = Identity(transactions=transactions)
    user = identity.add_user()
    current = await _open_session(identity)

    async def nothing() -> None:
        return None

    transactions.before = nothing

    result = await identity.change_password.execute(user.id, current, PASSWORD, NEW_PASSWORD)

    assert isinstance(result, Ok)
    assert identity.users.by_id[user.id].password_hash == identity.hasher.encode(NEW_PASSWORD)
    assert identity.users.by_id[user.id].is_active is True


# --- RequestPasswordReset: lock the user before touching their resets -----------------------


async def test_a_reset_request_locks_the_user_before_cancelling_their_resets() -> None:
    log = CallLog()
    identity = Identity(users=LoggingUsers(log), resets=LoggingResets(log))
    identity.add_user("owner@example.test", role=Role.OWNER)

    result = await identity.request_reset.execute("owner@example.test", ip=None)

    assert isinstance(result, Ok)
    assert log.calls == ["lock user", "cancel resets", "add reset"]


async def test_a_user_deactivated_between_the_lookup_and_the_lock_gets_no_reset() -> None:
    class DeactivatingUsers(LoggingUsers):
        async def get_for_update(self, user_id: UUID) -> User | None:
            self.by_id[user_id].deactivate()  # committed by someone else before our lock
            return await super().get_for_update(user_id)

    log = CallLog()
    identity = Identity(users=DeactivatingUsers(log), resets=LoggingResets(log))
    identity.add_user("owner@example.test")

    result = await identity.request_reset.execute("owner@example.test", ip=None)

    assert isinstance(result, Ok)  # the same answer as for an unknown email
    assert log.calls == ["lock user"]
    assert identity.resets.by_id == {}
    assert identity.events.published == []


async def test_an_unknown_email_takes_no_lock() -> None:
    log = CallLog()
    identity = Identity(users=LoggingUsers(log), resets=LoggingResets(log))

    result = await identity.request_reset.execute("nobody@example.test", ip=None)

    assert isinstance(result, Ok)
    assert log.calls == []


# --- InviteUser: revoke first, then look for the user ---------------------------------------


async def test_inviting_revokes_the_open_invitation_before_looking_for_the_user() -> None:
    log = CallLog()
    identity = Identity(users=LoggingUserLookup(log), invitations=LoggingInvitations(log))

    result = await identity.invite_user.execute(INVITER, STAFF, "Staff Uno")

    assert isinstance(result, Ok)
    assert log.calls == ["revoke open", "look up user by email", "add invitation"]


async def test_an_acceptance_that_lands_during_the_revoke_makes_the_email_taken() -> None:
    """The real UPDATE waits for an acceptance in flight; the user check after it then sees the
    account the acceptance created. Simulated: the user appears while the revoke runs."""
    log = CallLog()
    users = LoggingUserLookup(log)

    def accepted_meanwhile() -> None:
        created = User.create(
            Email(STAFF), DisplayName("Staff Uno"), Role.STAFF, "hash", now=datetime(2026, 1, 1)
        )
        users.by_id[created.id] = created

    identity = Identity(
        users=users, invitations=LoggingInvitations(log, on_revoke=accepted_meanwhile)
    )

    result = await identity.invite_user.execute(INVITER, STAFF, "Staff Uno")

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_EMAIL_TAKEN"
    assert "add invitation" not in log.calls
    assert identity.events.published == []
