"""Plan 004: the public query use cases (with the in-memory adapter as the port)."""

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from fragancia_api.modules.catalog.application.ports import PublicPerfumeFilters
from fragancia_api.modules.catalog.application.queries.perfumes import (
    GetPublicPerfume,
    ListPublicPerfumes,
)
from fragancia_api.modules.catalog.contracts import PublicPerfume, PublicPerfumePage
from fragancia_api.modules.catalog.domain.perfume import (
    Availability,
    Description,
    Gender,
    Ml,
    Notes,
    PerfumeName,
    Price,
    Sale,
)
from fragancia_api.modules.catalog.infrastructure.in_memory_perfumes import InMemoryPerfumes
from fragancia_api.shared.infrastructure.in_memory import FixedClock
from fragancia_api.shared.kernel import Err, Money, Ok
from tests.unit.catalog.perfume_support import (
    NOW,
    World,
    add_presentation,
    make_brand,
    make_perfume,
    unwrap_ok,
)

NO_FILTERS = PublicPerfumeFilters(
    q=None,
    brands=(),
    families=(),
    genders=(),
    min_price_cents=None,
    max_price_cents=None,
    sort="name",
    page=1,
    size=24,
)


class RecordingQueries:
    """Wraps the in-memory queries and remembers what the use cases passed to them."""

    def __init__(self, inner: InMemoryPerfumes) -> None:
        self._inner = inner
        self.list_calls: list[tuple[PublicPerfumeFilters, datetime]] = []
        self.get_calls: list[tuple[str, datetime]] = []

    async def list_public(
        self, filters: PublicPerfumeFilters, *, now: datetime
    ) -> PublicPerfumePage:
        self.list_calls.append((filters, now))
        return await self._inner.list_public(filters, now=now)

    async def get_public(self, slug: str, *, now: datetime) -> PublicPerfume | None:
        self.get_calls.append((slug, now))
        return await self._inner.get_public(slug, now=now)


def _published(world: World, name: str = "Eros", **presentation: Any) -> Any:
    perfume = world.seed(make_perfume(world.brand, world.family, world.concentration, name=name))
    add_presentation(perfume, **presentation)
    unwrap_ok(perfume.publish(NOW))
    world.perfumes.by_id[perfume.id] = perfume
    return perfume


# --- ListPublicPerfumes -----------------------------------------------------------------------


@pytest.mark.parametrize("q", ["", "   ", "\t\n"])
async def test_a_blank_search_becomes_no_search(q: str) -> None:
    world = World()
    queries = RecordingQueries(world.perfumes)

    await ListPublicPerfumes(queries, FixedClock()).execute(replace(NO_FILTERS, q=q))  # type: ignore[arg-type]

    assert queries.list_calls[0][0].q is None


async def test_the_search_is_trimmed_before_the_query() -> None:
    world = World()
    queries = RecordingQueries(world.perfumes)

    await ListPublicPerfumes(queries, FixedClock()).execute(replace(NO_FILTERS, q="  lanc "))  # type: ignore[arg-type]

    assert queries.list_calls[0][0].q == "lanc"


async def test_the_other_filters_reach_the_query_untouched() -> None:
    world = World()
    queries = RecordingQueries(world.perfumes)
    filters = PublicPerfumeFilters(
        q=None,
        brands=("a", "b"),
        families=("f",),
        genders=("men",),
        min_price_cents=1,
        max_price_cents=9,
        sort="newest",
        page=3,
        size=5,
    )

    await ListPublicPerfumes(queries, FixedClock()).execute(filters)  # type: ignore[arg-type]

    assert queries.list_calls[0][0] == filters


async def test_listing_passes_the_clock_now_to_the_query() -> None:
    world = World()
    queries = RecordingQueries(world.perfumes)
    clock = FixedClock()
    use_case = ListPublicPerfumes(queries, clock)  # type: ignore[arg-type]

    await use_case.execute(NO_FILTERS)
    clock.current = clock.current + timedelta(days=1)
    await use_case.execute(NO_FILTERS)

    assert [now for _, now in queries.list_calls] == [NOW, NOW + timedelta(days=1)]


async def test_listing_returns_the_page_of_the_query() -> None:
    world = World()
    _published(world)

    page = await ListPublicPerfumes(world.perfumes, FixedClock()).execute(NO_FILTERS)

    assert (page.total, [card.name for card in page.items]) == (1, ["Eros"])


async def test_the_in_memory_list_uses_the_domain_effective_price_at_the_clock_time() -> None:
    world = World()
    perfume = world.seed(make_perfume(world.brand, world.family, world.concentration))
    perfume.add_presentation(
        Ml(100),
        Price(Money(250_000)),
        Sale(Money(200_000), NOW, NOW + timedelta(hours=1)),
        Availability("in_stock", None, None),
        created_at=NOW,
    )
    unwrap_ok(perfume.publish(NOW))
    world.perfumes.by_id[perfume.id] = perfume
    clock = FixedClock()
    use_case = ListPublicPerfumes(world.perfumes, clock)

    during = (await use_case.execute(NO_FILTERS)).items[0]
    clock.current = NOW + timedelta(hours=1)  # the end is exclusive
    after = (await use_case.execute(NO_FILTERS)).items[0]

    assert (during.price_from_cents, during.on_sale) == (200_000, True)
    assert (after.price_from_cents, after.on_sale) == (250_000, False)


async def test_the_in_memory_list_hides_hidden_archived_and_archived_brand_perfumes() -> None:
    world = World()
    _published(world, "Visible")
    hidden = world.seed(make_perfume(world.brand, world.family, world.concentration, name="Hid"))
    add_presentation(hidden)
    archived = _published(world, "Gone")
    archived.archive()
    world.perfumes.by_id[archived.id] = archived
    inactive_brand = make_brand("Closed", active=False)
    world.brands.by_id[inactive_brand.id] = inactive_brand
    other = world.seed(make_perfume(inactive_brand, world.family, world.concentration, name="Zed"))
    add_presentation(other)
    unwrap_ok(other.publish(NOW))
    world.perfumes.by_id[other.id] = other

    page = await ListPublicPerfumes(world.perfumes, FixedClock()).execute(NO_FILTERS)

    assert [card.name for card in page.items] == ["Visible"]


# --- GetPublicPerfume -------------------------------------------------------------------------


async def test_get_returns_the_visible_perfume() -> None:
    world = World()
    perfume = _published(world)

    result = await GetPublicPerfume(world.perfumes, FixedClock()).execute(perfume.slug)

    assert isinstance(result, Ok)
    assert result.value.slug == perfume.slug


async def test_get_answers_not_found_for_an_unknown_slug() -> None:
    result = await GetPublicPerfume(World().perfumes, FixedClock()).execute("nope")

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_PERFUME_NOT_FOUND"


async def test_get_answers_not_found_for_a_hidden_perfume() -> None:
    world = World()
    perfume = world.seed(make_perfume(world.brand, world.family, world.concentration))
    add_presentation(perfume)

    result = await GetPublicPerfume(world.perfumes, FixedClock()).execute(perfume.slug)

    assert isinstance(result, Err)
    assert result.error.code == "CATALOG_PERFUME_NOT_FOUND"


async def test_get_passes_the_clock_now_to_the_query() -> None:
    world = World()
    queries = RecordingQueries(world.perfumes)

    await GetPublicPerfume(queries, FixedClock()).execute("nope")  # type: ignore[arg-type]

    assert queries.get_calls == [("nope", NOW)]


async def test_an_old_slug_finds_the_perfume_and_answers_the_current_slug() -> None:
    world = World()
    perfume = _published(world)
    old_slug = perfume.slug
    stored = await world.perfumes.get_for_update(perfume.id)
    assert stored is not None
    unwrap_ok(
        stored.update(
            brand_id=stored.brand_id,
            concentration_id=stored.concentration_id,
            family_id=stored.family_id,
            name=unwrap_ok(PerfumeName.create("Eros Flame")),
            slug="versace-eros-flame-edt",
            gender=Gender.MEN,
            description=unwrap_ok(Description.create("")),
            notes=unwrap_ok(Notes.create([], [], [])),
            now=NOW,
        )
    )
    unwrap_ok(await world.perfumes.save(stored))

    result = await GetPublicPerfume(world.perfumes, FixedClock()).execute(old_slug)

    assert isinstance(result, Ok)
    assert result.value.slug == "versace-eros-flame-edt"
