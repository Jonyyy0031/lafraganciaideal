"""Wiring of the identity module for the composition root."""

from datetime import timedelta

from fragancia_api.modules.identity.application.commands.change_password import ChangePassword
from fragancia_api.modules.identity.application.commands.create_user import CreateUser
from fragancia_api.modules.identity.application.commands.log_in import LogIn
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
from fragancia_api.modules.identity.http.cookies import SessionCookie
from fragancia_api.modules.identity.http.router import routers
from fragancia_api.modules.identity.infrastructure.argon2_password_hasher import (
    Argon2PasswordHasher,
)
from fragancia_api.modules.identity.infrastructure.secure_session_tokens import (
    SecureSessionTokens,
)
from fragancia_api.modules.identity.infrastructure.sql_account_queries import SqlAccountQueries
from fragancia_api.modules.identity.infrastructure.sql_login_throttle import SqlLoginThrottle
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
    users = SqlUserRepository(platform.database)
    sessions = SqlSessionRepository(platform.database)
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
