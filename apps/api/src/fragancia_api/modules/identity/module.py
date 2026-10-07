"""Wiring of the identity module for the composition root."""

from datetime import timedelta

from fragancia_api.modules.identity.application.commands.change_password import ChangePassword
from fragancia_api.modules.identity.application.commands.create_user import CreateUser
from fragancia_api.modules.identity.application.commands.invitations import (
    AcceptInvitation,
    InviteUser,
    RevokeInvitation,
)
from fragancia_api.modules.identity.application.commands.log_in import LogIn
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
from fragancia_api.modules.identity.domain.events import InvitationIssued, PasswordResetRequested
from fragancia_api.modules.identity.http.cookies import SessionCookie
from fragancia_api.modules.identity.http.router import routers
from fragancia_api.modules.identity.infrastructure.argon2_password_hasher import (
    Argon2PasswordHasher,
)
from fragancia_api.modules.identity.infrastructure.secure_session_tokens import (
    SecureSessionTokens,
)
from fragancia_api.modules.identity.infrastructure.sql_account_queries import SqlAccountQueries
from fragancia_api.modules.identity.infrastructure.sql_invitation_repository import (
    SqlInvitationRepository,
)
from fragancia_api.modules.identity.infrastructure.sql_login_throttle import SqlLoginThrottle
from fragancia_api.modules.identity.infrastructure.sql_password_reset_repository import (
    SqlPasswordResetRepository,
)
from fragancia_api.modules.identity.infrastructure.sql_session_repository import (
    SqlSessionRepository,
)
from fragancia_api.modules.identity.infrastructure.sql_user_repository import SqlUserRepository
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.module import AppModule, Platform


def register(platform: Platform, services: ServiceRegistry) -> None:
    settings = platform.settings
    policy = AuthPolicy(
        session_idle=timedelta(minutes=settings.session_idle_minutes),
        session_max_age=timedelta(hours=settings.session_max_hours),
        throttle_window=timedelta(minutes=settings.login_window_minutes),
        email_max_attempts=settings.login_email_max_attempts,
        ip_max_attempts=settings.login_ip_max_attempts,
    )
    links = AccountLinks(
        admin_web_url=settings.admin_web_url,
        invitation_ttl=timedelta(hours=settings.invitation_ttl_hours),
        reset_ttl=timedelta(minutes=settings.password_reset_ttl_minutes),
    )
    users = SqlUserRepository(platform.database)
    sessions = SqlSessionRepository(platform.database)
    invitations = SqlInvitationRepository(platform.database)
    resets = SqlPasswordResetRepository(platform.database)
    queries = SqlAccountQueries(platform.database)
    throttle = SqlLoginThrottle(platform.database)
    hasher = Argon2PasswordHasher()
    tokens = SecureSessionTokens()
    transactions = platform.transactions
    clock = platform.clock

    services.add(
        CreateUser,
        CreateUser(users=users, hasher=hasher, transactions=transactions, clock=clock),
    )
    services.add(
        LogIn,
        LogIn(
            users=users,
            sessions=sessions,
            throttle=throttle,
            hasher=hasher,
            tokens=tokens,
            transactions=transactions,
            clock=clock,
            policy=policy,
        ),
    )
    services.add(LogOut, LogOut(sessions=sessions, transactions=transactions, clock=clock))
    services.add(
        ChangePassword,
        ChangePassword(
            users=users,
            sessions=sessions,
            throttle=throttle,
            hasher=hasher,
            transactions=transactions,
            clock=clock,
            policy=policy,
        ),
    )
    services.add(
        RevokeSession, RevokeSession(sessions=sessions, transactions=transactions, clock=clock)
    )
    services.add(
        RevokeOtherSessions,
        RevokeOtherSessions(sessions=sessions, transactions=transactions, clock=clock),
    )
    services.add(
        InviteUser,
        InviteUser(
            users=users,
            invitations=invitations,
            transactions=transactions,
            events=platform.events,
            clock=clock,
            links=links,
        ),
    )
    services.add(
        RevokeInvitation,
        RevokeInvitation(invitations=invitations, transactions=transactions, clock=clock),
    )
    services.add(
        AcceptInvitation,
        AcceptInvitation(
            users=users,
            invitations=invitations,
            hasher=hasher,
            tokens=tokens,
            transactions=transactions,
            clock=clock,
        ),
    )
    services.add(
        RequestPasswordReset,
        RequestPasswordReset(
            users=users,
            resets=resets,
            throttle=throttle,
            transactions=transactions,
            events=platform.events,
            clock=clock,
            policy=policy,
            links=links,
        ),
    )
    services.add(
        ResetPassword,
        ResetPassword(
            users=users,
            sessions=sessions,
            resets=resets,
            hasher=hasher,
            tokens=tokens,
            transactions=transactions,
            clock=clock,
        ),
    )
    services.add(
        DeactivateUser,
        DeactivateUser(
            users=users,
            sessions=sessions,
            resets=resets,
            transactions=transactions,
            clock=clock,
        ),
    )
    services.add(ReactivateUser, ReactivateUser(users=users, transactions=transactions))
    services.add(ListUsers, ListUsers(queries))
    services.add(ListPendingInvitations, ListPendingInvitations(queries, clock=clock))
    platform.subscriptions.subscribe(
        InvitationIssued.name,
        SendInvitationEmail(
            invitations=invitations,
            tokens=tokens,
            email=platform.email,
            transactions=transactions,
            clock=clock,
            links=links,
        ),
    )
    platform.subscriptions.subscribe(
        PasswordResetRequested.name,
        SendPasswordResetEmail(
            resets=resets,
            users=users,
            tokens=tokens,
            email=platform.email,
            transactions=transactions,
            clock=clock,
            links=links,
        ),
    )
    services.add(GetMyAccount, GetMyAccount(queries))
    services.add(ListMySessions, ListMySessions(queries, clock=clock, policy=policy))
    services.add(
        SessionCookie,
        SessionCookie(
            secure=settings.is_production,
            max_age_seconds=settings.session_max_hours * 3600,
        ),
    )
    services.add(
        ActorResolver,  # type: ignore[type-abstract]
        ResolveSessionActor(
            sessions=sessions,
            users=users,
            tokens=tokens,
            transactions=transactions,
            clock=clock,
            policy=policy,
        ),
    )


module = AppModule(name="identity", register=register, routers=routers)
