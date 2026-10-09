"""Plan 004: the effective-price rule and the slug history, in the domain."""

from datetime import datetime, timedelta, timezone

import pytest

from fragancia_api.modules.catalog.domain.perfume import (
    Availability,
    Description,
    Gender,
    Ml,
    Notes,
    Perfume,
    PerfumeName,
    Presentation,
    Price,
    Sale,
)
from fragancia_api.shared.kernel import Money, Ok, new_id
from tests.unit.catalog.perfume_support import (
    LATER,
    NOW,
    make_brand,
    make_concentration,
    make_family,
    make_perfume,
    unwrap_ok,
)

SECOND = timedelta(seconds=1)


def _sale(starts_at: datetime | None, ends_at: datetime | None) -> Sale:
    return Sale(Money(200_000), starts_at, ends_at)


def _presentation(sale: Sale | None) -> Presentation:
    return Presentation(
        id=new_id(),
        ml=Ml(100),
        price=Price(Money(250_000)),
        sale=sale,
        availability=Availability("in_stock", None, None),
        is_active=True,
        created_at=NOW,
    )


# --- Sale.is_active ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("starts_at", "ends_at", "active"),
    [
        (None, None, True),  # no window: always active
        (NOW, None, True),  # the start is inclusive
        (NOW + SECOND, None, False),  # starts in the future
        (NOW - SECOND, None, True),
        (None, NOW, False),  # the end is exclusive
        (None, NOW + SECOND, True),
        (None, NOW - SECOND, False),  # already over
        (NOW, NOW + SECOND, True),  # a window that has just opened
        (NOW - SECOND, NOW, False),  # a window that has just closed
        (NOW - timedelta(days=1), NOW + timedelta(days=1), True),
        (NOW + SECOND, NOW + 2 * SECOND, False),
    ],
)
def test_sale_is_active_with_the_start_inclusive_and_the_end_exclusive(
    starts_at: datetime | None, ends_at: datetime | None, active: bool
) -> None:
    assert _sale(starts_at, ends_at).is_active(NOW) is active


def test_sale_activity_is_compared_as_instants_across_time_zones() -> None:
    mexico = timezone(timedelta(hours=-6))
    starts_at = datetime(2026, 10, 2, 6, 0, tzinfo=mexico)  # == 12:00 UTC == NOW

    assert _sale(starts_at, None).is_active(NOW) is True
    assert _sale(starts_at + SECOND, None).is_active(NOW) is False


# --- Presentation.effective_price -------------------------------------------------------------


def test_the_effective_price_is_the_regular_one_without_a_sale() -> None:
    assert _presentation(None).effective_price(NOW) == Money(250_000)


def test_the_effective_price_is_the_sale_price_while_the_sale_is_active() -> None:
    assert _presentation(_sale(NOW, LATER)).effective_price(NOW) == Money(200_000)


@pytest.mark.parametrize(
    ("starts_at", "ends_at"),
    [(NOW + SECOND, None), (None, NOW), (None, NOW - SECOND), (LATER, None)],
)
def test_the_effective_price_is_the_regular_one_outside_the_sale_window(
    starts_at: datetime | None, ends_at: datetime | None
) -> None:
    assert _presentation(_sale(starts_at, ends_at)).effective_price(NOW) == Money(250_000)


# --- Perfume.retired_slugs --------------------------------------------------------------------


def _perfume() -> Perfume:
    return make_perfume(make_brand(), make_family(), make_concentration())


def _rename(perfume: Perfume, name: str, slug: str) -> None:
    result = perfume.update(
        brand_id=perfume.brand_id,
        concentration_id=perfume.concentration_id,
        family_id=perfume.family_id,
        name=unwrap_ok(PerfumeName.create(name)),
        slug=slug,
        gender=Gender.MEN,
        description=unwrap_ok(Description.create("")),
        notes=unwrap_ok(Notes.create([], [], [])),
        now=LATER,
    )
    assert isinstance(result, Ok), result


def test_a_new_perfume_has_no_retired_slugs() -> None:
    assert _perfume().retired_slugs == []


def test_update_records_the_slug_it_replaces() -> None:
    perfume = _perfume()

    _rename(perfume, "Eros Flame", "versace-eros-flame-edt")

    assert perfume.slug == "versace-eros-flame-edt"
    assert perfume.retired_slugs == ["versace-eros-edt"]


def test_update_that_keeps_the_slug_retires_nothing() -> None:
    perfume = _perfume()

    _rename(perfume, "Eros", perfume.slug)

    assert perfume.retired_slugs == []


def test_successive_renames_accumulate_the_retired_slugs_in_order() -> None:
    perfume = _perfume()

    _rename(perfume, "Eros Flame", "versace-eros-flame-edt")
    _rename(perfume, "Eros Black", "versace-eros-black-edt")

    assert perfume.retired_slugs == ["versace-eros-edt", "versace-eros-flame-edt"]


def test_a_refused_update_retires_nothing() -> None:
    perfume = _perfume()
    perfume.archive()

    result = perfume.update(
        brand_id=perfume.brand_id,
        concentration_id=perfume.concentration_id,
        family_id=perfume.family_id,
        name=unwrap_ok(PerfumeName.create("Other")),
        slug="other",
        gender=Gender.MEN,
        description=unwrap_ok(Description.create("")),
        notes=unwrap_ok(Notes.create([], [], [])),
        now=LATER,
    )

    assert not isinstance(result, Ok)
    assert perfume.retired_slugs == []
    assert perfume.slug == "versace-eros-edt"
