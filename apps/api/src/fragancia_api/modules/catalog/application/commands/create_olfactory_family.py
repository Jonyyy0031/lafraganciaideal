from uuid import UUID

from fragancia_api.modules.catalog.domain.errors import FamilyAlreadyExists
from fragancia_api.modules.catalog.domain.olfactory_family import FamilyName, OlfactoryFamily
from fragancia_api.modules.catalog.domain.repositories import OlfactoryFamilyRepository
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class CreateOlfactoryFamily:
    """Command: register a new, active olfactory family. Publishes no event."""

    def __init__(
        self,
        *,
        families: OlfactoryFamilyRepository,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._families = families
        self._transactions = transactions
        self._clock = clock

    async def execute(self, name: str) -> Result[UUID, DomainError]:
        async def work() -> Result[UUID, DomainError]:
            match FamilyName.create(name):
                case Err(invalid):
                    return Err(invalid)
                case Ok(family_name):
                    pass
            if await self._families.exists_with_slug(family_name.slug):
                return Err(FamilyAlreadyExists())
            family = OlfactoryFamily.create(family_name, created_at=self._clock.now())
            match await self._families.add(family):  # also catches a concurrent duplicate
                case Err(conflict):
                    return Err(conflict)
            return Ok(family.id)

        return await self._transactions.run(work)
