import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import Depends, Response
from pydantic import BaseModel

from fragancia_api.shared.http.access import public_router
from fragancia_api.shared.http.services import provide

CHECK_TIMEOUT_SECONDS = 2.0


class HealthChecks(dict[str, Callable[[], Awaitable[None]]]):
    """Readiness checks by name (e.g. "database", "valkey"). Each raises when unhealthy."""


class Liveness(BaseModel):
    status: Literal["ok"] = "ok"


class Readiness(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, Literal["ok", "error"]]


router = public_router(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> Liveness:
    """The process is up."""
    return Liveness()


@router.get("/ready", responses={503: {"model": Readiness}})
async def readiness(
    response: Response, checks: Annotated[HealthChecks, Depends(provide(HealthChecks))]
) -> Readiness:
    """The process can serve traffic: its dependencies answer."""
    results = await asyncio.gather(*(_run(check) for check in checks.values()))
    outcome = dict(zip(checks.keys(), results, strict=True))
    healthy = all(result == "ok" for result in outcome.values())
    if not healthy:
        response.status_code = 503
    return Readiness(status="ok" if healthy else "unavailable", checks=outcome)


async def _run(check: Callable[[], Awaitable[None]]) -> Literal["ok", "error"]:
    try:
        await asyncio.wait_for(check(), timeout=CHECK_TIMEOUT_SECONDS)
    except Exception:
        return "error"
    return "ok"
