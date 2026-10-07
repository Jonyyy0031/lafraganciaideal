"""Declared access: every route lives in a `public_router()` or an `admin_router()`.

Admin routers are mounted under `/admin` and require an admin actor resolved from the session
cookie (`fragancia_session`, set by `POST /api/v1/auth/login`) through the `ActorResolver`
port. A test walks every route to check that nothing under `/admin` escapes this dependency.
"""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.security import APIKeyCookie

from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http.errors import AuthenticationRequired, ErrorResponse, Forbidden
from fragancia_api.shared.http.services import provide

SESSION_COOKIE = "fragancia_session"

_cookie = APIKeyCookie(
    name=SESSION_COOKIE,
    auto_error=False,
    description="Session cookie set by POST /api/v1/auth/login",
)


async def require_admin(
    request: Request,
    token: Annotated[str | None, Depends(_cookie)],
    resolver: Annotated[ActorResolver, Depends(provide(ActorResolver))],
) -> Actor:
    if token is None:
        raise AuthenticationRequired
    actor = await resolver.resolve(token)
    if actor is None:
        raise AuthenticationRequired
    if not actor.is_admin:
        raise Forbidden
    request.state.actor = actor
    return actor


def require_permission(permission: str) -> Callable[[Request], None]:
    """Router dependency for admin routes that need a permission (after require_admin)."""

    def check(request: Request) -> None:
        if permission not in request.state.actor.permissions:
            raise Forbidden

    return check


def public_router(**kwargs: Any) -> APIRouter:
    """Routes anyone can call (the storefront)."""
    return APIRouter(**kwargs)


ADMIN_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or unknown session"},
    403: {
        "model": ErrorResponse,
        "description": "The actor is not an admin or lacks the permission",
    },
}


def admin_router(*, prefix: str = "", **kwargs: Any) -> APIRouter:
    """Routes for the back office: mounted under /admin and restricted to admins."""
    dependencies = [Depends(require_admin), *kwargs.pop("dependencies", [])]
    responses = {**ADMIN_RESPONSES, **kwargs.pop("responses", {})}
    return APIRouter(
        prefix=f"/admin{prefix}", dependencies=dependencies, responses=responses, **kwargs
    )
