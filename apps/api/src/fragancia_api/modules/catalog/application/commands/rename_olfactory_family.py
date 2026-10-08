from uuid import UUID

from fragancia_api.modules.catalog.domain.errors import FamilyAlreadyExists, FamilyNotFound
from fragancia_api.modules.catalog.domain.olfactory_family import FamilyName
from fragancia_api.modules.catalog.domain.repositories import OlfactoryFamilyRepository
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class RenameOlfactoryFamily:
    """Command: rename a family. Its slug follows the name; another family's slug is a conflict."""

    def __init__(
        self, *, families: OlfactoryFamilyRepository, transactions: TransactionRunner
    ) -> None:
        self._families = families
        self._transactions = transactions

    async def execute(self, family_id: UUID, name: str) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            match FamilyName.create(name):
                case Err(invalid):
                    return Err(invalid)
                case Ok(family_name):
                    pass
            family = await self._families.get_for_update(family_id)
            if family is None:
                return Err(FamilyNotFound())
            if await self._families.exists_with_slug(family_name.slug, except_id=family_id):
                return Err(FamilyAlreadyExists())
            family.rename(family_name)
            return await self._families.save(family)  # also catches a concurrent rename

        return await self._transactions.run(work)
