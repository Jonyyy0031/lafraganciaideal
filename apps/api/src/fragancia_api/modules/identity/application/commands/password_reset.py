from fragancia_api.modules.identity.application.commands.log_in import email_throttle_key
from fragancia_api.modules.identity.application.policy import AccountLinks, AuthPolicy
from fragancia_api.modules.identity.application.ports import (
    LoginThrottle,
    PasswordHasher,
    SessionTokens,
)
from fragancia_api.modules.identity.domain.errors import LinkInvalid, TooManyAttempts
from fragancia_api.modules.identity.domain.password_reset import PasswordReset
from fragancia_api.modules.identity.domain.repositories import (
    PasswordResetRepository,
    SessionRepository,
    UserRepository,
)
from fragancia_api.modules.identity.domain.user import Email, PlainPassword
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.events import EventPublisher
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class RequestPasswordReset:
    """Command: ask for a reset link by email. Answers the same whether or not the account
    exists (and is active); throttled per email and per IP so it cannot flood an inbox. The
    email itself is sent by the worker (outbox)."""

    def __init__(
        self,
        *,
        users: UserRepository,
        resets: PasswordResetRepository,
        throttle: LoginThrottle,
        transactions: TransactionRunner,
        events: EventPublisher,
        clock: Clock,
        policy: AuthPolicy,
        links: AccountLinks,
    ) -> None:
        self._users = users
        self._resets = resets
        self._throttle = throttle
        self._transactions = transactions
        self._events = events
        self._clock = clock
        self._policy = policy
        self._links = links

    async def execute(self, email: str, *, ip: str | None) -> Result[None, DomainError]:
        email_key = "reset-" + email_throttle_key(email)
        ip_key = f"reset-ip:{ip or 'unknown'}"

        async def count() -> Result[tuple[int, int], DomainError]:
            now = self._clock.now()
            window = self._policy.throttle_window
            email_count = await self._throttle.hit(email_key, now=now, window=window)
            ip_count = await self._throttle.hit(ip_key, now=now, window=window)
            return Ok((email_count, ip_count))  # always Ok: the counts must commit

        match await self._transactions.run(count):
            case Err(error):
                return Err(error)
            case Ok((email_count, ip_count)):
                pass
        if email_count > self._policy.email_max_attempts or ip_count > self._policy.ip_max_attempts:
            return Err(TooManyAttempts())

        async def work() -> Result[None, DomainError]:
            now = self._clock.now()
            match Email.create(email):
                case Err(_):
                    return Ok(None)
                case Ok(valid_email):
                    pass
            found = await self._users.get_by_email(valid_email)
            if found is None or not found.is_active:
                return Ok(None)
            # Lock the user before touching their resets: every reset flow locks the user first.
            user = await self._users.get_for_update(found.id)
            if user is None or not user.is_active:
                return Ok(None)
            await self._resets.cancel_open_for_user(user.id, now=now)
            reset = PasswordReset.request(user.id, now=now, ttl=self._links.reset_ttl)
            await self._resets.add(reset)
            await self._events.publish(reset.pull_events())
            return Ok(None)

        return await self._transactions.run(work)


class ResetPassword:
    """Command: set a new password with an emailed link and close every session of the user."""

    def __init__(
        self,
        *,
        users: UserRepository,
        sessions: SessionRepository,
        resets: PasswordResetRepository,
        hasher: PasswordHasher,
        tokens: SessionTokens,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._resets = resets
        self._hasher = hasher
        self._tokens = tokens
        self._transactions = transactions
        self._clock = clock

    async def execute(self, token: str, new_password: str) -> Result[None, DomainError]:
        match PlainPassword.create(new_password):
            case Err(weak):
                return Err(weak)
            case Ok(plain):
                pass
        digest = self._tokens.digest(token)

        async def check() -> Result[None, DomainError]:
            reset = await self._resets.get_by_token_hash(digest)
            if reset is None or not reset.is_pending(self._clock.now()):
                return Err(LinkInvalid())
            return Ok(None)

        match await self._transactions.run(check):
            case Err(error):
                return Err(error)
            case Ok(_):
                pass

        password_hash = await self._hasher.hash(plain.value)  # slow: outside any transaction

        async def work() -> Result[None, DomainError]:
            now = self._clock.now()
            # Lock order: user, then reset (same as DeactivateUser), so they cannot deadlock.
            found = await self._resets.get_by_token_hash(digest)
            if found is None:
                return Err(LinkInvalid())
            user = await self._users.get_for_update(found.user_id)
            if user is None or not user.is_active:
                return Err(LinkInvalid())
            reset = await self._resets.get_by_token_hash(digest, for_update=True)
            if reset is None or not reset.is_pending(now):
                return Err(LinkInvalid())
            user.change_password(password_hash, now=now)
            await self._users.save(user)
            reset.use(now)
            await self._resets.save(reset)
            await self._sessions.revoke_all_for_user(user.id, except_id=None, now=now)
            return Ok(None)

        return await self._transactions.run(work)
