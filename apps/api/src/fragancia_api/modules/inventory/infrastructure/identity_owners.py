"""Anti-corruption layer: inventory's `OwnerRecipients` over identity's public facade."""

from fragancia_api.modules.identity import OwnerDirectory
from fragancia_api.modules.inventory.application.ports import Recipient


class IdentityOwners:
    def __init__(self, directory: OwnerDirectory) -> None:
        self._directory = directory

    async def active_owners(self) -> list[Recipient]:
        return [
            Recipient(name=owner.name, email=owner.email)
            for owner in await self._directory.active_owners()
        ]
