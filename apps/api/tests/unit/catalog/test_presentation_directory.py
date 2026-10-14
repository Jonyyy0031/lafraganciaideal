"""The catalog's public facade `PresentationDirectory` over the in-memory queries."""

from fragancia_api.modules.catalog import PresentationDirectory, PresentationRef
from fragancia_api.modules.catalog.domain.perfume import Availability, Ml, Price
from tests.unit.catalog.perfume_support import (
    World,
    add_presentation,
    make_brand,
    make_perfume,
    unwrap_ok,
)


def directory(world: World) -> PresentationDirectory:
    return PresentationDirectory(world.perfumes)


async def test_find_returns_the_names_and_kind_of_a_presentation() -> None:
    world = World()
    perfume = world.seed(make_perfume(world.brand, world.family, world.concentration))
    presentation_id = add_presentation(perfume, ml=100)

    found = await directory(world).find([presentation_id])

    assert found == {
        presentation_id: PresentationRef(
            presentation_id=presentation_id,
            perfume_id=perfume.id,
            perfume_name="Eros",
            brand_name="Versace",
            concentration_abbreviation="EDT",
            ml=100,
            availability="in_stock",
            is_sellable=True,
        )
    }


async def test_find_omits_unknown_ids() -> None:
    from uuid import UUID

    world = World()
    perfume = world.seed(make_perfume(world.brand, world.family, world.concentration))
    known = add_presentation(perfume)

    found = await directory(world).find([known, UUID(int=999)])

    assert list(found) == [known]


async def test_find_reports_made_to_order_and_is_not_sellable_when_inactive() -> None:
    world = World()
    perfume = world.seed(make_perfume(world.brand, world.family, world.concentration))
    presentation_id = unwrap_ok(
        perfume.add_presentation(
            unwrap_ok(Ml.create(50)),
            unwrap_ok(Price.create(100_000)),
            None,
            unwrap_ok(Availability.create("made_to_order", 3, 5)),
            created_at=perfume.created_at,
        )
    )
    add_presentation(perfume, ml=100)  # keeps the perfume with an active size
    perfume.presentations[0].is_active = False

    ref = (await directory(world).find([presentation_id]))[presentation_id]

    assert ref.availability == "made_to_order"
    assert ref.is_sellable is False


async def test_a_presentation_of_an_archived_perfume_or_inactive_brand_is_not_sellable() -> None:
    world = World()
    archived = world.seed(make_perfume(world.brand, world.family, world.concentration))
    archived_id = add_presentation(archived)
    archived.is_archived = True
    closed_brand = make_brand("Closed", active=False)
    world.brands.by_id[closed_brand.id] = closed_brand
    other = world.seed(make_perfume(closed_brand, world.family, world.concentration, name="Other"))
    other_id = add_presentation(other)

    found = await directory(world).find([archived_id, other_id])

    assert found[archived_id].is_sellable is False
    assert found[other_id].is_sellable is False


async def test_list_in_stock_keeps_only_active_in_stock_presentations_of_live_perfumes() -> None:
    world = World()
    live = world.seed(make_perfume(world.brand, world.family, world.concentration, name="Live"))
    live_id = add_presentation(live, ml=100)
    unwrap_ok(
        live.add_presentation(
            unwrap_ok(Ml.create(50)),
            unwrap_ok(Price.create(100_000)),
            None,
            unwrap_ok(Availability.create("made_to_order", 3, 5)),
            created_at=live.created_at,
        )
    )
    inactive_id = add_presentation(live, ml=30)
    next(p for p in live.presentations if p.id == inactive_id).is_active = False
    archived = world.seed(
        make_perfume(world.brand, world.family, world.concentration, name="Archived")
    )
    add_presentation(archived)
    archived.is_archived = True

    listed = await directory(world).list_in_stock()

    assert [ref.presentation_id for ref in listed] == [live_id]


async def test_list_in_stock_is_ordered_by_brand_perfume_and_ml() -> None:
    world = World()
    zed = make_brand("Zed")
    world.brands.by_id[zed.id] = zed
    b = world.seed(make_perfume(world.brand, world.family, world.concentration, name="Beta"))
    a = world.seed(make_perfume(world.brand, world.family, world.concentration, name="alpha"))
    z = world.seed(make_perfume(zed, world.family, world.concentration, name="Alpha"))
    ids = [
        add_presentation(b, ml=100),
        add_presentation(a, ml=200),
        add_presentation(a, ml=50),
        add_presentation(z, ml=10),
    ]

    listed = await directory(world).list_in_stock()

    assert [ref.presentation_id for ref in listed] == [ids[2], ids[1], ids[0], ids[3]]
