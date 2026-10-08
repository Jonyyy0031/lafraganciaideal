from uuid import UUID

from fragancia_api.modules.catalog.domain.errors import BrandNotFound
from fragancia_api.modules.catalog.domain.repositories import BrandRepository
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Result


class ArchiveBrand:
    """Command: hide a brand from the storefront. Idempotent."""

    def __init__(self, *, brands: BrandRepository, transactions: TransactionRunner) -> None:
        self._brands = brands
        self._transactions = transactions

    async def execute(self, brand_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            brand = await self._brands.get_for_update(brand_id)
            if brand is None:
                return Err(BrandNotFound())
            brand.archive()
            return await self._brands.save(brand)

        return await self._transactions.run(work)


class RestoreBrand:
    """Command: show an archived brand in the storefront again. Idempotent."""

    def __init__(self, *, brands: BrandRepository, transactions: TransactionRunner) -> None:
        self._brands = brands
        self._transactions = transactions

    async def execute(self, brand_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            brand = await self._brands.get_for_update(brand_id)
            if brand is None:
                return Err(BrandNotFound())
            brand.restore()
            return await self._brands.save(brand)

        return await self._transactions.run(work)
