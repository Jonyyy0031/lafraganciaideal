"""Identity unit tests: every use case wired over the in-memory adapters."""

from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from fragancia_api.modules.identity.application.commands.change_password import ChangePassword
from fragancia_api.modules.identity.application.commands.create_user import CreateUser
from fragancia_api.modules.identity.application.commands.invitations import (
    AcceptInvitation,
    InviteUser,
    RevokeInvitation,
)
from fragancia_api.modules.identity.application.commands.log_in import LogIn, LoginResult
from fragancia_api.modules.identity.application.commands.log_out import LogOut
from fragancia_api.modules.identity.application.commands.password_reset import (
    RequestPasswordReset,
    ResetPassword,
)
from fragancia_api.modules.identity.application.commands.resolve_session_actor import (
    ResolveSessionActor,
)
from fragancia_api.modules.identity.application.commands.revoke_sessions import (
    RevokeOtherSessions,
    RevokeSession,
)
from fragancia_api.modules.identity.application.commands.user_status import (
    DeactivateUser,
    ReactivateUser,
)
from fragancia_api.modules.identity.application.handlers.account_emails import (
    SendInvitationEmail,
    SendPasswordResetEmail,
)
from fragancia_api.modules.identity.application.policy import AccountLinks, AuthPolicy
from fragancia_api.modules.identity.application.queries.my_account import (
    GetMyAccount,
    ListMySessions,
)
from fragancia_api.modules.identity.application.queries.team import (
    ListPendingInvitations,
    ListUsers,
)
from fragancia_api.modules.identity.domain.user import DisplayName, Email, Role, User
from fragancia_api.modules.identity.infrastructure.in_memory import (
    InMemoryAccountQueries,
    InMemoryInvitations,
    InMemoryLoginThrottle,
    InMemoryPasswordResets,
    InMemorySessions,
    InMemoryUsers,
    PlainTextPasswordHasher,
    SequentialSessionTokens,
)
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.infrastructure.in_memory import (
    FixedClock,
    InMemoryTransactionRunner,
    RecordingEmailSender,
    RecordingEventPublisher,
)
from fragancia_api.shared.kernel import DomainEvent, Ok

PASSWORD = "correct horse battery"

POLICY = AuthPolicy(
    session_idle=timedelta(minutes=120),
    session_max_age=timedelta(hours=12),
    throttle_window=timedelta(minutes=15),
    email_max_attempts=5,
    ip_max_attempts=8,
)

LINKS = AccountLinks(
    admin_web_url="http://admin.test",
    invitation_ttl=timedelta(hours=72),
    reset_ttl=timedelta(minutes=60),
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
    invitations: InMemoryInvitations = field(default_factory=InMemoryInvitations)
    resets: InMemoryPasswordResets = field(default_factory=InMemoryPasswordResets)
    events: RecordingEventPublisher = field(default_factory=RecordingEventPublisher)
    email: RecordingEmailSender = field(default_factory=RecordingEmailSender)
    links: AccountLinks = LINKS

    def __post_init__(self) -> None:
        queries = InMemoryAccountQueries(self.users, self.sessions, self.invitations)
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
            throttle=self.throttle,
            hasher=self.hasher,
            transactions=transactions,
            clock=clock,
            policy=self.policy,
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
        self.invite_user = InviteUser(
            users=self.users,
            invitations=self.invitations,
            transactions=transactions,
            events=self.events,
            clock=clock,
            links=self.links,
        )
        self.revoke_invitation = RevokeInvitation(
            invitations=self.invitations, transactions=transactions, clock=clock
        )
        self.accept_invitation = AcceptInvitation(
            users=self.users,
            invitations=self.invitations,
            hasher=self.hasher,
            tokens=self.tokens,
            transactions=transactions,
            clock=clock,
        )
        self.request_reset = RequestPasswordReset(
            users=self.users,
            resets=self.resets,
            throttle=self.throttle,
            transactions=transactions,
            events=self.events,
            clock=clock,
            policy=self.policy,
            links=self.links,
        )
        self.reset_password = ResetPassword(
            users=self.users,
            sessions=self.sessions,
            resets=self.resets,
            hasher=self.hasher,
            tokens=self.tokens,
            transactions=transactions,
            clock=clock,
        )
        self.deactivate_user = DeactivateUser(
            users=self.users,
            sessions=self.sessions,
            resets=self.resets,
            transactions=transactions,
            clock=clock,
        )
        self.reactivate_user = ReactivateUser(users=self.users, transactions=transactions)
        self.list_users = ListUsers(queries)
        self.list_pending_invitations = ListPendingInvitations(queries, clock=clock)
        self.send_invitation_email = SendInvitationEmail(
            invitations=self.invitations,
            tokens=self.tokens,
            email=self.email,
            transactions=transactions,
            clock=clock,
            links=self.links,
        )
        self.send_reset_email = SendPasswordResetEmail(
            resets=self.resets,
            users=self.users,
            tokens=self.tokens,
            email=self.email,
            transactions=transactions,
            clock=clock,
            links=self.links,
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
            self.hasher.encode(password),
            now=self.clock.now(),
        )
        user.is_active = active
        self.users.by_id[user.id] = user
        return user

    def message_of(self, event: DomainEvent) -> EventMessage:
        """What the outbox relay would hand a subscriber for this event."""
        return EventMessage(
            id=event.event_id,
            name=event.name,
            payload=event.payload(),
            occurred_at=event.occurred_at,
        )

    async def sign_in(
        self, email: str = "owner@example.test", password: str = PASSWORD
    ) -> LoginResult:
        result = await self.log_in.execute(email, password, ip="203.0.113.7", user_agent="tests")
        assert isinstance(result, Ok), result
        return result.value


@pytest.fixture
def identity() -> Identity:
    return Identity()
