from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request, Response

from fragancia_api.modules.identity.application.commands.change_password import ChangePassword
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
from fragancia_api.modules.identity.application.commands.revoke_sessions import (
    RevokeOtherSessions,
    RevokeSession,
)
from fragancia_api.modules.identity.application.commands.user_status import (
    DeactivateUser,
    ReactivateUser,
)
from fragancia_api.modules.identity.application.queries.my_account import (
    GetMyAccount,
    ListMySessions,
)
from fragancia_api.modules.identity.application.queries.team import (
    ListPendingInvitations,
    ListUsers,
)
from fragancia_api.modules.identity.contracts import (
    AcceptInvitationRequest,
    AdminInvitation,
    AdminMe,
    AdminSession,
    AdminUser,
    ChangePasswordRequest,
    InviteUserRequest,
    LoginRequest,
    LoginResponse,
    PasswordResetRequest,
    ResetPasswordRequest,
)
from fragancia_api.modules.identity.domain.user import USERS_MANAGE
from fragancia_api.modules.identity.http.cookies import SessionCookie
from fragancia_api.shared.http import (
    ErrorResponse,
    admin_router,
    provide,
    public_router,
    require_permission,
    unwrap,
)
from fragancia_api.shared.http.errors import AuthenticationRequired, Forbidden

public = public_router(prefix="/auth", tags=["identity"])
admin = admin_router(prefix="/auth", tags=["identity · admin"])


def current_session(request: Request) -> UUID:
    """The session of the request (set by `require_admin`). Only a test resolver leaves it
    empty, which these session-only routes refuse."""
    session_id: UUID | None = request.state.actor.session_id
    if session_id is None:
        raise Forbidden
    return session_id


def current_user(request: Request) -> UUID:
    """The signed-in user's id (set by `require_admin`). Only a test resolver gives an id that
    is not a user's UUID, which these routes refuse."""
    try:
        return UUID(request.state.actor.id)
    except ValueError:
        raise Forbidden from None


def _found(me: AdminMe | None) -> AdminMe:
    if me is None:  # the user behind a resolved session always exists
        raise AuthenticationRequired
    return me


@public.post(
    "/login",
    responses={
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
)
async def log_in(
    body: LoginRequest,
    request: Request,
    response: Response,
    use_case: Annotated[LogIn, Depends(provide(LogIn))],
    account: Annotated[GetMyAccount, Depends(provide(GetMyAccount))],
    cookie: Annotated[SessionCookie, Depends(provide(SessionCookie))],
) -> LoginResponse:
    """Sign in to the back office. Sets the httpOnly session cookie; the token is never in the
    body. 401 `IDENTITY_INVALID_CREDENTIALS` for a wrong email or password, 429
    `IDENTITY_TOO_MANY_ATTEMPTS` when the email or IP made too many attempts."""
    result = unwrap(
        await use_case.execute(
            body.email,
            body.password,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    )
    cookie.set(response, result.token)
    me = _found(await account.execute(result.user_id))
    return LoginResponse(user=me, expires_at=result.expires_at)


@admin.post("/logout", status_code=204)
async def log_out(
    response: Response,
    session_id: Annotated[UUID, Depends(current_session)],
    use_case: Annotated[LogOut, Depends(provide(LogOut))],
    cookie: Annotated[SessionCookie, Depends(provide(SessionCookie))],
) -> None:
    """Sign out: revoke the current session and clear the cookie."""
    unwrap(await use_case.execute(session_id))
    cookie.clear(response)


@admin.get("/me")
async def get_my_account(
    user_id: Annotated[UUID, Depends(current_user)],
    _: Annotated[UUID, Depends(current_session)],
    use_case: Annotated[GetMyAccount, Depends(provide(GetMyAccount))],
) -> AdminMe:
    """The signed-in user with their role and permissions."""
    return _found(await use_case.execute(user_id))


@admin.put(
    "/password",
    status_code=204,
    responses={422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
async def change_my_password(
    body: ChangePasswordRequest,
    user_id: Annotated[UUID, Depends(current_user)],
    session_id: Annotated[UUID, Depends(current_session)],
    use_case: Annotated[ChangePassword, Depends(provide(ChangePassword))],
) -> None:
    """Change my password; every other session of mine is closed. 401
    `IDENTITY_ACTOR_INACTIVE` if I was deactivated meanwhile, 422
    `IDENTITY_PASSWORD_TOO_WEAK` if the new password breaks the rules, 422
    `IDENTITY_CURRENT_PASSWORD_WRONG` if the current one is not correct, 429
    `IDENTITY_TOO_MANY_ATTEMPTS` after too many wrong current passwords."""
    unwrap(await use_case.execute(user_id, session_id, body.current_password, body.new_password))


@admin.get("/sessions")
async def list_my_sessions(
    user_id: Annotated[UUID, Depends(current_user)],
    session_id: Annotated[UUID, Depends(current_session)],
    use_case: Annotated[ListMySessions, Depends(provide(ListMySessions))],
) -> list[AdminSession]:
    """My open sessions, most recently seen first; `current` marks this one."""
    return await use_case.execute(user_id, current_session_id=session_id)


@admin.delete("/sessions/{session_id}", status_code=204, responses={404: {"model": ErrorResponse}})
async def close_my_session(
    session_id: UUID,
    user_id: Annotated[UUID, Depends(current_user)],
    _: Annotated[UUID, Depends(current_session)],
    use_case: Annotated[RevokeSession, Depends(provide(RevokeSession))],
) -> None:
    """Close one of my sessions. 404 `IDENTITY_SESSION_NOT_FOUND` if it is not an open
    session of mine."""
    unwrap(await use_case.execute(user_id, session_id))


@admin.delete("/sessions", status_code=204)
async def close_my_other_sessions(
    user_id: Annotated[UUID, Depends(current_user)],
    session_id: Annotated[UUID, Depends(current_session)],
    use_case: Annotated[RevokeOtherSessions, Depends(provide(RevokeOtherSessions))],
) -> None:
    """Close every session of mine except the current one."""
    unwrap(await use_case.execute(user_id, session_id))


@public.post(
    "/invitations/accept",
    status_code=204,
    responses={
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
async def accept_invitation(
    body: AcceptInvitationRequest,
    use_case: Annotated[AcceptInvitation, Depends(provide(AcceptInvitation))],
) -> None:
    """Create the staff account of an emailed invitation link. 422 `IDENTITY_LINK_INVALID` for
    an unknown, used, revoked or expired link, 422 `IDENTITY_PASSWORD_TOO_WEAK`, 409
    `IDENTITY_EMAIL_TAKEN`. The web app then sends the user to the login page."""
    unwrap(await use_case.execute(body.token, body.password))


@public.post(
    "/password-reset",
    status_code=202,
    response_class=Response,
    responses={429: {"model": ErrorResponse}},
)
async def request_password_reset(
    body: PasswordResetRequest,
    request: Request,
    use_case: Annotated[RequestPasswordReset, Depends(provide(RequestPasswordReset))],
) -> Response:
    """Ask for a password-reset email. Always 202 with no body, whether or not the account
    exists. 429 `IDENTITY_TOO_MANY_ATTEMPTS` when the email or IP asked too often."""
    unwrap(await use_case.execute(body.email, ip=request.client.host if request.client else None))
    return Response(status_code=202)


@public.post(
    "/password-reset/confirm",
    status_code=204,
    responses={422: {"model": ErrorResponse}},
)
async def reset_password(
    body: ResetPasswordRequest,
    use_case: Annotated[ResetPassword, Depends(provide(ResetPassword))],
) -> None:
    """Set a new password with an emailed link; closes every session of the user. 422
    `IDENTITY_LINK_INVALID`, 422 `IDENTITY_PASSWORD_TOO_WEAK`."""
    unwrap(await use_case.execute(body.token, body.new_password))


team = admin_router(
    tags=["identity · users"],
    dependencies=[Depends(require_permission(USERS_MANAGE))],
)


@team.get("/users")
async def list_users(
    _: Annotated[UUID, Depends(current_session)],
    use_case: Annotated[ListUsers, Depends(provide(ListUsers))],
) -> list[AdminUser]:
    """Every back-office user. Needs `users:manage`."""
    return await use_case.execute()


@team.post(
    "/users/{user_id}/deactivate",
    status_code=204,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
async def deactivate_user(
    user_id: UUID,
    actor_id: Annotated[UUID, Depends(current_user)],
    _: Annotated[UUID, Depends(current_session)],
    use_case: Annotated[DeactivateUser, Depends(provide(DeactivateUser))],
) -> None:
    """Deactivate a user and close all their sessions. 401 `IDENTITY_ACTOR_INACTIVE` (the caller
    was deactivated meanwhile), 404 `IDENTITY_USER_NOT_FOUND`, 422
    `IDENTITY_CANNOT_DEACTIVATE_SELF`."""
    unwrap(await use_case.execute(actor_id, user_id))


@team.post(
    "/users/{user_id}/reactivate",
    status_code=204,
    responses={404: {"model": ErrorResponse}},
)
async def reactivate_user(
    user_id: UUID,
    _: Annotated[UUID, Depends(current_session)],
    use_case: Annotated[ReactivateUser, Depends(provide(ReactivateUser))],
) -> None:
    """Let a deactivated user sign in again. 404 `IDENTITY_USER_NOT_FOUND`."""
    unwrap(await use_case.execute(user_id))


@team.get("/invitations")
async def list_invitations(
    _: Annotated[UUID, Depends(current_session)],
    use_case: Annotated[ListPendingInvitations, Depends(provide(ListPendingInvitations))],
) -> list[AdminInvitation]:
    """Pending invitations, newest first."""
    return await use_case.execute()


@team.post(
    "/invitations",
    status_code=201,
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def invite_user(
    body: InviteUserRequest,
    user_id: Annotated[UUID, Depends(current_user)],
    _: Annotated[UUID, Depends(current_session)],
    use_case: Annotated[InviteUser, Depends(provide(InviteUser))],
    pending: Annotated[ListPendingInvitations, Depends(provide(ListPendingInvitations))],
) -> AdminInvitation:
    """Invite someone to become staff; the email is sent by the worker. 409
    `IDENTITY_EMAIL_TAKEN`, 422 `IDENTITY_EMAIL_INVALID` / `IDENTITY_NAME_INVALID`. Inviting the
    same email again revokes the earlier invitation."""
    invitation_id = unwrap(await use_case.execute(user_id, body.email, body.name))
    for invitation in await pending.execute():
        if invitation.id == invitation_id:
            return invitation
    raise RuntimeError("The invitation just created is not pending")


@team.delete(
    "/invitations/{invitation_id}",
    status_code=204,
    responses={404: {"model": ErrorResponse}},
)
async def revoke_invitation(
    invitation_id: UUID,
    _: Annotated[UUID, Depends(current_session)],
    use_case: Annotated[RevokeInvitation, Depends(provide(RevokeInvitation))],
) -> None:
    """Cancel a pending invitation. 404 `IDENTITY_INVITATION_NOT_FOUND`."""
    unwrap(await use_case.execute(invitation_id))


routers = (public, admin, team)
