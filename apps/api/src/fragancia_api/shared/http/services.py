"""How routers get their use cases: a registry keyed by type, stored on the app.

    @router.post("")
    async def create(use_case: Annotated[CreateBrand, Depends(provide(CreateBrand))]): ...

Tests build the registry with in-memory adapters instead of overriding dependencies.
"""

from collections.abc import Callable
from typing import Any, cast

from fastapi import Request


class ServiceRegistry:
    def __init__(self) -> None:
        self._services: dict[type, object] = {}

    def add[T](self, service_type: type[T], service: T) -> None:
        if service_type in self._services:
            raise ValueError(f"{service_type.__name__} is already registered")
        self._services[service_type] = service

    def get[T](self, service_type: type[T]) -> T:
        try:
            return cast(T, self._services[service_type])
        except KeyError:
            raise LookupError(f"{service_type.__name__} is not registered") from None

    def __contains__(self, service_type: Any) -> bool:
        return service_type in self._services


def provide[T](service_type: type[T]) -> Callable[[Request], T]:
    def dependency(request: Request) -> T:
        registry: ServiceRegistry = request.app.state.services
        return registry.get(service_type)

    dependency.__name__ = f"provide_{service_type.__name__}"
    return dependency
