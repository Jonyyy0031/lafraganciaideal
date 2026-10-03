"""Identity unit tests: every use case wired over the in-memory adapters."""

from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from fragancia_api.modules.identity.application.commands.change_password import ChangePassword
from fragancia_api.modules.identity.application.commands.create_user import CreateUser
from fragancia_api.modules.identity.application.commands.log_in import LogIn, LoginResult
from fragancia_api.modules.identity.application.commands.log_out import LogOut
from fragancia_api.modules.identity.application.commands.resolve_session_actor import (
    ResolveSessionActor,
)
from fragancia_api.modules.identity.application.commands.revoke_sessions import (
    RevokeOtherSessions,
    RevokeSession,
)
from fragancia_api.modules.identity.application.policy import AuthPolicy
from fragancia_api.modules.identity.application.queries.my_account import (
    GetMyAccount,
    ListMySessions,
)
from fragancia_api.modules.identity.domain.user import DisplayName, Email, Role, User
from fragancia_api.modules.identity.infrastructure.in_memory import (
    InMemoryAccountQueries,
    InMemoryLoginThrottle,
    InMemorySessions,
    InMemoryUsers,
    PlainTextPasswordHasher,
    SequentialSessionTokens,
)
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from fragancia_api.shared.kernel import Ok

PASSWORD = "correct horse battery"

POLICY = AuthPolicy(
    session_idle=timedelta(minutes=120),
    session_max_age=timedelta(hours=12),
    throttle_window=timedelta(minutes=15),
    email_max_attempts=5,
    ip_max_attempts=8,
)


@dataclass
class Identity:
    """The in-memory world of the identity module and all of its use cases."""

    policy: AuthPolicy = POLICY
    clock: FixedClock = field(default_factory=FixedClock)
    users: InMemoryUsers = field(default_factory=InMemoryUsers)
    sessions: InMemorySessions = field(default_factory=InMemorySessions)
    throttle: InMemoryLoginThrottle = field(default_factory=InMemoryLoginThrottle)
    hasher: PlainTextPasswordHasher = field(default_factory=PlainTextPasswordHasher)
    tokens: SequentialSessionTokens = field(default_factory=SequentialSessionTokens)
    transactions: InMemoryTransactionRunner = field(default_factory=InMemoryTransactionRunner)

    def __post_init__(self) -> None:
        queries = InMemoryAccountQueries(self.users, self.sessions)
        transactions, clock = self.transactions, self.clock
        self.create_user = CreateUser(
            users=self.users, hasher=self.hasher, transactions=transactions, clock=clock
        )
        self.log_in = LogIn(
            users=self.users,
            sessions=self.sessions,
            throttle=self.throttle,
            hasher=self.hasher,
            tokens=self.tokens,
            transactions=transactions,
            clock=clock,
            policy=self.policy,
        )
        self.log_out = LogOut(sessions=self.sessions, transactions=transactions, clock=clock)
        self.change_password = ChangePassword(
            users=self.users,
            sessions=self.sessions,
            hasher=self.hasher,
            transactions=transactions,
            clock=clock,
        )
        self.revoke_session = RevokeSession(
            sessions=self.sessions, transactions=transactions, clock=clock
        )
        self.revoke_others = RevokeOtherSessions(
            sessions=self.sessions, transactions=transactions, clock=clock
        )
        self.resolver = ResolveSessionActor(
            sessions=self.sessions,
            users=self.users,
            tokens=self.tokens,
            transactions=transactions,
            clock=clock,
            policy=self.policy,
        )
        self.get_my_account = GetMyAccount(queries)
        self.list_my_sessions = ListMySessions(queries, clock=self.clock, policy=self.policy)

    def add_user(
        self,
        email: str = "owner@example.test",
        *,
        password: str = PASSWORD,
        role: Role = Role.OWNER,
        active: bool = True,
        name: str = "Owner Test",
    ) -> User:
        user = User.create(
            Email(email),
            DisplayName(name),
            role,
            self.hasher.hash(password),
            now=self.clock.now(),
        )
        user.is_active = active
        self.users.by_id[user.id] = user
        return user

    async def sign_in(
        self, email: str = "owner@example.test", password: str = PASSWORD
    ) -> LoginResult:
        result = await self.log_in.execute(email, password, ip="203.0.113.7", user_agent="tests")
        assert isinstance(result, Ok), result
        return result.value


@pytest.fixture
def identity() -> Identity:
    return Identity()
