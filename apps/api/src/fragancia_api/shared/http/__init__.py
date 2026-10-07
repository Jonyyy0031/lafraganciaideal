"""HTTP building blocks every module's router uses."""

from fragancia_api.shared.http.access import (
    SESSION_COOKIE,
    admin_router,
    public_router,
    require_admin,
    require_permission,
)
from fragancia_api.shared.http.errors import ErrorResponse, unwrap
from fragancia_api.shared.http.services import provide

__all__ = [
    "SESSION_COOKIE",
    "ErrorResponse",
    "admin_router",
    "provide",
    "public_router",
    "require_admin",
    "require_permission",
    "unwrap",
]
