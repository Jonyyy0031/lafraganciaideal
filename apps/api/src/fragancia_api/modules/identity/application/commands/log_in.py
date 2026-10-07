import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fragancia_api.modules.identity.application.policy import AuthPolicy
from fragancia_api.modules.identity.application.ports import (
    LoginThrottle,
    PasswordHasher,
    SessionTokens,
)
from fragancia_api.modules.identity.domain.errors import InvalidCredentials, TooManyAttempts
from fragancia_api.modules.identity.domain.repositories import SessionRepository, UserRepository
from fragancia_api.modules.identity.domain.session import Session
from fragancia_api.modules.identity.domain.user import EMAIL_MAX_LENGTH, Email, User
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


def email_throttle_key(email: str) -> str:
    """The throttle key of an email, normalized as `Email` does. Lowercasing can make a string
    longer, so a result longer than any valid email (which belongs to no account) is keyed by
    its SHA-256 instead: the key always fits the throttle table."""
    normalized = email.strip().lower()
    if len(normalized) > EMAIL_MAX_LENGTH:
        return f"email-sha256:{hashlib.sha256(normalized.encode()).hexdigest()}"
    return f"email:{normalized}"


@dataclass(frozen=True, slots=True)
class LoginResult:
    token: str
    expires_at: datetime
    user_id: UUID


class LogIn:
    """Command: check email and password and open a session.

    The attempt is counted (per email and per IP) BEFORE the password is checked, so parallel
    bursts cannot slip past the limit. A missing user is checked against a dummy hash, so the
    answer takes as long as for a real one. The final transaction re-reads the user under a row
    lock, so a deactivation or password reset that commits during the check wins.
    """

    def __init__(
        self,
        *,
        users: UserRepository,
        sessions: SessionRepository,
        throttle: LoginThrottle,
        hasher: PasswordHasher,
        tokens: SessionTokens,
        transactions: TransactionRunner,
        clock: Clock,
        policy: AuthPolicy,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._throttle = throttle
        self._hasher = hasher
        self._tokens = tokens
        self._transactions = transactions
        self._clock = clock
        self._policy = policy

    async def execute(
        self, email: str, password: str, *, ip: str | None, user_agent: str | None
    ) -> Result[LoginResult, DomainError]:
        email_key = email_throttle_key(email)
        ip_key = f"ip:{ip or 'unknown'}"

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

        async def find() -> Result[User | None, DomainError]:
            match Email.create(email):
                case Err(_):
                    return Ok(None)  # a malformed email cannot belong to anyone
                case Ok(valid_email):
                    return Ok(await self._users.get_by_email(valid_email))

        match await self._transactions.run(find):
            case Err(error):
                return Err(error)
            case Ok(user):
                pass

        # Slow: outside any transaction. Always verify something, even without a user.
        password_hash = user.password_hash if user is not None else self._hasher.dummy_hash
        password_matches = await self._hasher.verify(password_hash, password)
        if user is None or not user.is_active or not password_matches:
            return Err(InvalidCredentials())  # the attempt stays counted
        signed_in = user

        async def open_session() -> Result[LoginResult, DomainError]:
            current = await self._users.get_for_update(signed_in.id)
            if (
                current is None
                or not current.is_active
                or current.password_hash != signed_in.password_hash
            ):
                return Err(InvalidCredentials())  # the attempt stays counted
            await self._throttle.clear(email_key)
            await self._throttle.give_back(ip_key)
            token = self._tokens.new()
            session = Session.open(
                signed_in.id,
                self._tokens.digest(token),
                now=self._clock.now(),
                max_age=self._policy.session_max_age,
                user_agent=user_agent,
                ip=ip,
            )
            await self._sessions.add(session)
            return Ok(LoginResult(token=token, expires_at=session.expires_at, user_id=signed_in.id))

        return await self._transactions.run(open_session)
