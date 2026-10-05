from fragancia_api.container import build_container
from fragancia_api.modules.identity.application.commands.resolve_session_actor import (
    ResolveSessionActor,
)
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.http.health import HealthChecks
from tests.support import make_settings


async def test_container_wires_the_platform_services_without_connecting() -> None:
    container = build_container(make_settings())

    assert isinstance(container.services.get(ActorResolver), ResolveSessionActor)  # type: ignore[type-abstract]
    assert TransactionRunner in container.services
    assert set(container.services.get(HealthChecks)) == {"database", "valkey"}
    await container.close()
