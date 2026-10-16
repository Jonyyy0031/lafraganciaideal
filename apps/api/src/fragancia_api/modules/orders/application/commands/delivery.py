from fragancia_api.modules.orders.contracts import DeliveryOptions
from fragancia_api.modules.orders.domain.delivery import DeliverySettings
from fragancia_api.modules.orders.domain.repositories import DeliverySettingsRepository
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class UpdateDeliveryOptions:
    """Command: replace the whole delivery configuration."""

    def __init__(
        self, *, repository: DeliverySettingsRepository, transactions: TransactionRunner
    ) -> None:
        self._repository = repository
        self._transactions = transactions

    async def execute(self, options: DeliveryOptions) -> Result[None, DomainError]:
        match DeliverySettings.create(
            home_enabled=options.home.enabled,
            home_price_cents=options.home.price_cents,
            home_free_from_cents=options.home.free_from_cents,
            local_enabled=options.local.enabled,
            local_price_cents=options.local.price_cents,
            local_postal_codes=options.local.postal_codes,
            pickup_enabled=options.pickup.enabled,
            pickup_address=options.pickup.address,
            pickup_hours=options.pickup.hours,
        ):
            case Err(invalid):
                return Err(invalid)
            case Ok(settings):
                pass

        async def work() -> Result[None, DomainError]:
            await self._repository.get_for_update()
            await self._repository.save(settings)
            return Ok(None)

        return await self._transactions.run(work)
