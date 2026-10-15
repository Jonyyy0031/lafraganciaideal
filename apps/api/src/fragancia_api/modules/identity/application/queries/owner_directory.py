"""Public facade of identity for other modules: who the active owners are."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fragancia_api.modules.identity.application.ports import OwnerDirectoryQueries


@dataclass(frozen=True, slots=True)
class OwnerContact:
    """How to address one owner."""

    name: str
    email: str


class OwnerDirectory:
    """Query facade: the only way other modules read owners from identity."""

    def __init__(self, queries: OwnerDirectoryQueries) -> None:
        self._queries = queries

    async def active_owners(self) -> list[OwnerContact]:
        """The active users with the owner role, ordered by name."""
        return await self._queries.active_owners()
