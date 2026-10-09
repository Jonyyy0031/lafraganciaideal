from uuid import UUID

from fragancia_api.modules.catalog.domain.concentration import (
    Abbreviation,
    Concentration,
    ConcentrationName,
)
from fragancia_api.modules.catalog.domain.errors import (
    ConcentrationAbbreviationTaken,
    ConcentrationAlreadyExists,
)
from fragancia_api.modules.catalog.domain.repositories import ConcentrationRepository
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class CreateConcentration:
    """Command: register a new, active concentration. Publishes no event."""

    def __init__(
        self,
        *,
        concentrations: ConcentrationRepository,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._concentrations = concentrations
        self._transactions = transactions
        self._clock = clock

    async def execute(self, name: str, abbreviation: str) -> Result[UUID, DomainError]:
        async def work() -> Result[UUID, DomainError]:
            match ConcentrationName.create(name):
                case Err(invalid):
                    return Err(invalid)
                case Ok(concentration_name):
                    pass
            match Abbreviation.create(abbreviation):
                case Err(invalid_abbreviation):
                    return Err(invalid_abbreviation)
                case Ok(concentration_abbreviation):
                    pass
            if await self._concentrations.exists_with_slug(concentration_name.slug):
                return Err(ConcentrationAlreadyExists())
            if await self._concentrations.exists_with_abbreviation(concentration_abbreviation.slug):
                return Err(ConcentrationAbbreviationTaken())
            concentration = Concentration.create(
                concentration_name, concentration_abbreviation, created_at=self._clock.now()
            )
            match await self._concentrations.add(concentration):  # also catches a concurrent one
                case Err(conflict):
                    return Err(conflict)
            return Ok(concentration.id)

        return await self._transactions.run(work)
