from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is making the request.

    `session_id` is the session the request came with. The identity resolver always sets it;
    `None` exists only for test resolvers. `permissions` are the user's permission names; the
    identity resolver sets them.
    """

    id: str
    is_admin: bool
    session_id: UUID | None = None
    permissions: frozenset[str] = frozenset()


class ActorResolver(Protocol):
    """Resolves a session token to an actor, or None when the token is unknown."""

    async def resolve(self, token: str) -> Actor | None: ...
