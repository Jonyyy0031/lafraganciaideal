from uuid import UUID

from fragancia_api.modules.catalog.domain.brand import BrandName
from fragancia_api.modules.catalog.domain.errors import BrandAlreadyExists, BrandNotFound
from fragancia_api.modules.catalog.domain.repositories import BrandRepository
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class RenameBrand:
    """Command: rename a brand. Its slug follows the name; another brand's slug is a conflict."""

    def __init__(self, *, brands: BrandRepository, transactions: TransactionRunner) -> None:
        self._brands = brands
        self._transactions = transactions

    async def execute(self, brand_id: UUID, name: str) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            match BrandName.create(name):
                case Err(invalid):
                    return Err(invalid)
                case Ok(brand_name):
                    pass
            brand = await self._brands.get_for_update(brand_id)
            if brand is None:
                return Err(BrandNotFound())
            if await self._brands.exists_with_slug(brand_name.slug, except_id=brand_id):
                return Err(BrandAlreadyExists())
            brand.rename(brand_name)
            return await self._brands.save(brand)  # also catches a concurrent rename

        return await self._transactions.run(work)
