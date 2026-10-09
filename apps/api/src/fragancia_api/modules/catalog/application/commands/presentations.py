"""Commands on the presentations of a perfume: add, update, archive and restore.

Presentations are saved with their perfume, whose row is locked for the whole command.
"""

from dataclasses import dataclass
from uuid import UUID

from fragancia_api.modules.catalog.contracts import PresentationRequest
from fragancia_api.modules.catalog.domain.errors import PerfumeNotFound
from fragancia_api.modules.catalog.domain.perfume import Availability, Ml, Price, Sale
from fragancia_api.modules.catalog.domain.repositories import PerfumeRepository
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


@dataclass(frozen=True, slots=True)
class _PresentationValues:
    ml: Ml
    price: Price
    sale: Sale | None
    availability: Availability


def _values(request: PresentationRequest) -> Result[_PresentationValues, DomainError]:
    """The validated values, in the order ml, price, sale, availability (first Err wins)."""
    match Ml.create(request.ml):
        case Err(invalid_ml):
            return Err(invalid_ml)
        case Ok(ml):
            pass
    match Price.create(request.price_cents):
        case Err(invalid_price):
            return Err(invalid_price)
        case Ok(price):
            pass
    match Sale.create(
        request.sale_price_cents, request.sale_starts_at, request.sale_ends_at, regular=price
    ):
        case Err(invalid_sale):
            return Err(invalid_sale)
        case Ok(sale):
            pass
    match Availability.create(
        request.availability, request.lead_time_min_days, request.lead_time_max_days
    ):
        case Err(invalid_availability):
            return Err(invalid_availability)
        case Ok(availability):
            pass
    return Ok(_PresentationValues(ml, price, sale, availability))


class AddPresentation:
    """Command: add a presentation (active) to a perfume. Its ml must be new in the perfume."""

    def __init__(
        self, *, perfumes: PerfumeRepository, transactions: TransactionRunner, clock: Clock
    ) -> None:
        self._perfumes = perfumes
        self._transactions = transactions
        self._clock = clock

    async def execute(
        self, perfume_id: UUID, request: PresentationRequest
    ) -> Result[UUID, DomainError]:
        async def work() -> Result[UUID, DomainError]:
            match _values(request):
                case Err(invalid):
                    return Err(invalid)
                case Ok(values):
                    pass
            perfume = await self._perfumes.get_for_update(perfume_id)
            if perfume is None:
                return Err(PerfumeNotFound())
            match perfume.add_presentation(
                values.ml,
                values.price,
                values.sale,
                values.availability,
                created_at=self._clock.now(),
            ):
                case Err(refused):
                    return Err(refused)
                case Ok(presentation_id):
                    pass
            match await self._perfumes.save(perfume):  # also catches a concurrent one
                case Err(conflict):
                    return Err(conflict)
            return Ok(presentation_id)

        return await self._transactions.run(work)


class UpdatePresentation:
    """Command: replace ml, price, sale and availability of a presentation."""

    def __init__(
        self, *, perfumes: PerfumeRepository, transactions: TransactionRunner, clock: Clock
    ) -> None:
        self._perfumes = perfumes
        self._transactions = transactions
        self._clock = clock

    async def execute(
        self, perfume_id: UUID, presentation_id: UUID, request: PresentationRequest
    ) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            match _values(request):
                case Err(invalid):
                    return Err(invalid)
                case Ok(values):
                    pass
            perfume = await self._perfumes.get_for_update(perfume_id)
            if perfume is None:
                return Err(PerfumeNotFound())
            match perfume.update_presentation(
                presentation_id,
                values.ml,
                values.price,
                values.sale,
                values.availability,
                now=self._clock.now(),
            ):
                case Err(refused):
                    return Err(refused)
            return await self._perfumes.save(perfume)  # also catches a concurrent one

        return await self._transactions.run(work)


class ArchivePresentation:
    """Command: stop selling a presentation. Idempotent; refused for the last active
    presentation of a published perfume."""

    def __init__(
        self, *, perfumes: PerfumeRepository, transactions: TransactionRunner, clock: Clock
    ) -> None:
        self._perfumes = perfumes
        self._transactions = transactions
        self._clock = clock

    async def execute(self, perfume_id: UUID, presentation_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            perfume = await self._perfumes.get_for_update(perfume_id)
            if perfume is None:
                return Err(PerfumeNotFound())
            match perfume.archive_presentation(presentation_id):
                case Err(refused):
                    return Err(refused)
            return await self._perfumes.save(perfume)

        return await self._transactions.run(work)


class RestorePresentation:
    """Command: sell an archived presentation again. Idempotent."""

    def __init__(
        self, *, perfumes: PerfumeRepository, transactions: TransactionRunner, clock: Clock
    ) -> None:
        self._perfumes = perfumes
        self._transactions = transactions
        self._clock = clock

    async def execute(self, perfume_id: UUID, presentation_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            perfume = await self._perfumes.get_for_update(perfume_id)
            if perfume is None:
                return Err(PerfumeNotFound())
            match perfume.restore_presentation(presentation_id):
                case Err(refused):
                    return Err(refused)
            return await self._perfumes.save(perfume)

        return await self._transactions.run(work)
