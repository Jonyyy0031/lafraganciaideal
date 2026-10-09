"""In-memory perfume adapter for unit tests. It honors the same contracts as the SQL ones."""

from copy import deepcopy
from uuid import UUID

from fragancia_api.modules.catalog.contracts import (
    AdminPerfume,
    AdminPerfumePage,
    AdminPerfumeSummary,
    AdminPresentation,
    PerfumeBrandRef,
    PerfumeConcentrationRef,
    PerfumeFamilyRef,
)
from fragancia_api.modules.catalog.domain.errors import (
    PerfumeAlreadyExists,
    PresentationAlreadyExists,
)
from fragancia_api.modules.catalog.domain.perfume import Perfume, Presentation
from fragancia_api.modules.catalog.infrastructure.in_memory import (
    InMemoryBrands,
    InMemoryConcentrations,
    InMemoryOlfactoryFamilies,
)
from fragancia_api.shared.kernel import Err, Ok, Result


class InMemoryPerfumes:
    """Both the repository and the queries over one store. The brand, family and concentration
    stores provide the refs of the read models.

    Like the SQL adapter, it stores and hands out copies: a mutation is only kept by `save`.
    """

    def __init__(
        self,
        brands: InMemoryBrands,
        families: InMemoryOlfactoryFamilies,
        concentrations: InMemoryConcentrations,
        *perfumes: Perfume,
    ) -> None:
        self._brands = brands
        self._families = families
        self._concentrations = concentrations
        self.by_id: dict[UUID, Perfume] = {perfume.id: deepcopy(perfume) for perfume in perfumes}

    async def exists_with_identity(
        self,
        brand_id: UUID,
        name_slug: str,
        concentration_id: UUID,
        *,
        except_id: UUID | None = None,
    ) -> bool:
        return any(
            p.brand_id == brand_id
            and p.name_slug == name_slug
            and p.concentration_id == concentration_id
            and p.id != except_id
            for p in self.by_id.values()
        )

    def _perfume_conflict(self, perfume: Perfume) -> bool:
        return any(
            other.id != perfume.id
            and (
                other.slug == perfume.slug
                or (
                    other.brand_id == perfume.brand_id
                    and other.name_slug == perfume.name_slug
                    and other.concentration_id == perfume.concentration_id
                )
            )
            for other in self.by_id.values()
        )

    async def add(self, perfume: Perfume) -> Result[None, PerfumeAlreadyExists]:
        if self._perfume_conflict(perfume):
            return Err(PerfumeAlreadyExists())
        self.by_id[perfume.id] = deepcopy(perfume)
        return Ok(None)

    async def get_for_update(self, perfume_id: UUID) -> Perfume | None:
        perfume = self.by_id.get(perfume_id)
        if perfume is None:
            return None
        copy = deepcopy(perfume)
        copy.presentations.sort(key=lambda p: p.ml.value)
        return copy

    async def save(
        self, perfume: Perfume
    ) -> Result[None, PerfumeAlreadyExists | PresentationAlreadyExists]:
        if self._perfume_conflict(perfume):
            return Err(PerfumeAlreadyExists())
        mls = [presentation.ml for presentation in perfume.presentations]
        if len(mls) != len(set(mls)):
            return Err(PresentationAlreadyExists())
        self.by_id[perfume.id] = deepcopy(perfume)
        return Ok(None)

    def _brand_ref(self, perfume: Perfume) -> PerfumeBrandRef:
        brand = self._brands.by_id[perfume.brand_id]
        return PerfumeBrandRef(id=brand.id, name=brand.name.value, is_active=brand.is_active)

    def _concentration_ref(self, perfume: Perfume) -> PerfumeConcentrationRef:
        concentration = self._concentrations.by_id[perfume.concentration_id]
        return PerfumeConcentrationRef(
            id=concentration.id,
            name=concentration.name.value,
            abbreviation=concentration.abbreviation.value,
            is_active=concentration.is_active,
        )

    async def list_admin(self, *, page: int, size: int, archived: bool) -> AdminPerfumePage:
        everything = sorted(
            (p for p in self.by_id.values() if p.is_archived == archived),
            key=lambda p: (
                self._brands.by_id[p.brand_id].name.value.lower(),
                p.name.value.lower(),
                p.id,
            ),
        )
        chunk = everything[(page - 1) * size : page * size]
        items = [
            AdminPerfumeSummary(
                id=p.id,
                slug=p.slug,
                name=p.name.value,
                brand=self._brand_ref(p),
                concentration=self._concentration_ref(p),
                gender=p.gender.value,
                is_published=p.is_published,
                is_archived=p.is_archived,
                active_presentations=sum(1 for x in p.presentations if x.is_active),
                created_at=p.created_at,
            )
            for p in chunk
        ]
        return AdminPerfumePage(items=items, total=len(everything), page=page, size=size)

    async def get_admin(self, perfume_id: UUID) -> AdminPerfume | None:
        perfume = self.by_id.get(perfume_id)
        if perfume is None:
            return None
        family = self._families.by_id[perfume.family_id]
        return AdminPerfume(
            id=perfume.id,
            slug=perfume.slug,
            name=perfume.name.value,
            gender=perfume.gender.value,
            description=perfume.description.value,
            top_notes=list(perfume.notes.top),
            heart_notes=list(perfume.notes.heart),
            base_notes=list(perfume.notes.base),
            brand=self._brand_ref(perfume),
            concentration=self._concentration_ref(perfume),
            family=PerfumeFamilyRef(
                id=family.id, name=family.name.value, is_active=family.is_active
            ),
            is_published=perfume.is_published,
            first_published_at=perfume.first_published_at,
            is_archived=perfume.is_archived,
            created_at=perfume.created_at,
            updated_at=perfume.updated_at,
            presentations=[
                _admin_presentation(p)
                for p in sorted(perfume.presentations, key=lambda p: p.ml.value)
            ],
        )


def _admin_presentation(presentation: Presentation) -> AdminPresentation:
    sale = presentation.sale
    return AdminPresentation(
        id=presentation.id,
        ml=presentation.ml.value,
        price_cents=presentation.price.amount.cents,
        sale_price_cents=sale.price.cents if sale is not None else None,
        sale_starts_at=sale.starts_at if sale is not None else None,
        sale_ends_at=sale.ends_at if sale is not None else None,
        availability=presentation.availability.kind,
        lead_time_min_days=presentation.availability.min_days,
        lead_time_max_days=presentation.availability.max_days,
        is_active=presentation.is_active,
        created_at=presentation.created_at,
    )
