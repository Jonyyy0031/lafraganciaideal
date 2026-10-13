"""The anti-corruption adapter: inventory's `PresentationCatalog` over the catalog facade."""

from uuid import UUID

from fragancia_api.modules.catalog import PresentationDirectory, PresentationRef
from fragancia_api.modules.inventory.infrastructure.catalog_presentations import (
    CatalogPresentations,
)


class FakeQueries:
    def __init__(self, *refs: PresentationRef) -> None:
        self.refs = list(refs)

    async def find(self, ids: list[UUID]) -> dict[UUID, PresentationRef]:
        return {r.presentation_id: r for r in self.refs if r.presentation_id in set(ids)}

    async def list_in_stock(self) -> list[PresentationRef]:
        return [r for r in self.refs if r.availability == "in_stock"]


def ref(number: int, availability: str = "in_stock", *, sellable: bool = True) -> PresentationRef:
    return PresentationRef(
        presentation_id=UUID(int=number),
        perfume_id=UUID(int=500 + number),
        perfume_name="Eros",
        brand_name="Versace",
        concentration_abbreviation="EDT",
        ml=100,
        availability=availability,  # type: ignore[arg-type]
        is_sellable=sellable,
    )


def adapter(*refs: PresentationRef) -> CatalogPresentations:
    return CatalogPresentations(PresentationDirectory(FakeQueries(*refs)))  # type: ignore[arg-type]


async def test_describe_builds_the_label_from_brand_perfume_concentration_and_ml() -> None:
    described = await adapter(ref(1)).describe([UUID(int=1)])

    stocked = described[UUID(int=1)]
    assert stocked.label == "Versace Eros EDT 100 ml"
    assert (stocked.perfume_id, stocked.stocked, stocked.is_sellable) == (
        UUID(int=501),
        True,
        True,
    )


async def test_describe_marks_made_to_order_as_not_stocked_and_keeps_sellable() -> None:
    described = await adapter(ref(2, "made_to_order", sellable=False)).describe([UUID(int=2)])

    assert (described[UUID(int=2)].stocked, described[UUID(int=2)].is_sellable) == (False, False)


async def test_describe_omits_unknown_ids() -> None:
    assert await adapter(ref(1)).describe([UUID(int=9)]) == {}


async def test_in_stock_lists_only_the_facade_in_stock_presentations() -> None:
    listed = await adapter(ref(1), ref(2, "made_to_order")).in_stock()

    assert [p.presentation_id for p in listed] == [UUID(int=1)]
    assert all(p.stocked for p in listed)
