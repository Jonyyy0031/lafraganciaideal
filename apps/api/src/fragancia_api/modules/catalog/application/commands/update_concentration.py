from uuid import UUID

from fragancia_api.modules.catalog.domain.concentration import Abbreviation, ConcentrationName
from fragancia_api.modules.catalog.domain.errors import (
    ConcentrationAbbreviationTaken,
    ConcentrationAlreadyExists,
    ConcentrationNotFound,
)
from fragancia_api.modules.catalog.domain.repositories import ConcentrationRepository
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class UpdateConcentration:
    """Command: change name and abbreviation together. Their slugs follow; another
    concentration's slug is a conflict."""

    def __init__(
        self, *, concentrations: ConcentrationRepository, transactions: TransactionRunner
    ) -> None:
        self._concentrations = concentrations
        self._transactions = transactions

    async def execute(
        self, concentration_id: UUID, name: str, abbreviation: str
    ) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
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
            concentration = await self._concentrations.get_for_update(concentration_id)
            if concentration is None:
                return Err(ConcentrationNotFound())
            if await self._concentrations.exists_with_slug(
                concentration_name.slug, except_id=concentration_id
            ):
                return Err(ConcentrationAlreadyExists())
            if await self._concentrations.exists_with_abbreviation(
                concentration_abbreviation.slug, except_id=concentration_id
            ):
                return Err(ConcentrationAbbreviationTaken())
            concentration.update(concentration_name, concentration_abbreviation)
            return await self._concentrations.save(concentration)  # also catches a concurrent one

        return await self._transactions.run(work)
