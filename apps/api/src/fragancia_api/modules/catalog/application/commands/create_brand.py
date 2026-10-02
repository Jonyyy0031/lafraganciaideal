from uuid import UUID

from fragancia_api.modules.catalog.domain.brand import Brand, BrandName
from fragancia_api.modules.catalog.domain.errors import BrandAlreadyExists
from fragancia_api.modules.catalog.domain.repositories import BrandRepository
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.events import EventPublisher
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class CreateBrand:
    """Command: register a new, active brand. Publishes `catalog.brand.created`."""

    def __init__(
        self,
        *,
        brands: BrandRepository,
        transactions: TransactionRunner,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._brands = brands
        self._transactions = transactions
        self._events = events
        self._clock = clock

    async def execute(self, name: str) -> Result[UUID, DomainError]:
        async def work() -> Result[UUID, DomainError]:
            match BrandName.create(name):
                case Err(invalid):
                    return Err(invalid)
                case Ok(brand_name):
                    pass
            if await self._brands.exists_with_slug(brand_name.slug):
                return Err(BrandAlreadyExists())
            brand = Brand.create(brand_name, created_at=self._clock.now())
            match await self._brands.add(brand):  # also catches a concurrent duplicate
                case Err(conflict):
                    return Err(conflict)
            await self._events.publish(brand.pull_events())
            return Ok(brand.id)

        return await self._transactions.run(work)
