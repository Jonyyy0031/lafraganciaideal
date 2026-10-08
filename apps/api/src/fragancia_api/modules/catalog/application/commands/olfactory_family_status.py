from uuid import UUID

from fragancia_api.modules.catalog.domain.errors import FamilyNotFound
from fragancia_api.modules.catalog.domain.repositories import OlfactoryFamilyRepository
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Result


class ArchiveOlfactoryFamily:
    """Command: hide a family from the storefront. Idempotent."""

    def __init__(
        self, *, families: OlfactoryFamilyRepository, transactions: TransactionRunner
    ) -> None:
        self._families = families
        self._transactions = transactions

    async def execute(self, family_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            family = await self._families.get_for_update(family_id)
            if family is None:
                return Err(FamilyNotFound())
            family.archive()
            return await self._families.save(family)

        return await self._transactions.run(work)


class RestoreOlfactoryFamily:
    """Command: show an archived family in the storefront again. Idempotent."""

    def __init__(
        self, *, families: OlfactoryFamilyRepository, transactions: TransactionRunner
    ) -> None:
        self._families = families
        self._transactions = transactions

    async def execute(self, family_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            family = await self._families.get_for_update(family_id)
            if family is None:
                return Err(FamilyNotFound())
            family.restore()
            return await self._families.save(family)

        return await self._transactions.run(work)
