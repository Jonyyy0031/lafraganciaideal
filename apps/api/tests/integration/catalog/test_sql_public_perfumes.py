"""Plan 004: the public catalog queries and the slug history against `fragancia_test`."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, insert, select, text

from fragancia_api.container import Container
from fragancia_api.modules.catalog.application.commands.perfumes import (
    ArchivePerfume,
    CreatePerfume,
    HidePerfume,
    PublishPerfume,
    UpdatePerfume,
)
from fragancia_api.modules.catalog.application.commands.presentations import (
    AddPresentation,
    ArchivePresentation,
)
from fragancia_api.modules.catalog.application.ports import PublicPerfumeFilters
from fragancia_api.modules.catalog.contracts import (
    PerfumeRequest,
    PresentationRequest,
    PublicPerfume,
    PublicPerfumePage,
)
from fragancia_api.modules.catalog.domain.naming import slugify
from fragancia_api.modules.catalog.domain.perfume import (
    Availability,
    Ml,
    Presentation,
    Price,
    Sale,
)
from fragancia_api.modules.catalog.infrastructure.sql_perfume_queries import SqlPerfumeQueries
from fragancia_api.modules.catalog.infrastructure.tables import (
    brands,
    concentrations,
    olfactory_families,
    perfume_slug_history,
    perfumes,
    presentations,
)
from fragancia_api.shared.kernel import Money, Ok, new_id

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
MICRO = timedelta(microseconds=1)
DAY = timedelta(days=1)
CREATED = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

NO_FILTERS = PublicPerfumeFilters(
    q=None,
    brands=(),
    families=(),
    genders=(),
    min_price_cents=None,
    max_price_cents=None,
    sort="name",
    page=1,
    size=48,
)


@dataclass(frozen=True)
class Shop:
    """Rows this module owns: brands, families and concentrations of every visibility."""

    lancome: UUID  # "Zz Lancôme", active
    versace: UUID  # "zz versace", active (lower case: sorts after "Zz Lancôme" only ignoring case)
    closed: UUID  # "Zz Closed", archived brand
    floral: UUID  # active family
    woody: UUID  # archived family
    edp: UUID  # active concentration
    edt: UUID  # archived concentration
    queries: SqlPerfumeQueries


def _named(row_id: UUID, name: str, *, active: bool = True) -> dict[str, Any]:
    return {
        "id": row_id,
        "name": name,
        "slug": slugify(name),
        "is_active": active,
        "created_at": CREATED,
    }


def _concentration(row_id: UUID, name: str, abbreviation: str, *, active: bool) -> dict[str, Any]:
    return {
        **_named(row_id, name, active=active),
        "abbreviation": abbreviation,
        "abbreviation_slug": slugify(abbreviation),
    }


@pytest.fixture
async def shop(container: Container) -> AsyncIterator[Shop]:
    ids = [new_id() for _ in range(7)]
    lancome, versace, closed, floral, woody, edp, edt = ids
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE catalog.perfumes CASCADE"))
        await connection.execute(
            insert(brands),
            [
                _named(lancome, "Zz Lancôme"),
                _named(versace, "zz versace"),
                _named(closed, "Zz Closed", active=False),
            ],
        )
        await connection.execute(
            insert(olfactory_families),
            [_named(floral, "Zz Floral"), _named(woody, "Zz Woody", active=False)],
        )
        await connection.execute(
            insert(concentrations),
            [
                _concentration(edp, "Zz Eau de Parfum", "ZEP", active=True),
                _concentration(edt, "Zz Eau de Toilette", "ZET", active=False),
            ],
        )
    yield Shop(
        lancome, versace, closed, floral, woody, edp, edt, SqlPerfumeQueries(container.database)
    )
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE catalog.perfumes CASCADE"))
        await connection.execute(delete(brands).where(brands.c.id.in_([lancome, versace, closed])))
        await connection.execute(
            delete(olfactory_families).where(olfactory_families.c.id.in_([floral, woody]))
        )
        await connection.execute(delete(concentrations).where(concentrations.c.id.in_([edp, edt])))


# --- seeding helpers (direct rows, so every field is under the test's control) ----------------


async def _perfume(
    container: Container,
    shop: Shop,
    name: str = "Eros",
    *,
    brand: UUID | None = None,
    family: UUID | None = None,
    concentration: UUID | None = None,
    gender: str = "men",
    published: bool = True,
    archived: bool = False,
    first_published_at: datetime | None = CREATED,
    top: list[str] | None = None,
    heart: list[str] | None = None,
    base: list[str] | None = None,
    perfume_id: UUID | None = None,
    sizes: tuple[dict[str, Any], ...] = ({},),
) -> UUID:
    """Insert a perfume and its presentations (each dict overrides the defaults of one)."""
    row_id = perfume_id or new_id()
    values = {
        "id": row_id,
        "brand_id": brand or shop.lancome,
        "concentration_id": concentration or shop.edp,
        "family_id": family or shop.floral,
        "name": name,
        "name_slug": slugify(name),
        "slug": f"{slugify(name)}-{row_id}",
        "gender": gender,
        "description": "",
        "top_notes": top or [],
        "heart_notes": heart or [],
        "base_notes": base or [],
        "is_published": published,
        "first_published_at": first_published_at if published else None,
        "is_archived": archived,
        "created_at": CREATED,
        "updated_at": CREATED,
    }
    async with container.database.engine.begin() as connection:
        await connection.execute(insert(perfumes).values(values))
        for position, size in enumerate(sizes):
            await connection.execute(
                insert(presentations).values(
                    {
                        "id": new_id(),
                        "perfume_id": row_id,
                        "ml": 100 + position,
                        "price_cents": 250_000,
                        "availability": "in_stock",
                        "is_active": True,
                        "created_at": CREATED,
                        **size,
                    }
                )
            )
    return row_id


async def _slug(container: Container, perfume_id: UUID) -> str:
    async with container.database.reader() as session:
        return str(await session.scalar(select(perfumes.c.slug).where(perfumes.c.id == perfume_id)))


async def _list(shop: Shop, now: datetime = NOW, **overrides: Any) -> PublicPerfumePage:
    return await shop.queries.list_public(replace(NO_FILTERS, **overrides), now=now)


def _names(page: PublicPerfumePage) -> list[str]:
    return [card.name for card in page.items]


# --- visibility -------------------------------------------------------------------------------


async def test_only_published_unarchived_perfumes_of_active_brands_are_listed(
    container: Container, shop: Shop
) -> None:
    await _perfume(container, shop, "Visible")
    await _perfume(container, shop, "Hidden", published=False)
    await _perfume(container, shop, "Archived", archived=True)
    await _perfume(container, shop, "Archived Brand", brand=shop.closed)

    page = await _list(shop)

    assert _names(page) == ["Visible"]
    assert page.total == 1


async def test_an_archived_family_or_concentration_does_not_hide_a_perfume(
    container: Container, shop: Shop
) -> None:
    await _perfume(container, shop, "Archived Family", family=shop.woody)
    await _perfume(container, shop, "Archived Concentration", concentration=shop.edt)

    page = await _list(shop)

    assert sorted(_names(page)) == ["Archived Concentration", "Archived Family"]


async def test_a_perfume_without_active_presentations_is_not_listed(
    container: Container, shop: Shop
) -> None:
    await _perfume(container, shop, "Only Archived Size", sizes=({"is_active": False},))
    await _perfume(container, shop, "No Sizes", sizes=())

    assert (await _list(shop)).items == []


async def test_card_fields_come_from_the_perfume_and_its_refs(
    container: Container, shop: Shop
) -> None:
    await _perfume(container, shop, "Idole", gender="women")

    [card] = (await _list(shop)).items

    assert card.name == "Idole"
    assert card.gender == "women"
    assert card.slug.startswith("idole-")
    assert (card.brand.name, card.brand.slug) == ("Zz Lancôme", "zz-lancome")
    assert (card.concentration.name, card.concentration.abbreviation) == (
        "Zz Eau de Parfum",
        "ZEP",
    )
    assert (card.family.name, card.family.slug) == ("Zz Floral", "zz-floral")


# --- effective price and on_sale: SQL agrees with the domain ----------------------------------

SALE = 200_000
WINDOWS = {
    "no window": (None, None),
    "starts exactly now": (NOW, None),
    "starts one microsecond from now": (NOW + MICRO, None),
    "starts one microsecond ago": (NOW - MICRO, None),
    "ends exactly now": (None, NOW),
    "ends one microsecond from now": (None, NOW + MICRO),
    "ended one microsecond ago": (None, NOW - MICRO),
    "window opens now": (NOW, NOW + DAY),
    "window closes now": (NOW - DAY, NOW),
    "inside the window": (NOW - DAY, NOW + DAY),
    "future window": (NOW + DAY, NOW + 2 * DAY),
    "past window": (NOW - 2 * DAY, NOW - DAY),
}


def _domain_price(row: dict[str, Any], now: datetime) -> tuple[int, bool]:
    sale = (
        Sale(Money(row["sale_price_cents"]), row["sale_starts_at"], row["sale_ends_at"])
        if row["sale_price_cents"] is not None
        else None
    )
    presentation = Presentation(
        id=row["id"],
        ml=Ml(row["ml"]),
        price=Price(Money(row["price_cents"])),
        sale=sale,
        availability=Availability("in_stock", None, None),
        is_active=True,
        created_at=CREATED,
    )
    return presentation.effective_price(now).cents, sale is not None and sale.is_active(now)


@pytest.mark.parametrize("label", list(WINDOWS))
async def test_sql_effective_price_and_on_sale_agree_with_the_domain_at_the_boundaries(
    container: Container, shop: Shop, label: str
) -> None:
    starts_at, ends_at = WINDOWS[label]
    perfume_id = await _perfume(
        container,
        shop,
        sizes=(
            {
                "sale_price_cents": SALE,
                "sale_starts_at": starts_at,
                "sale_ends_at": ends_at,
            },
        ),
    )
    async with container.database.reader() as session:
        row = dict(
            (
                await session.execute(
                    select(presentations).where(presentations.c.perfume_id == perfume_id)
                )
            )
            .mappings()
            .one()
        )
    expected_price, expected_on_sale = _domain_price(row, NOW)

    [card] = (await _list(shop)).items
    detail = await shop.queries.get_public(card.slug, now=NOW)

    assert (card.price_from_cents, card.on_sale) == (expected_price, expected_on_sale)
    assert detail is not None
    [presentation] = detail.presentations
    assert presentation.price_cents == expected_price
    assert (presentation.regular_price_cents is not None) is expected_on_sale
    assert (presentation.sale_ends_at is not None) is (expected_on_sale and ends_at is not None)


async def test_the_boundary_matrix_covers_both_outcomes() -> None:
    outcomes = {
        _domain_price(
            {
                "id": new_id(),
                "ml": 100,
                "price_cents": 250_000,
                "sale_price_cents": SALE,
                "sale_starts_at": starts_at,
                "sale_ends_at": ends_at,
            },
            NOW,
        )[1]
        for starts_at, ends_at in WINDOWS.values()
    }
    assert outcomes == {True, False}


async def test_a_sale_ending_in_the_future_reports_the_regular_price_and_the_end(
    container: Container, shop: Shop
) -> None:
    await _perfume(
        container,
        shop,
        sizes=({"price_cents": 390_000, "sale_price_cents": 350_000, "sale_ends_at": NOW + DAY},),
    )

    [card] = (await _list(shop)).items
    detail = await shop.queries.get_public(card.slug, now=NOW)

    assert detail is not None
    [presentation] = detail.presentations
    assert (presentation.price_cents, presentation.regular_price_cents) == (350_000, 390_000)
    assert presentation.sale_ends_at == NOW + DAY


async def test_the_from_price_is_the_lowest_effective_price_among_active_presentations(
    container: Container, shop: Shop
) -> None:
    await _perfume(
        container,
        shop,
        sizes=(
            {"price_cents": 250_000},
            {"price_cents": 390_000, "sale_price_cents": 350_000},  # active sale, not the lowest
            {"price_cents": 500_000, "sale_price_cents": 100_000, "sale_starts_at": NOW + DAY},
            {"price_cents": 80_000, "is_active": False},  # cheaper, but archived
        ),
    )

    [card] = (await _list(shop)).items

    assert card.price_from_cents == 250_000
    assert card.on_sale is True  # the 390000 -> 350000 one


async def test_a_sale_makes_its_presentation_the_cheapest(container: Container, shop: Shop) -> None:
    await _perfume(
        container,
        shop,
        sizes=({"price_cents": 250_000}, {"price_cents": 390_000, "sale_price_cents": 200_000}),
    )

    [card] = (await _list(shop)).items

    assert (card.price_from_cents, card.on_sale) == (200_000, True)


async def test_on_sale_ignores_the_sale_of_an_archived_presentation(
    container: Container, shop: Shop
) -> None:
    await _perfume(
        container,
        shop,
        sizes=({}, {"price_cents": 390_000, "sale_price_cents": 100_000, "is_active": False}),
    )

    [card] = (await _list(shop)).items

    assert (card.price_from_cents, card.on_sale) == (250_000, False)


async def test_the_price_depends_on_the_now_passed_to_the_query(
    container: Container, shop: Shop
) -> None:
    await _perfume(container, shop, sizes=({"sale_price_cents": SALE, "sale_ends_at": NOW + DAY},))

    before = (await _list(shop, now=NOW)).items[0]
    after = (await _list(shop, now=NOW + DAY)).items[0]

    assert (before.price_from_cents, before.on_sale) == (SALE, True)
    assert (after.price_from_cents, after.on_sale) == (250_000, False)


# --- filters ----------------------------------------------------------------------------------


async def _filter_world(container: Container, shop: Shop) -> None:
    await _perfume(
        container, shop, "Lanc Floral Women", gender="women", sizes=({"price_cents": 250_000},)
    )
    await _perfume(
        container,
        shop,
        "Vers Woody Men",
        brand=shop.versace,
        family=shop.woody,
        gender="men",
        sizes=({"price_cents": 400_000},),
    )
    await _perfume(
        container,
        shop,
        "Vers Floral Unisex",
        brand=shop.versace,
        gender="unisex",
        sizes=({"price_cents": 600_000},),
    )


async def test_the_brand_filter_accepts_several_slugs(container: Container, shop: Shop) -> None:
    await _filter_world(container, shop)

    one = await _list(shop, brands=("zz-versace",))
    both = await _list(shop, brands=("zz-versace", "zz-lancome"))

    assert sorted(_names(one)) == ["Vers Floral Unisex", "Vers Woody Men"]
    assert one.total == 2
    assert both.total == 3


async def test_the_family_filter(container: Container, shop: Shop) -> None:
    await _filter_world(container, shop)

    assert _names(await _list(shop, families=("zz-woody",))) == ["Vers Woody Men"]
    both = await _list(shop, families=("zz-woody", "zz-floral"))
    assert both.total == 3


async def test_the_gender_filter_accepts_several_values(container: Container, shop: Shop) -> None:
    await _filter_world(container, shop)

    assert _names(await _list(shop, genders=("men",))) == ["Vers Woody Men"]
    assert sorted(_names(await _list(shop, genders=("women", "unisex")))) == [
        "Lanc Floral Women",
        "Vers Floral Unisex",
    ]


async def test_the_price_range_filters_the_from_price_inclusively(
    container: Container, shop: Shop
) -> None:
    await _filter_world(container, shop)

    assert (await _list(shop, min_price_cents=250_001)).total == 2
    assert (await _list(shop, min_price_cents=250_000)).total == 3
    assert (await _list(shop, max_price_cents=399_999)).total == 1
    assert (await _list(shop, max_price_cents=400_000)).total == 2
    assert _names(await _list(shop, min_price_cents=300_000, max_price_cents=500_000)) == [
        "Vers Woody Men"
    ]
    assert (await _list(shop, min_price_cents=700_000)).items == []
    assert (await _list(shop, min_price_cents=500_000, max_price_cents=300_000)).items == []


async def test_the_price_range_uses_the_effective_from_price_not_the_regular_one(
    container: Container, shop: Shop
) -> None:
    await _perfume(
        container,
        shop,
        "Discounted",
        sizes=({"price_cents": 500_000, "sale_price_cents": 200_000},),
    )

    assert (await _list(shop, max_price_cents=250_000)).total == 1
    assert (await _list(shop, min_price_cents=300_000)).total == 0


async def test_filters_combine_with_and(container: Container, shop: Shop) -> None:
    await _filter_world(container, shop)

    page = await _list(
        shop,
        brands=("zz-versace",),
        families=("zz-floral",),
        genders=("unisex",),
        min_price_cents=500_000,
    )

    assert _names(page) == ["Vers Floral Unisex"]
    assert (await _list(shop, brands=("zz-lancome",), genders=("men",))).items == []


async def test_an_unknown_slug_in_a_filter_matches_nothing(
    container: Container, shop: Shop
) -> None:
    await _filter_world(container, shop)

    assert (await _list(shop, brands=("nobody",))).items == []
    assert (await _list(shop, families=("nothing",))).total == 0


async def test_filters_never_reveal_non_visible_perfumes(container: Container, shop: Shop) -> None:
    await _perfume(container, shop, "Closed Shop", brand=shop.closed)
    await _perfume(container, shop, "Not Published", published=False)

    assert (await _list(shop, brands=("zz-closed",))).items == []
    assert (await _list(shop, genders=("men",))).items == []


# --- search -----------------------------------------------------------------------------------


async def _search_world(container: Container, shop: Shop) -> None:
    await _perfume(
        container,
        shop,
        "Idôle",
        top=["Rose"],
        heart=["Jazmín"],
        base=["Vainilla", "Ámbar"],
        gender="women",
    )
    await _perfume(
        container, shop, "Sauvage", brand=shop.versace, top=["Bergamota"], base=["Cedro"]
    )


async def test_search_finds_a_brand_ignoring_accents_and_case(
    container: Container, shop: Shop
) -> None:
    await _search_world(container, shop)

    for q in ("lanc", "LANC", "lancôme", "LANCOME", "zz lancôme"):
        assert _names(await _list(shop, q=q)) == ["Idôle"], q


async def test_search_finds_a_perfume_name_ignoring_accents_and_case(
    container: Container, shop: Shop
) -> None:
    await _search_world(container, shop)

    assert _names(await _list(shop, q="idole")) == ["Idôle"]
    assert _names(await _list(shop, q="IDÔLE")) == ["Idôle"]
    assert _names(await _list(shop, q="sauv")) == ["Sauvage"]


async def test_search_finds_notes_of_any_level(container: Container, shop: Shop) -> None:
    await _search_world(container, shop)

    assert _names(await _list(shop, q="VAIN")) == ["Idôle"]  # base
    assert _names(await _list(shop, q="jazmin")) == ["Idôle"]  # heart, accent in data only
    assert _names(await _list(shop, q="rose")) == ["Idôle"]  # top
    assert _names(await _list(shop, q="ambar")) == ["Idôle"]
    assert _names(await _list(shop, q="bergam")) == ["Sauvage"]


async def test_search_matches_substrings_not_only_word_starts(
    container: Container, shop: Shop
) -> None:
    await _search_world(container, shop)

    assert _names(await _list(shop, q="auvag")) == ["Sauvage"]


async def test_search_with_no_match_is_an_empty_page(container: Container, shop: Shop) -> None:
    await _search_world(container, shop)

    page = await _list(shop, q="zzzzz")

    assert (page.items, page.total) == ([], 0)


async def test_search_combines_with_the_other_filters(container: Container, shop: Shop) -> None:
    await _search_world(container, shop)

    assert _names(await _list(shop, q="zz", brands=("zz-versace",))) == ["Sauvage"]
    assert (await _list(shop, q="vain", brands=("zz-versace",))).items == []


async def test_search_never_reveals_non_visible_perfumes(container: Container, shop: Shop) -> None:
    await _perfume(container, shop, "Secret Oud", published=False)
    await _perfume(container, shop, "Closed Oud", brand=shop.closed)

    assert (await _list(shop, q="oud")).items == []


async def test_search_treats_percent_and_underscore_and_backslash_literally(
    container: Container, shop: Shop
) -> None:
    await _perfume(container, shop, "Plain Perfume", top=["Oud"])
    await _perfume(container, shop, "Percent", top=["100% Oud"])
    await _perfume(container, shop, "Under", top=["a_b"])
    await _perfume(container, shop, "Slash", top=["c\\d"])

    assert _names(await _list(shop, q="100%")) == ["Percent"]
    assert _names(await _list(shop, q="%")) == ["Percent"]  # not "everything"
    assert (await _list(shop, q="50%")).items == []
    assert _names(await _list(shop, q="a_b")) == ["Under"]
    assert _names(await _list(shop, q="_")) == ["Under"]  # not "any single character"
    assert (await _list(shop, q="p_ain")).items == []
    assert _names(await _list(shop, q="c\\d")) == ["Slash"]
    assert _names(await _list(shop, q="\\")) == ["Slash"]
    # "%" is not a wildcard: as one, "%oud" would match both perfumes with the note "Oud"
    assert (await _list(shop, q="%oud")).items == []


async def test_search_with_sql_looking_text_is_inert(container: Container, shop: Shop) -> None:
    await _perfume(container, shop, "Plain Perfume")

    page = await _list(shop, q="'; DROP TABLE catalog.perfumes; --")

    assert page.items == []
    assert (await _list(shop)).total == 1


# --- sorting and pagination -------------------------------------------------------------------


async def test_the_default_order_is_brand_then_name_ignoring_case(
    container: Container, shop: Shop
) -> None:
    await _perfume(container, shop, "beta", brand=shop.versace)
    await _perfume(container, shop, "Alpha", brand=shop.versace)
    await _perfume(container, shop, "Zeta", brand=shop.lancome)
    await _perfume(container, shop, "alpha", brand=shop.lancome, concentration=shop.edt)

    page = await _list(shop)

    # "Zz Lancôme" < "zz versace" only compared case-insensitively
    assert [(c.brand.name, c.name) for c in page.items] == [
        ("Zz Lancôme", "alpha"),
        ("Zz Lancôme", "Zeta"),
        ("zz versace", "Alpha"),
        ("zz versace", "beta"),
    ]


async def test_name_ties_break_by_id(container: Container, shop: Shop) -> None:
    first, second = UUID(int=1), UUID(int=2)
    # the same brand and name under two concentrations
    await _perfume(container, shop, "Same", perfume_id=second)
    await _perfume(container, shop, "Same", perfume_id=first, concentration=shop.edt)

    page = await _list(shop)

    assert [c.concentration.abbreviation for c in page.items] == ["ZET", "ZEP"]


async def test_price_ascending_orders_by_the_from_price_then_name(
    container: Container, shop: Shop
) -> None:
    await _perfume(container, shop, "Mid", sizes=({"price_cents": 300_000},))
    await _perfume(container, shop, "Cheap B", sizes=({"price_cents": 100_000},))
    await _perfume(container, shop, "Cheap A", sizes=({"price_cents": 100_000},))
    await _perfume(container, shop, "Top", sizes=({"price_cents": 900_000},))
    await _perfume(
        container,
        shop,
        "On Sale",
        sizes=({"price_cents": 900_000, "sale_price_cents": 200_000},),
    )

    asc = await _list(shop, sort="price_asc")

    assert _names(asc) == ["Cheap A", "Cheap B", "On Sale", "Mid", "Top"]
    assert [c.price_from_cents for c in asc.items] == [100_000, 100_000, 200_000, 300_000, 900_000]


async def test_price_descending_orders_by_the_from_price_then_name(
    container: Container, shop: Shop
) -> None:
    await _perfume(container, shop, "Mid", sizes=({"price_cents": 300_000},))
    await _perfume(container, shop, "Cheap B", sizes=({"price_cents": 100_000},))
    await _perfume(container, shop, "Cheap A", sizes=({"price_cents": 100_000},))
    await _perfume(container, shop, "Top", sizes=({"price_cents": 900_000},))

    desc = await _list(shop, sort="price_desc")

    assert _names(desc) == ["Top", "Mid", "Cheap A", "Cheap B"]  # ties still by name ascending


async def test_price_sort_uses_the_lowest_of_several_presentations(
    container: Container, shop: Shop
) -> None:
    await _perfume(
        container, shop, "Wide", sizes=({"price_cents": 700_000}, {"price_cents": 50_000})
    )
    await _perfume(container, shop, "Narrow", sizes=({"price_cents": 100_000},))

    assert _names(await _list(shop, sort="price_asc")) == ["Wide", "Narrow"]


async def test_newest_orders_by_first_publication_descending_then_id(
    container: Container, shop: Shop
) -> None:
    await _perfume(container, shop, "Old", first_published_at=NOW - 10 * DAY)
    await _perfume(container, shop, "New", first_published_at=NOW - DAY)
    await _perfume(
        container, shop, "Tie B", first_published_at=NOW - 5 * DAY, perfume_id=UUID(int=2)
    )
    await _perfume(
        container, shop, "Tie A", first_published_at=NOW - 5 * DAY, perfume_id=UUID(int=1)
    )

    assert _names(await _list(shop, sort="newest")) == ["New", "Tie A", "Tie B", "Old"]


async def test_newest_puts_a_missing_publication_date_last(
    container: Container, shop: Shop
) -> None:
    await _perfume(container, shop, "Undated", first_published_at=None)
    await _perfume(container, shop, "Dated", first_published_at=NOW - 10 * DAY)

    assert _names(await _list(shop, sort="newest")) == ["Dated", "Undated"]


async def test_pagination_slices_the_ordered_list_and_reports_the_total(
    container: Container, shop: Shop
) -> None:
    for letter in "ABCDE":
        await _perfume(container, shop, f"Perfume {letter}")

    first = await _list(shop, page=1, size=2)
    second = await _list(shop, page=2, size=2)
    last = await _list(shop, page=3, size=2)
    beyond = await _list(shop, page=4, size=2)

    assert _names(first) == ["Perfume A", "Perfume B"]
    assert _names(second) == ["Perfume C", "Perfume D"]
    assert _names(last) == ["Perfume E"]
    assert beyond.items == []
    assert [p.total for p in (first, second, last, beyond)] == [5, 5, 5, 5]
    assert (second.page, second.size) == (2, 2)


async def test_the_total_counts_the_filtered_set_not_the_page(
    container: Container, shop: Shop
) -> None:
    for letter in "ABC":
        await _perfume(container, shop, f"Lanc {letter}")
    await _perfume(container, shop, "Vers D", brand=shop.versace)

    page = await _list(shop, brands=("zz-lancome",), size=2)

    assert (len(page.items), page.total) == (2, 3)


async def test_a_perfume_with_many_presentations_is_listed_once(
    container: Container, shop: Shop
) -> None:
    await _perfume(container, shop, sizes=({}, {}, {}))

    page = await _list(shop)

    assert (len(page.items), page.total) == (1, 1)


# --- detail -----------------------------------------------------------------------------------


async def test_the_detail_lists_active_presentations_ordered_by_ml(
    container: Container, shop: Shop
) -> None:
    row_id = await _perfume(
        container,
        shop,
        "Detailed",
        top=["Mint"],
        heart=["Rose"],
        base=["Musk", "Musk"],
        sizes=(
            {"ml": 100},
            {
                "ml": 30,
                "availability": "made_to_order",
                "lead_time_min_days": 3,
                "lead_time_max_days": 5,
            },
            {"ml": 50, "is_active": False},
            {"ml": 10},
        ),
    )
    slug = await _slug(container, row_id)

    detail = await shop.queries.get_public(slug, now=NOW)

    assert detail is not None
    assert detail.slug == slug
    assert (detail.name, detail.gender) == ("Detailed", "men")
    assert (detail.top_notes, detail.heart_notes, detail.base_notes) == (
        ["Mint"],
        ["Rose"],
        ["Musk", "Musk"],
    )
    assert (detail.brand.name, detail.brand.slug) == ("Zz Lancôme", "zz-lancome")
    assert detail.family.slug == "zz-floral"
    assert detail.concentration.abbreviation == "ZEP"
    assert [p.ml for p in detail.presentations] == [10, 30, 100]
    made_to_order = detail.presentations[1]
    assert (made_to_order.availability, made_to_order.lead_time_min_days) == ("made_to_order", 3)
    assert made_to_order.lead_time_max_days == 5
    in_stock = detail.presentations[0]
    assert (in_stock.lead_time_min_days, in_stock.lead_time_max_days) == (None, None)
    assert (in_stock.regular_price_cents, in_stock.sale_ends_at) == (None, None)


async def test_the_detail_is_found_for_a_family_or_concentration_archived_perfume(
    container: Container, shop: Shop
) -> None:
    row_id = await _perfume(container, shop, family=shop.woody, concentration=shop.edt)

    assert await shop.queries.get_public(await _slug(container, row_id), now=NOW) is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"published": False},
        {"archived": True},
        {"brand": "closed"},
    ],
)
async def test_the_detail_of_a_non_visible_perfume_is_not_found(
    container: Container, shop: Shop, overrides: dict[str, Any]
) -> None:
    if overrides.get("brand") == "closed":
        overrides = {"brand": shop.closed}
    row_id = await _perfume(container, shop, **overrides)

    assert await shop.queries.get_public(await _slug(container, row_id), now=NOW) is None


async def test_an_unknown_slug_is_not_found(container: Container, shop: Shop) -> None:
    await _perfume(container, shop)

    assert await shop.queries.get_public("not-a-slug", now=NOW) is None


# --- slug history (through the real commands and repository) ----------------------------------


def _request(shop: Shop, **overrides: Any) -> PerfumeRequest:
    values: dict[str, Any] = {
        "brand_id": shop.lancome,
        "concentration_id": shop.edp,
        "family_id": shop.floral,
        "name": "Eros",
        "gender": "men",
    }
    values.update(overrides)
    return PerfumeRequest.model_validate(values)


async def _publish_new(container: Container, shop: Shop, **overrides: Any) -> UUID:
    created = await container.services.get(CreatePerfume).execute(_request(shop, **overrides))
    assert isinstance(created, Ok), created
    perfume_id = created.value
    added = await container.services.get(AddPresentation).execute(
        perfume_id,
        PresentationRequest.model_validate(
            {"ml": 100, "price_cents": 250_000, "availability": "in_stock"}
        ),
    )
    assert isinstance(added, Ok), added
    published = await container.services.get(PublishPerfume).execute(perfume_id)
    assert isinstance(published, Ok), published
    return perfume_id


async def _rename(container: Container, shop: Shop, perfume_id: UUID, name: str) -> None:
    result = await container.services.get(UpdatePerfume).execute(
        perfume_id, _request(shop, name=name)
    )
    assert isinstance(result, Ok), result


async def _history(container: Container) -> dict[str, tuple[UUID, datetime]]:
    async with container.database.reader() as session:
        rows = (await session.execute(select(perfume_slug_history))).mappings().all()
    return {row["slug"]: (row["perfume_id"], row["retired_at"]) for row in rows}


async def test_renaming_writes_the_old_slug_to_the_history(
    container: Container, shop: Shop
) -> None:
    perfume_id = await _publish_new(container, shop)
    old_slug = await _slug(container, perfume_id)
    assert old_slug == "zz-lancome-eros-zep"

    await _rename(container, shop, perfume_id, "Eros Flame")

    history = await _history(container)
    assert list(history) == [old_slug]
    assert history[old_slug][0] == perfume_id
    assert await _slug(container, perfume_id) == "zz-lancome-eros-flame-zep"
    async with container.database.reader() as session:
        updated_at = await session.scalar(
            select(perfumes.c.updated_at).where(perfumes.c.id == perfume_id)
        )
    assert history[old_slug][1] == updated_at


async def test_an_update_that_keeps_the_slug_writes_no_history(
    container: Container, shop: Shop
) -> None:
    perfume_id = await _publish_new(container, shop)

    await _rename(container, shop, perfume_id, "Eros")
    await _rename(container, shop, perfume_id, "  EROS ")

    assert await _history(container) == {}


async def test_the_old_slug_finds_the_perfume_and_answers_the_current_slug(
    container: Container, shop: Shop
) -> None:
    perfume_id = await _publish_new(container, shop)
    old_slug = await _slug(container, perfume_id)
    await _rename(container, shop, perfume_id, "Eros Flame")

    by_old = await shop.queries.get_public(old_slug, now=NOW)
    by_current = await shop.queries.get_public("zz-lancome-eros-flame-zep", now=NOW)

    assert by_old is not None
    assert by_old.slug == "zz-lancome-eros-flame-zep"
    assert by_old == by_current


async def test_every_retired_slug_of_successive_renames_still_resolves(
    container: Container, shop: Shop
) -> None:
    perfume_id = await _publish_new(container, shop)
    await _rename(container, shop, perfume_id, "Eros Flame")
    await _rename(container, shop, perfume_id, "Eros Black")

    for old in ("zz-lancome-eros-zep", "zz-lancome-eros-flame-zep", "zz-lancome-eros-black-zep"):
        detail = await shop.queries.get_public(old, now=NOW)
        assert detail is not None, old
        assert detail.slug == "zz-lancome-eros-black-zep"
    assert len(await _history(container)) == 2


async def test_the_history_does_not_make_a_hidden_perfume_visible(
    container: Container, shop: Shop
) -> None:
    perfume_id = await _publish_new(container, shop)
    await _rename(container, shop, perfume_id, "Eros Flame")

    hidden = await container.services.get(HidePerfume).execute(perfume_id)
    assert isinstance(hidden, Ok)
    assert await shop.queries.get_public("zz-lancome-eros-zep", now=NOW) is None

    # back to visible, then archived
    assert isinstance(await container.services.get(PublishPerfume).execute(perfume_id), Ok)
    assert await shop.queries.get_public("zz-lancome-eros-zep", now=NOW) is not None
    await container.services.get(ArchivePerfume).execute(perfume_id)
    assert await shop.queries.get_public("zz-lancome-eros-zep", now=NOW) is None


async def test_the_history_does_not_make_an_archived_brand_perfume_visible(
    container: Container, shop: Shop
) -> None:
    perfume_id = await _publish_new(container, shop)
    await _rename(container, shop, perfume_id, "Eros Flame")
    async with container.database.engine.begin() as connection:
        await connection.execute(
            brands.update().where(brands.c.id == shop.lancome).values(is_active=False)
        )

    assert await shop.queries.get_public("zz-lancome-eros-zep", now=NOW) is None


async def test_a_slug_is_owned_by_the_latest_perfume_that_dropped_it(
    container: Container, shop: Shop
) -> None:
    first = await _publish_new(container, shop)
    shared = "zz-lancome-eros-zep"
    await _rename(container, shop, first, "Eros Flame")  # history: shared -> first
    second = await _publish_new(container, shop)  # takes the freed slug as its current one
    assert await _slug(container, second) == shared

    # while it is somebody's current slug, the current owner answers
    current = await shop.queries.get_public(shared, now=NOW)
    assert current is not None
    assert current.name == "Eros"
    assert (await _history(container))[shared][0] == first

    await _rename(container, shop, second, "Eros Noir")  # history: shared -> second

    assert (await _history(container))[shared][0] == second
    owner = await shop.queries.get_public(shared, now=NOW)
    assert owner is not None
    assert owner.slug == "zz-lancome-eros-noir-zep"
    assert len(await _history(container)) == 1  # one row per slug (primary key)


async def test_archiving_a_presentation_hides_it_from_the_public_detail(
    container: Container, shop: Shop
) -> None:
    perfume_id = await _publish_new(container, shop)
    extra = await container.services.get(AddPresentation).execute(
        perfume_id,
        PresentationRequest.model_validate(
            {"ml": 50, "price_cents": 150_000, "availability": "in_stock"}
        ),
    )
    assert isinstance(extra, Ok)

    before = await shop.queries.get_public(await _slug(container, perfume_id), now=NOW)
    archived = await container.services.get(ArchivePresentation).execute(perfume_id, extra.value)
    assert isinstance(archived, Ok)
    after = await shop.queries.get_public(await _slug(container, perfume_id), now=NOW)
    page = await _list(shop)

    assert before is not None
    assert [p.ml for p in before.presentations] == [50, 100]
    assert after is not None
    assert [p.ml for p in after.presentations] == [100]
    assert page.items[0].price_from_cents == 250_000


# --- the migration ----------------------------------------------------------------------------


async def test_the_unaccent_extension_is_installed(container: Container) -> None:
    async with container.database.reader() as session:
        installed = await session.scalar(
            text("SELECT count(*) FROM pg_extension WHERE extname = 'unaccent'")
        )
        folded = await session.scalar(text("SELECT unaccent('Lancôme')"))

    assert installed == 1
    assert folded == "Lancome"


async def test_a_history_slug_cannot_point_to_a_missing_perfume(container: Container) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError, match="foreign key"):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                insert(perfume_slug_history).values(
                    slug="orphan", perfume_id=UUID(int=404), retired_at=NOW
                )
            )


async def test_the_history_slug_is_the_primary_key(container: Container, shop: Shop) -> None:
    from sqlalchemy.exc import IntegrityError

    perfume_id = await _perfume(container, shop)
    async with container.database.engine.begin() as connection:
        await connection.execute(
            insert(perfume_slug_history).values(slug="dup", perfume_id=perfume_id, retired_at=NOW)
        )

    with pytest.raises(IntegrityError, match="pk_perfume_slug_history"):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                insert(perfume_slug_history).values(
                    slug="dup", perfume_id=perfume_id, retired_at=NOW
                )
            )


async def test_a_detail_model_is_returned_as_the_public_contract(
    container: Container, shop: Shop
) -> None:
    row_id = await _perfume(container, shop)

    detail = await shop.queries.get_public(await _slug(container, row_id), now=NOW)

    assert isinstance(detail, PublicPerfume)
