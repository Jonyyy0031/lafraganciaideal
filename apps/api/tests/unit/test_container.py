from fragancia_api.container import build_container
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.infrastructure.dev_token_actor_resolver import DevTokenActorResolver
from tests.support import ADMIN_TOKEN, make_settings


async def test_container_wires_the_platform_services_without_connecting() -> None:
    container = build_container(make_settings())

    assert ActorResolver in container.services
    assert TransactionRunner in container.services
    assert set(container.services.get(HealthChecks)) == {"database", "valkey"}
    await container.close()


async def test_dev_token_resolver() -> None:
    resolver = DevTokenActorResolver(ADMIN_TOKEN)
    actor = await resolver.resolve(ADMIN_TOKEN)
    assert actor is not None and actor.is_admin
    assert await resolver.resolve("other") is None
    assert await DevTokenActorResolver(None).resolve("") is None
