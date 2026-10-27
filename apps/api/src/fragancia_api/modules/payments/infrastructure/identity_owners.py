"""Anti-corruption layer: payments' `OwnerContacts` over identity's public facade."""

from fragancia_api.modules.identity import OwnerDirectory
from fragancia_api.modules.payments.application.ports import OwnerContact


class IdentityOwners:
    def __init__(self, directory: OwnerDirectory) -> None:
        self._directory = directory

    async def active_owners(self) -> list[OwnerContact]:
        return [
            OwnerContact(name=owner.name, email=owner.email)
            for owner in await self._directory.active_owners()
        ]
