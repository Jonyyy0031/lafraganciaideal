from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is making the request."""

    id: str
    is_admin: bool


class ActorResolver(Protocol):
    """Resolves a bearer token to an actor, or None when the token is unknown."""

    async def resolve(self, token: str) -> Actor | None: ...
