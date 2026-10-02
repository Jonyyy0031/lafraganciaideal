"""Declared access: every route lives in a `public_router()` or an `admin_router()`.

Admin routers are mounted under `/admin` and require an admin actor resolved from the
`Authorization: Bearer <token>` header through the `ActorResolver` port. A test walks every
route to check that nothing under `/admin` escapes this dependency.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http.errors import AuthenticationRequired, Forbidden
from fragancia_api.shared.http.services import provide

_bearer = HTTPBearer(auto_error=False, description="Admin token")


async def require_admin(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    resolver: Annotated[ActorResolver, Depends(provide(ActorResolver))],
) -> Actor:
    if credentials is None:
        raise AuthenticationRequired
    actor = await resolver.resolve(credentials.credentials)
    if actor is None:
        raise AuthenticationRequired
    if not actor.is_admin:
        raise Forbidden
    request.state.actor = actor
    return actor


def public_router(**kwargs: Any) -> APIRouter:
    """Routes anyone can call (the storefront)."""
    return APIRouter(**kwargs)


def admin_router(*, prefix: str = "", **kwargs: Any) -> APIRouter:
    """Routes for the back office: mounted under /admin and restricted to admins."""
    dependencies = [Depends(require_admin), *kwargs.pop("dependencies", [])]
    return APIRouter(prefix=f"/admin{prefix}", dependencies=dependencies, **kwargs)
