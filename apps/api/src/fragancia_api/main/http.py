"""HTTP entrypoint: `uvicorn fragancia_api.main.http:create_app --factory`."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fragancia_api.config import Settings
from fragancia_api.container import Container, build_container
from fragancia_api.shared.http import health
from fragancia_api.shared.http.errors import install_error_handlers
from fragancia_api.shared.http.middleware import RequestContextMiddleware
from fragancia_api.shared.http.openapi import install_openapi, operation_id
from fragancia_api.shared.http.reference import reference_router
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.logging import configure_logging

API_PREFIX = "/api/v1"
TITLE = "La Fragancia Ideal API"
DESCRIPTION = (
    "Online perfume store and back office. Admin routes (`/api/v1/admin/*`) need the "
    "`fragancia_session` session cookie set by `POST /api/v1/auth/login`. Errors always have "
    "the shape `{code, message, details?}`."
)


def create_app(container: Container | None = None) -> FastAPI:
    """Production factory: settings from the environment, real adapters."""
    if container is None:
        settings = Settings()  # values come from the environment
        configure_logging(settings.log_level, json=settings.app_env != "development")
        container = build_container(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await container.close()

    return build_app(
        container.services,
        container.routers,
        docs=not container.settings.is_production,
        cors_origins=container.settings.cors_origin_list,
        lifespan=lifespan,
    )


def build_app(
    services: ServiceRegistry,
    routers: Sequence[APIRouter],
    *,
    docs: bool = True,
    cors_origins: Sequence[str] = (),
    lifespan: object = None,
) -> FastAPI:
    """Assemble the FastAPI app from a service registry; tests call it with in-memory adapters."""
    openapi_url = f"{API_PREFIX}/openapi.json"
    app = FastAPI(
        title=TITLE,
        version="1.0.0",
        docs_url=None,  # Scalar replaces Swagger UI (reference_router below)
        redoc_url=None,
        openapi_url=openapi_url if docs else None,
        generate_unique_id_function=operation_id,
        lifespan=lifespan,  # type: ignore[arg-type]
    )
    install_openapi(app, description=DESCRIPTION)
    app.state.services = services
    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID"],
        )
    if docs:
        app.include_router(
            reference_router(openapi_url=openapi_url, title=TITLE), prefix=API_PREFIX
        )
    app.include_router(health.router, prefix=API_PREFIX)
    for router in routers:
        app.include_router(router, prefix=API_PREFIX)
    return app
