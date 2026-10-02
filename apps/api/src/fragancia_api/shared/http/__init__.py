"""HTTP building blocks every module's router uses."""

from fragancia_api.shared.http.access import admin_router, public_router, require_admin
from fragancia_api.shared.http.errors import ErrorResponse, unwrap
from fragancia_api.shared.http.services import provide

__all__ = [
    "ErrorResponse",
    "admin_router",
    "provide",
    "public_router",
    "require_admin",
    "unwrap",
]
