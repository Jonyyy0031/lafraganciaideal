"""Plan 002: the catalog's storefront stock rules, and their agreement with inventory's "low"."""

import pytest

from fragancia_api.modules.catalog.domain.perfume import Availability, is_buyable, stock_status
from fragancia_api.modules.inventory.domain.stock import is_low

IN_STOCK = Availability("in_stock", None, None)
MADE_TO_ORDER = Availability("made_to_order", 7, 10)


@pytest.mark.parametrize(
    ("available", "threshold", "expected"),
    [
        (0, 3, "sold_out"),
        (-1, 3, "sold_out"),
        (1, 3, "last_units"),
        (2, 3, "last_units"),
        (3, 3, "last_units"),  # equal to the threshold is already "last units"
        (4, 3, "available"),
        (10, 3, "available"),
        (10, 15, "last_units"),  # an override above the stock
        (0, 0, "sold_out"),  # sold out wins over last units when the threshold is 0
        (1, 0, "available"),
    ],
)
def test_stock_status_of_an_in_stock_presentation(
    available: int, threshold: int, expected: str
) -> None:
    assert stock_status(IN_STOCK, available, threshold) == expected


@pytest.mark.parametrize("available", [0, 1, 50])
def test_a_made_to_order_presentation_has_no_stock_status(available: int) -> None:
    assert stock_status(MADE_TO_ORDER, available, 3) is None


@pytest.mark.parametrize(
    ("availability", "available", "expected"),
    [
        (IN_STOCK, 0, False),
        (IN_STOCK, 1, True),
        (IN_STOCK, 50, True),
        (MADE_TO_ORDER, 0, True),
        (MADE_TO_ORDER, 5, True),
    ],
)
def test_is_buyable(availability: Availability, available: int, expected: bool) -> None:
    assert is_buyable(availability, available) is expected


@pytest.mark.parametrize("threshold", [0, 1, 3, 15])
@pytest.mark.parametrize("available", [1, 2, 3, 4, 14, 15, 16])
def test_the_catalog_status_and_the_inventory_low_rule_agree(
    available: int, threshold: int
) -> None:
    # `is_low(on_hand, override, global)`: with an override, the global value is irrelevant.
    status = stock_status(IN_STOCK, available, threshold)

    assert (status == "last_units") is is_low(available, threshold, 999)


@pytest.mark.parametrize("threshold", [0, 3])
def test_a_status_of_sold_out_is_always_low_for_inventory_too(threshold: int) -> None:
    assert stock_status(IN_STOCK, 0, threshold) == "sold_out"
    assert is_low(0, threshold, 999) is True


def test_equal_to_the_threshold_is_low_in_both_modules() -> None:
    assert stock_status(IN_STOCK, 3, 3) == "last_units"
    assert is_low(3, None, 3) is True
    assert stock_status(IN_STOCK, 4, 3) == "available"
    assert is_low(4, None, 3) is False
