from uuid import UUID

from fragancia_api.modules.catalog.domain.errors import ConcentrationNotFound
from fragancia_api.modules.catalog.domain.repositories import ConcentrationRepository
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Result


class ArchiveConcentration:
    """Command: hide a concentration from the storefront. Idempotent."""

    def __init__(
        self, *, concentrations: ConcentrationRepository, transactions: TransactionRunner
    ) -> None:
        self._concentrations = concentrations
        self._transactions = transactions

    async def execute(self, concentration_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            concentration = await self._concentrations.get_for_update(concentration_id)
            if concentration is None:
                return Err(ConcentrationNotFound())
            concentration.archive()
            return await self._concentrations.save(concentration)

        return await self._transactions.run(work)


class RestoreConcentration:
    """Command: show an archived concentration in the storefront again. Idempotent."""

    def __init__(
        self, *, concentrations: ConcentrationRepository, transactions: TransactionRunner
    ) -> None:
        self._concentrations = concentrations
        self._transactions = transactions

    async def execute(self, concentration_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            concentration = await self._concentrations.get_for_update(concentration_id)
            if concentration is None:
                return Err(ConcentrationNotFound())
            concentration.restore()
            return await self._concentrations.save(concentration)

        return await self._transactions.run(work)
