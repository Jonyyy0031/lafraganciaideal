"""Commands on perfumes: create, update, publish, hide, archive and restore.

None publishes events yet: nothing subscribes to them.
"""

from dataclasses import dataclass
from uuid import UUID

from fragancia_api.modules.catalog.contracts import PerfumeRequest
from fragancia_api.modules.catalog.domain.errors import (
    PerfumeAlreadyExists,
    PerfumeBrandUnavailable,
    PerfumeConcentrationUnavailable,
    PerfumeFamilyUnavailable,
    PerfumeNotFound,
)
from fragancia_api.modules.catalog.domain.perfume import (
    Description,
    Gender,
    Notes,
    Perfume,
    PerfumeName,
    perfume_slug,
)
from fragancia_api.modules.catalog.domain.repositories import (
    BrandRepository,
    ConcentrationRepository,
    OlfactoryFamilyRepository,
    PerfumeRepository,
)
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


@dataclass(frozen=True, slots=True)
class _PerfumeValues:
    name: PerfumeName
    gender: Gender
    description: Description
    notes: Notes


def _values(request: PerfumeRequest) -> Result[_PerfumeValues, DomainError]:
    """The validated values, in the order name, description, notes (first Err wins)."""
    match PerfumeName.create(request.name):
        case Err(invalid_name):
            return Err(invalid_name)
        case Ok(name):
            pass
    match Description.create(request.description):
        case Err(invalid_description):
            return Err(invalid_description)
        case Ok(description):
            pass
    match Notes.create(request.top_notes, request.heart_notes, request.base_notes):
        case Err(invalid_notes):
            return Err(invalid_notes)
        case Ok(notes):
            pass
    return Ok(_PerfumeValues(name, Gender(request.gender), description, notes))


class CreatePerfume:
    """Command: register a new perfume, hidden and without presentations. Its brand, family and
    concentration must exist and be active."""

    def __init__(
        self,
        *,
        perfumes: PerfumeRepository,
        brands: BrandRepository,
        families: OlfactoryFamilyRepository,
        concentrations: ConcentrationRepository,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._perfumes = perfumes
        self._brands = brands
        self._families = families
        self._concentrations = concentrations
        self._transactions = transactions
        self._clock = clock

    async def execute(self, request: PerfumeRequest) -> Result[UUID, DomainError]:
        async def work() -> Result[UUID, DomainError]:
            match _values(request):
                case Err(invalid):
                    return Err(invalid)
                case Ok(values):
                    pass
            brand = await self._brands.get(request.brand_id)
            if brand is None or not brand.is_active:
                return Err(PerfumeBrandUnavailable())
            family = await self._families.get(request.family_id)
            if family is None or not family.is_active:
                return Err(PerfumeFamilyUnavailable())
            concentration = await self._concentrations.get(request.concentration_id)
            if concentration is None or not concentration.is_active:
                return Err(PerfumeConcentrationUnavailable())
            if await self._perfumes.exists_with_identity(
                request.brand_id, values.name.slug, request.concentration_id
            ):
                return Err(PerfumeAlreadyExists())
            perfume = Perfume.create(
                brand_id=request.brand_id,
                concentration_id=request.concentration_id,
                family_id=request.family_id,
                name=values.name,
                slug=perfume_slug(brand.name.value, values.name, concentration.abbreviation.value),
                gender=values.gender,
                description=values.description,
                notes=values.notes,
                now=self._clock.now(),
            )
            match await self._perfumes.add(perfume):  # also catches a concurrent one
                case Err(conflict):
                    return Err(conflict)
            return Ok(perfume.id)

        return await self._transactions.run(work)


class UpdatePerfume:
    """Command: replace every field of a perfume and recompute its slug. Only a changed brand,
    family or concentration must be active; an unchanged archived one is kept."""

    def __init__(
        self,
        *,
        perfumes: PerfumeRepository,
        brands: BrandRepository,
        families: OlfactoryFamilyRepository,
        concentrations: ConcentrationRepository,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._perfumes = perfumes
        self._brands = brands
        self._families = families
        self._concentrations = concentrations
        self._transactions = transactions
        self._clock = clock

    async def execute(self, perfume_id: UUID, request: PerfumeRequest) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            match _values(request):
                case Err(invalid):
                    return Err(invalid)
                case Ok(values):
                    pass
            perfume = await self._perfumes.get_for_update(perfume_id)
            if perfume is None:
                return Err(PerfumeNotFound())
            # A changed reference must be active (decision 23); an unchanged one is only read for
            # the slug (brand name, concentration abbreviation), even when archived.
            brand = await self._brands.get(request.brand_id)
            if brand is None or (not brand.is_active and request.brand_id != perfume.brand_id):
                return Err(PerfumeBrandUnavailable())
            if request.family_id != perfume.family_id:
                family = await self._families.get(request.family_id)
                if family is None or not family.is_active:
                    return Err(PerfumeFamilyUnavailable())
            concentration = await self._concentrations.get(request.concentration_id)
            if concentration is None or (
                not concentration.is_active and request.concentration_id != perfume.concentration_id
            ):
                return Err(PerfumeConcentrationUnavailable())
            if await self._perfumes.exists_with_identity(
                request.brand_id,
                values.name.slug,
                request.concentration_id,
                except_id=perfume_id,
            ):
                return Err(PerfumeAlreadyExists())
            match perfume.update(
                brand_id=request.brand_id,
                concentration_id=request.concentration_id,
                family_id=request.family_id,
                name=values.name,
                slug=perfume_slug(brand.name.value, values.name, concentration.abbreviation.value),
                gender=values.gender,
                description=values.description,
                notes=values.notes,
                now=self._clock.now(),
            ):
                case Err(archived):
                    return Err(archived)
            return await self._perfumes.save(perfume)  # also catches a concurrent one

        return await self._transactions.run(work)


class PublishPerfume:
    """Command: show a perfume in the storefront. Needs an active presentation; idempotent."""

    def __init__(
        self, *, perfumes: PerfumeRepository, transactions: TransactionRunner, clock: Clock
    ) -> None:
        self._perfumes = perfumes
        self._transactions = transactions
        self._clock = clock

    async def execute(self, perfume_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            perfume = await self._perfumes.get_for_update(perfume_id)
            if perfume is None:
                return Err(PerfumeNotFound())
            match perfume.publish(self._clock.now()):
                case Err(refused):
                    return Err(refused)
            return await self._perfumes.save(perfume)

        return await self._transactions.run(work)


class HidePerfume:
    """Command: hide a perfume from the storefront. Idempotent; works on archived ones."""

    def __init__(
        self, *, perfumes: PerfumeRepository, transactions: TransactionRunner, clock: Clock
    ) -> None:
        self._perfumes = perfumes
        self._transactions = transactions
        self._clock = clock

    async def execute(self, perfume_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            perfume = await self._perfumes.get_for_update(perfume_id)
            if perfume is None:
                return Err(PerfumeNotFound())
            perfume.hide()
            return await self._perfumes.save(perfume)

        return await self._transactions.run(work)


class ArchivePerfume:
    """Command: archive (and hide) a perfume; it becomes read-only. Idempotent."""

    def __init__(
        self, *, perfumes: PerfumeRepository, transactions: TransactionRunner, clock: Clock
    ) -> None:
        self._perfumes = perfumes
        self._transactions = transactions
        self._clock = clock

    async def execute(self, perfume_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            perfume = await self._perfumes.get_for_update(perfume_id)
            if perfume is None:
                return Err(PerfumeNotFound())
            perfume.archive()
            return await self._perfumes.save(perfume)

        return await self._transactions.run(work)


class RestorePerfume:
    """Command: bring an archived perfume back, still hidden. Idempotent."""

    def __init__(
        self, *, perfumes: PerfumeRepository, transactions: TransactionRunner, clock: Clock
    ) -> None:
        self._perfumes = perfumes
        self._transactions = transactions
        self._clock = clock

    async def execute(self, perfume_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[None, DomainError]:
            perfume = await self._perfumes.get_for_update(perfume_id)
            if perfume is None:
                return Err(PerfumeNotFound())
            perfume.restore()
            return await self._perfumes.save(perfume)

        return await self._transactions.run(work)
