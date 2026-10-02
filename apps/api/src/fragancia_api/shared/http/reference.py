"""Interactive API reference (Scalar), served outside production only."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from scalar_fastapi import get_scalar_api_reference

# Pinned: update deliberately (https://www.npmjs.com/package/@scalar/api-reference).
SCALAR_JS_URL = "https://cdn.jsdelivr.net/npm/@scalar/api-reference@1.72.4"


def reference_router(*, openapi_url: str, title: str) -> APIRouter:
    router = APIRouter()

    @router.get("/docs", include_in_schema=False)
    async def api_reference() -> HTMLResponse:
        return get_scalar_api_reference(
            openapi_url=openapi_url,
            title=title,
            scalar_js_url=SCALAR_JS_URL,
            authentication={"preferredSecurityScheme": "HTTPBearer"},
            persist_auth=True,  # development convenience: the dev token survives reloads
        )

    return router
