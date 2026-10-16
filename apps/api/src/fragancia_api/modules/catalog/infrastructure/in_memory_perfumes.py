"""In-memory perfume adapter for unit tests. It honors the same contracts as the SQL ones."""

import unicodedata
from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import NamedTuple
from uuid import UUID

from fragancia_api.modules.catalog.application.ports import PublicPerfumeFilters
from fragancia_api.modules.catalog.application.queries.presentation_directory import (
    PresentationRef,
)
from fragancia_api.modules.catalog.application.queries.presentation_offers import (
    PresentationOffer,
)
from fragancia_api.modules.catalog.contracts import (
    AdminPerfume,
    AdminPerfumePage,
    AdminPerfumeSummary,
    AdminPhoto,
    AdminPresentation,
    PerfumeBrandRef,
    PerfumeConcentrationRef,
    PerfumeFamilyRef,
    PublicPerfume,
    PublicPerfumeBrand,
    PublicPerfumeCard,
    PublicPerfumeConcentration,
    PublicPerfumeFamily,
    PublicPerfumePage,
    PublicPhoto,
    PublicPresentation,
)
from fragancia_api.modules.catalog.domain.errors import (
    PerfumeAlreadyExists,
    PresentationAlreadyExists,
)
from fragancia_api.modules.catalog.domain.perfume import (
    Perfume,
    Presentation,
    is_buyable,
    stock_status,
)
from fragancia_api.modules.catalog.infrastructure.in_memory import (
    InMemoryBrands,
    InMemoryConcentrations,
    InMemoryOlfactoryFamilies,
)
from fragancia_api.shared.kernel import Err, Ok, Result


class _StockFigures(NamedTuple):
    available: int
    threshold: int
    as_of: datetime | None


# A presentation never counted: 0 available (sold out) and the default threshold.
_UNCOUNTED = _StockFigures(0, 3, None)


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
        media_base_url: str = "http://media.test",
    ) -> None:
        self._media_base_url = media_base_url
        self._brands = brands
        self._families = families
        self._concentrations = concentrations
        self.by_id: dict[UUID, Perfume] = {perfume.id: deepcopy(perfume) for perfume in perfumes}
        # Retired slug -> the perfume that dropped it last.
        self.slug_history: dict[str, UUID] = {}
        self.stock: dict[UUID, _StockFigures] = {}

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
        copy.photos.sort(key=lambda photo: photo.position)
        return copy

    async def lock_presentations(self, perfume_ids: Sequence[UUID]) -> None:
        return None

    async def save(
        self, perfume: Perfume
    ) -> Result[None, PerfumeAlreadyExists | PresentationAlreadyExists]:
        if self._perfume_conflict(perfume):
            return Err(PerfumeAlreadyExists())
        mls = [presentation.ml for presentation in perfume.presentations]
        if len(mls) != len(set(mls)):
            return Err(PresentationAlreadyExists())
        for slug in perfume.retired_slugs:
            self.slug_history[slug] = perfume.id
        perfume.retired_slugs.clear()
        perfume.removed_photo_ids.clear()
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

    def _presentation_ref(self, perfume: Perfume, presentation: Presentation) -> PresentationRef:
        brand = self._brands.by_id[perfume.brand_id]
        concentration = self._concentrations.by_id[perfume.concentration_id]
        return PresentationRef(
            presentation_id=presentation.id,
            perfume_id=perfume.id,
            perfume_name=perfume.name.value,
            brand_name=brand.name.value,
            concentration_abbreviation=concentration.abbreviation.value,
            ml=presentation.ml.value,
            availability=presentation.availability.kind,
            is_sellable=presentation.is_active and not perfume.is_archived and brand.is_active,
        )

    async def find(self, ids: Sequence[UUID]) -> dict[UUID, PresentationRef]:
        """The `PresentationDirectoryQueries` port, over the same store."""
        wanted = set(ids)
        return {
            presentation.id: self._presentation_ref(perfume, presentation)
            for perfume in self.by_id.values()
            for presentation in perfume.presentations
            if presentation.id in wanted
        }

    async def find_for_update(self, presentation_id: UUID) -> PresentationRef | None:
        return (await self.find([presentation_id])).get(presentation_id)

    async def offers(self, ids: Sequence[UUID], now: datetime) -> dict[UUID, PresentationOffer]:
        """The `PresentationOffersQueries` port, over the same store."""
        wanted = set(ids)
        found: dict[UUID, PresentationOffer] = {}
        for perfume in self.by_id.values():
            brand = self._brands.by_id[perfume.brand_id]
            concentration = self._concentrations.by_id[perfume.concentration_id]
            for presentation in perfume.presentations:
                if presentation.id not in wanted:
                    continue
                in_stock = presentation.availability.kind == "in_stock"
                found[presentation.id] = PresentationOffer(
                    presentation_id=presentation.id,
                    perfume_id=perfume.id,
                    perfume_slug=perfume.slug,
                    label=(
                        f"{brand.name.value} {perfume.name.value} "
                        f"{concentration.abbreviation.value} {presentation.ml.value} ml"
                    ),
                    ml=presentation.ml.value,
                    card_url=self._photo_url(perfume),
                    price_cents=presentation.effective_price(now).cents,
                    regular_price_cents=presentation.price.amount.cents,
                    availability=presentation.availability.kind,
                    lead_time_min_days=presentation.availability.min_days,
                    lead_time_max_days=presentation.availability.max_days,
                    is_visible=presentation.is_active and self._visible(perfume),
                    available_units=self._figures(presentation).available if in_stock else None,
                )
        return found

    async def list_in_stock(self) -> list[PresentationRef]:
        refs = [
            self._presentation_ref(perfume, presentation)
            for perfume in self.by_id.values()
            if not perfume.is_archived
            for presentation in perfume.presentations
            if presentation.is_active and presentation.availability.kind == "in_stock"
        ]
        return sorted(
            refs,
            key=lambda r: (r.brand_name.lower(), r.perfume_name.lower(), r.ml, r.presentation_id),
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

    def _visible(self, perfume: Perfume) -> bool:
        return (
            perfume.is_published
            and not perfume.is_archived
            and self._brands.by_id[perfume.brand_id].is_active
        )

    # StockCopy

    async def apply(
        self, presentation_id: UUID, available: int, threshold: int, as_of: datetime
    ) -> None:
        known = any(x.id == presentation_id for p in self.by_id.values() for x in p.presentations)
        current = self.stock.get(presentation_id, _UNCOUNTED)
        if known and (current.as_of is None or current.as_of < as_of):
            self.stock[presentation_id] = _StockFigures(available, threshold, as_of)

    def _figures(self, presentation: Presentation) -> _StockFigures:
        return self.stock.get(presentation.id, _UNCOUNTED)

    async def list_public(
        self, filters: PublicPerfumeFilters, *, now: datetime
    ) -> PublicPerfumePage:
        cards: list[tuple[Perfume, int, bool, bool]] = []
        for p in self.by_id.values():
            active = [x for x in p.presentations if x.is_active]
            if not self._visible(p) or not active:
                continue
            buyable = [x for x in active if is_buyable(x.availability, self._figures(x).available)]
            price_from = min(x.effective_price(now).cents for x in (buyable or active))
            if filters.in_stock and not buyable:
                continue
            on_sale = any(x.sale is not None and x.sale.is_active(now) for x in active)
            brand = self._brands.by_id[p.brand_id]
            family = self._families.by_id[p.family_id]
            if filters.brands and brand.slug not in filters.brands:
                continue
            if filters.families and family.slug not in filters.families:
                continue
            if filters.genders and p.gender.value not in filters.genders:
                continue
            if filters.min_price_cents is not None and price_from < filters.min_price_cents:
                continue
            if filters.max_price_cents is not None and price_from > filters.max_price_cents:
                continue
            if filters.q is not None:
                haystack = " ".join(
                    (p.name.value, brand.name.value, *p.notes.top, *p.notes.heart, *p.notes.base)
                )
                if _fold(filters.q) not in _fold(haystack):
                    continue
            cards.append((p, price_from, on_sale, not buyable))

        def by_name(entry: tuple[Perfume, int, bool, bool]) -> tuple[str, str, UUID]:
            p = entry[0]
            return (self._brands.by_id[p.brand_id].name.value.lower(), p.name.value.lower(), p.id)

        if filters.sort == "name":
            cards.sort(key=by_name)
        elif filters.sort == "newest":
            cards.sort(key=lambda e: e[0].id)
            cards.sort(
                key=lambda e: e[0].first_published_at or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )
        else:
            cards.sort(key=lambda e: (e[0].name.value.lower(), e[0].id))
            cards.sort(key=lambda e: e[1], reverse=filters.sort == "price_desc")
        chunk = cards[(filters.page - 1) * filters.size : filters.page * filters.size]
        items = [
            PublicPerfumeCard(
                slug=p.slug,
                name=p.name.value,
                gender=p.gender.value,
                brand=self._public_brand(p),
                concentration=self._public_concentration(p),
                family=self._public_family(p),
                price_from_cents=price_from,
                on_sale=on_sale,
                sold_out=sold_out,
                photo_url=self._photo_url(p),
            )
            for p, price_from, on_sale, sold_out in chunk
        ]
        return PublicPerfumePage(
            items=items, total=len(cards), page=filters.page, size=filters.size
        )

    async def get_public(self, slug: str, *, now: datetime) -> PublicPerfume | None:
        perfume = next((p for p in self.by_id.values() if p.slug == slug), None)
        if perfume is None or not self._visible(perfume):
            perfume = None
            owner = self.slug_history.get(slug)
            if owner is not None and self._visible(self.by_id[owner]):
                perfume = self.by_id[owner]
        if perfume is None:
            return None
        return PublicPerfume(
            slug=perfume.slug,
            name=perfume.name.value,
            gender=perfume.gender.value,
            description=perfume.description.value,
            brand=self._public_brand(perfume),
            concentration=self._public_concentration(perfume),
            family=self._public_family(perfume),
            top_notes=list(perfume.notes.top),
            heart_notes=list(perfume.notes.heart),
            base_notes=list(perfume.notes.base),
            presentations=[
                _public_presentation(p, now, self._figures(p))
                for p in sorted(perfume.presentations, key=lambda p: p.ml.value)
                if p.is_active
            ],
            photos=[
                PublicPhoto(
                    card_url=f"{self._media_base_url}/{photo.card_key}",
                    detail_url=f"{self._media_base_url}/{photo.detail_key}",
                )
                for photo in sorted(perfume.photos, key=lambda photo: photo.position)
            ],
        )

    def _photo_url(self, perfume: Perfume) -> str | None:
        first = next((photo for photo in perfume.photos if photo.position == 1), None)
        return f"{self._media_base_url}/{first.card_key}" if first is not None else None

    def _public_brand(self, perfume: Perfume) -> PublicPerfumeBrand:
        brand = self._brands.by_id[perfume.brand_id]
        return PublicPerfumeBrand(name=brand.name.value, slug=brand.slug)

    def _public_concentration(self, perfume: Perfume) -> PublicPerfumeConcentration:
        concentration = self._concentrations.by_id[perfume.concentration_id]
        return PublicPerfumeConcentration(
            name=concentration.name.value, abbreviation=concentration.abbreviation.value
        )

    def _public_family(self, perfume: Perfume) -> PublicPerfumeFamily:
        family = self._families.by_id[perfume.family_id]
        return PublicPerfumeFamily(name=family.name.value, slug=family.slug)

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
            photos=[
                AdminPhoto(
                    id=photo.id,
                    position=photo.position,
                    card_url=f"{self._media_base_url}/{photo.card_key}",
                    detail_url=f"{self._media_base_url}/{photo.detail_key}",
                    width=photo.width,
                    height=photo.height,
                    created_at=photo.created_at,
                )
                for photo in sorted(perfume.photos, key=lambda photo: photo.position)
            ],
        )


def _fold(text: str) -> str:
    """Case- and accent-insensitive form, like `unaccent(lower(...))` in SQL."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _public_presentation(
    presentation: Presentation, now: datetime, figures: _StockFigures
) -> PublicPresentation:
    sale = presentation.sale
    on_sale = sale is not None and sale.is_active(now)
    return PublicPresentation(
        id=presentation.id,
        ml=presentation.ml.value,
        availability=presentation.availability.kind,
        lead_time_min_days=presentation.availability.min_days,
        lead_time_max_days=presentation.availability.max_days,
        price_cents=presentation.effective_price(now).cents,
        regular_price_cents=presentation.price.amount.cents if on_sale else None,
        sale_ends_at=sale.ends_at if sale is not None and on_sale else None,
        stock_status=stock_status(presentation.availability, figures.available, figures.threshold),
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
