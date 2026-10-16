"""The catalog's public facade `PresentationOffers` over the in-memory queries."""

from datetime import timedelta
from uuid import UUID

from fragancia_api.modules.catalog import PresentationOffer, PresentationOffers
from fragancia_api.modules.catalog.domain.perfume import Availability, Ml, Perfume, Price, Sale
from fragancia_api.shared.kernel import Money
from tests.unit.catalog.perfume_support import (
    NOW,
    World,
    add_presentation,
    make_brand,
    make_perfume,
    unwrap_ok,
)


def offers(world: World) -> PresentationOffers:
    return PresentationOffers(world.perfumes)


def seeded(world: World, **kwargs: str) -> Perfume:
    perfume = world.seed(make_perfume(world.brand, world.family, world.concentration, **kwargs))
    return perfume


def publish(perfume: Perfume) -> None:
    unwrap_ok(perfume.publish(NOW))


def add_made_to_order(perfume: Perfume, *, ml: int = 50, days: tuple[int, int] = (3, 5)) -> UUID:
    return unwrap_ok(
        perfume.add_presentation(
            unwrap_ok(Ml.create(ml)),
            unwrap_ok(Price.create(180_000)),
            None,
            unwrap_ok(Availability.create("made_to_order", *days)),
            created_at=NOW,
        )
    )


def sale_of(perfume: Perfume, presentation_id: UUID, **kwargs: object) -> None:
    presentation = next(p for p in perfume.presentations if p.id == presentation_id)
    presentation.sale = Sale(Money(kwargs.pop("price_cents")), **kwargs)  # type: ignore[arg-type]


async def test_an_in_stock_offer_carries_label_slug_prices_and_the_stock_copy() -> None:
    world = World()
    perfume = seeded(world)
    presentation_id = add_presentation(perfume, ml=100, price_cents=250_000)
    publish(perfume)
    await world.perfumes.apply(presentation_id, 7, 3, NOW)

    offer = (await offers(world).offers([presentation_id], NOW))[presentation_id]

    assert offer == PresentationOffer(
        presentation_id=presentation_id,
        perfume_id=perfume.id,
        perfume_slug=perfume.slug,
        label="Versace Eros EDT 100 ml",
        ml=100,
        card_url=None,
        price_cents=250_000,
        regular_price_cents=250_000,
        availability="in_stock",
        lead_time_min_days=None,
        lead_time_max_days=None,
        is_visible=True,
        available_units=7,
    )


async def test_a_presentation_never_counted_has_zero_units() -> None:
    world = World()
    perfume = seeded(world)
    presentation_id = add_presentation(perfume)
    publish(perfume)

    offer = (await offers(world).offers([presentation_id], NOW))[presentation_id]

    assert offer.available_units == 0


async def test_a_made_to_order_offer_has_lead_times_and_no_units() -> None:
    world = World()
    perfume = seeded(world)
    presentation_id = add_made_to_order(perfume, days=(3, 5))
    publish(perfume)
    await world.perfumes.apply(presentation_id, 9, 3, NOW)

    offer = (await offers(world).offers([presentation_id], NOW))[presentation_id]

    assert (offer.availability, offer.lead_time_min_days, offer.lead_time_max_days) == (
        "made_to_order",
        3,
        5,
    )
    assert offer.available_units is None


async def test_the_price_is_the_sale_price_inside_the_window_and_the_regular_one_outside() -> None:
    world = World()
    perfume = seeded(world)
    presentation_id = add_presentation(perfume, price_cents=250_000)
    publish(perfume)
    start, end = NOW, NOW + timedelta(days=2)
    sale_of(perfume, presentation_id, price_cents=200_000, starts_at=start, ends_at=end)
    facade = offers(world)

    before = (await facade.offers([presentation_id], start - timedelta(seconds=1)))[presentation_id]
    at_start = (await facade.offers([presentation_id], start))[presentation_id]
    inside = (await facade.offers([presentation_id], NOW + timedelta(days=1)))[presentation_id]
    at_end = (await facade.offers([presentation_id], end))[presentation_id]

    assert [o.price_cents for o in (before, at_start, inside, at_end)] == [
        250_000,
        200_000,
        200_000,
        250_000,
    ]
    assert {o.regular_price_cents for o in (before, at_start, inside, at_end)} == {250_000}


async def test_an_open_ended_sale_applies_from_its_start() -> None:
    world = World()
    perfume = seeded(world)
    presentation_id = add_presentation(perfume, price_cents=250_000)
    publish(perfume)
    sale_of(perfume, presentation_id, price_cents=200_000, starts_at=None, ends_at=None)

    offer = (await offers(world).offers([presentation_id], NOW))[presentation_id]

    assert (offer.price_cents, offer.regular_price_cents) == (200_000, 250_000)


async def test_the_card_url_is_the_first_photo_by_position() -> None:
    world = World()
    perfume = seeded(world)
    presentation_id = add_presentation(perfume)
    publish(perfume)
    for key in ("first", "second"):
        unwrap_ok(
            perfume.add_photo(
                UUID(int=len(perfume.photos) + 50),
                f"{key}-card.webp",
                f"{key}-detail.webp",
                800,
                800,
                created_at=NOW,
            )
        )

    offer = (await offers(world).offers([presentation_id], NOW))[presentation_id]

    assert offer.card_url == "http://media.test/first-card.webp"


async def test_unknown_ids_are_absent_and_an_empty_list_is_empty() -> None:
    world = World()
    perfume = seeded(world)
    known = add_presentation(perfume)
    publish(perfume)

    found = await offers(world).offers([known, UUID(int=999)], NOW)

    assert list(found) == [known]
    assert await offers(world).offers([], NOW) == {}


async def test_an_unpublished_perfume_is_listed_but_not_visible() -> None:
    world = World()
    perfume = seeded(world)
    presentation_id = add_presentation(perfume)

    offer = (await offers(world).offers([presentation_id], NOW))[presentation_id]

    assert offer.is_visible is False


async def test_an_archived_perfume_is_not_visible() -> None:
    world = World()
    perfume = seeded(world)
    presentation_id = add_presentation(perfume)
    publish(perfume)
    perfume.archive()

    offer = (await offers(world).offers([presentation_id], NOW))[presentation_id]

    assert offer.is_visible is False


async def test_an_inactive_presentation_is_not_visible_but_its_sibling_is() -> None:
    world = World()
    perfume = seeded(world)
    inactive = add_presentation(perfume, ml=50)
    sibling = add_presentation(perfume, ml=100)
    publish(perfume)
    next(p for p in perfume.presentations if p.id == inactive).is_active = False

    found = await offers(world).offers([inactive, sibling], NOW)

    assert (found[inactive].is_visible, found[sibling].is_visible) == (False, True)


async def test_a_perfume_of_an_inactive_brand_is_not_visible() -> None:
    world = World()
    closed = make_brand("Closed", active=False)
    world.brands.by_id[closed.id] = closed
    perfume = world.seed(make_perfume(closed, world.family, world.concentration))
    presentation_id = add_presentation(perfume)
    publish(perfume)

    offer = (await offers(world).offers([presentation_id], NOW))[presentation_id]

    assert offer.is_visible is False
