"""The `Cart` aggregate and the pure `cart_view`: bounds, transitions, line statuses, totals."""

from datetime import timedelta
from uuid import UUID

import pytest

from fragancia_api.modules.orders.domain.cart import (
    CART_LIFETIME,
    CART_LINES_MAX,
    LINE_QUANTITY_MAX,
    Cart,
    CartLine,
    CartView,
    Offer,
    cart_view,
)
from fragancia_api.modules.orders.domain.errors import CartQuantityInvalid, CartTooManyLines
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.orders.support import NOW, made_to_order_offer, make_offer, presentation_id

LATER = NOW + timedelta(hours=1)
TOKEN = "d" * 64


def new_cart() -> Cart:
    return Cart.create(UUID(int=99), TOKEN, NOW)


def cart_with(*lines: tuple[int, int, int]) -> Cart:
    """A cart with `(n, quantity, seen_price_cents)` lines, added one minute apart."""
    cart = new_cart()
    for index, (n, quantity, seen) in enumerate(lines):
        cart.lines.append(
            CartLine(presentation_id(n), quantity, seen, NOW + timedelta(minutes=index))
        )
    return cart


def view_of(cart: Cart, *offers: Offer) -> CartView:
    return cart_view(cart, {offer.presentation_id: offer for offer in offers}, NOW)


# --- constants and creation ---------------------------------------------------------------------


def test_the_cart_limits_are_the_documented_ones() -> None:
    assert LINE_QUANTITY_MAX == 10
    assert CART_LINES_MAX == 30
    assert CART_LIFETIME == timedelta(days=30)


def test_a_new_cart_is_empty_and_stamped_with_the_creation_time() -> None:
    cart = new_cart()

    assert (cart.id, cart.token_digest, cart.lines) == (UUID(int=99), TOKEN, [])
    assert (cart.created_at, cart.updated_at) == (NOW, NOW)


def test_the_lines_are_kept_ordered_by_when_they_were_added() -> None:
    later = CartLine(presentation_id(1), 1, 100, NOW + timedelta(minutes=5))
    earlier = CartLine(presentation_id(2), 1, 100, NOW)

    cart = Cart(
        id=UUID(int=1), token_digest=TOKEN, lines=[later, earlier], created_at=NOW, updated_at=NOW
    )

    assert [line.presentation_id for line in cart.lines] == [presentation_id(2), presentation_id(1)]


# --- set_line -----------------------------------------------------------------------------------


@pytest.mark.parametrize("quantity", [1, 5, 10])
def test_set_line_accepts_a_quantity_from_1_to_10(quantity: int) -> None:
    cart = new_cart()

    result = cart.set_line(presentation_id(1), quantity, 250_000, LATER)

    assert isinstance(result, Ok)
    assert cart.lines == [CartLine(presentation_id(1), quantity, 250_000, LATER)]
    assert cart.updated_at == LATER


@pytest.mark.parametrize("quantity", [0, -1, 11, 100])
def test_set_line_rejects_a_quantity_outside_1_to_10(quantity: int) -> None:
    cart = new_cart()

    result = cart.set_line(presentation_id(1), quantity, 250_000, LATER)

    assert isinstance(result, Err)
    assert isinstance(result.error, CartQuantityInvalid)
    assert result.error.code == "ORDERS_CART_QUANTITY_INVALID"
    assert result.error.details == {"min": 1, "max": 10}
    assert cart.lines == []
    assert cart.updated_at == NOW


def test_set_line_with_an_invalid_quantity_leaves_an_existing_line_untouched() -> None:
    cart = cart_with((1, 2, 250_000))

    result = cart.set_line(presentation_id(1), 11, 300_000, LATER)

    assert isinstance(result, Err)
    assert cart.lines[0].quantity == 2
    assert cart.lines[0].seen_price_cents == 250_000
    assert cart.updated_at == NOW


def test_changing_a_quantity_acknowledges_the_current_price_and_keeps_the_added_at() -> None:
    cart = cart_with((1, 2, 250_000))
    added_at = cart.lines[0].added_at

    result = cart.set_line(presentation_id(1), 3, 280_000, LATER)

    assert isinstance(result, Ok)
    assert cart.lines == [CartLine(presentation_id(1), 3, 280_000, added_at)]
    assert cart.updated_at == LATER


def test_set_line_allows_exactly_30_lines() -> None:
    cart = cart_with(*[(n, 1, 100) for n in range(1, CART_LINES_MAX)])

    result = cart.set_line(presentation_id(CART_LINES_MAX), 1, 100, LATER)

    assert isinstance(result, Ok)
    assert len(cart.lines) == CART_LINES_MAX


def test_set_line_refuses_a_new_line_past_30() -> None:
    cart = cart_with(*[(n, 1, 100) for n in range(1, CART_LINES_MAX + 1)])

    result = cart.set_line(presentation_id(CART_LINES_MAX + 1), 1, 100, LATER)

    assert isinstance(result, Err)
    assert isinstance(result.error, CartTooManyLines)
    assert result.error.code == "ORDERS_CART_TOO_MANY_LINES"
    assert len(cart.lines) == CART_LINES_MAX
    assert cart.updated_at == NOW


def test_a_full_cart_still_accepts_a_quantity_change_on_an_existing_line() -> None:
    cart = cart_with(*[(n, 1, 100) for n in range(1, CART_LINES_MAX + 1)])

    result = cart.set_line(presentation_id(1), 4, 100, LATER)

    assert isinstance(result, Ok)
    assert cart.lines[0].quantity == 4


# --- remove_line --------------------------------------------------------------------------------


def test_remove_line_drops_the_line_and_stamps_the_change() -> None:
    cart = cart_with((1, 1, 100), (2, 1, 100))

    cart.remove_line(presentation_id(1), LATER)

    assert [line.presentation_id for line in cart.lines] == [presentation_id(2)]
    assert cart.updated_at == LATER


def test_remove_line_of_an_absent_presentation_changes_nothing() -> None:
    cart = cart_with((1, 1, 100))

    cart.remove_line(presentation_id(7), LATER)

    assert len(cart.lines) == 1
    assert cart.updated_at == NOW


# --- acknowledge_prices -------------------------------------------------------------------------


def test_acknowledge_prices_takes_the_current_price_of_every_line_present() -> None:
    cart = cart_with((1, 2, 100), (2, 3, 200))

    cart.acknowledge_prices({presentation_id(1): 150, presentation_id(2): 250}, LATER)

    assert [(line.quantity, line.seen_price_cents) for line in cart.lines] == [(2, 150), (3, 250)]
    assert cart.updated_at == LATER


def test_acknowledge_prices_leaves_a_line_missing_from_the_prices_as_it_was() -> None:
    cart = cart_with((1, 1, 100), (2, 1, 200))

    cart.acknowledge_prices({presentation_id(1): 150}, LATER)

    assert [line.seen_price_cents for line in cart.lines] == [150, 200]


# --- cart_view: the empty cart and one ok line --------------------------------------------------


def test_an_empty_cart_has_an_empty_view() -> None:
    view = view_of(new_cart())

    assert (view.lines, view.subtotal_cents, view.item_count) == ([], 0, 0)
    assert (view.has_problems, view.lead_time_max_days) == (False, None)


def test_an_ok_line_carries_the_offer_facts_the_current_price_and_the_total() -> None:
    offer = make_offer(1, price_cents=250_000)
    cart = cart_with((1, 3, 250_000))

    view = view_of(cart, offer)

    [line] = view.lines
    assert line.status == "ok"
    assert (line.quantity, line.unit_price_cents, line.line_total_cents) == (3, 250_000, 750_000)
    assert (line.label, line.perfume_slug, line.card_url, line.ml) == (
        offer.label,
        offer.perfume_slug,
        offer.card_url,
        100,
    )
    assert (line.availability, line.available_units) == ("in_stock", 5)
    assert line.price_changed_from_cents is None
    assert (view.subtotal_cents, view.item_count, view.has_problems) == (750_000, 3, False)


# --- cart_view: statuses at each boundary -------------------------------------------------------


def test_a_presentation_without_an_offer_is_unavailable_with_nothing_but_the_quantity() -> None:
    view = view_of(cart_with((1, 2, 250_000)))

    [line] = view.lines
    assert line.status == "unavailable"
    assert line.quantity == 2
    assert (line.label, line.perfume_slug, line.card_url, line.ml, line.availability) == (
        None,
        None,
        None,
        None,
        None,
    )
    assert (line.unit_price_cents, line.line_total_cents, line.price_changed_from_cents) == (
        None,
        None,
        None,
    )
    assert (view.subtotal_cents, view.item_count, view.has_problems) == (0, 0, True)


def test_an_invisible_offer_is_unavailable_but_keeps_its_facts_and_price() -> None:
    view = view_of(cart_with((1, 2, 250_000)), make_offer(1, is_visible=False))

    [line] = view.lines
    assert line.status == "unavailable"
    assert line.unit_price_cents == 250_000
    assert line.line_total_cents == 500_000
    assert (view.subtotal_cents, view.item_count, view.has_problems) == (0, 0, True)


@pytest.mark.parametrize(
    ("available", "quantity", "status"),
    [
        (0, 1, "sold_out"),
        (-2, 1, "sold_out"),
        (1, 2, "insufficient_stock"),
        (3, 4, "insufficient_stock"),
        (2, 2, "ok"),
        (5, 2, "ok"),
        (1, 1, "ok"),
    ],
)
def test_an_in_stock_line_is_classified_by_available_units_against_its_quantity(
    available: int, quantity: int, status: str
) -> None:
    view = view_of(cart_with((1, quantity, 250_000)), make_offer(1, available_units=available))

    assert view.lines[0].status == status
    assert view.has_problems is (status != "ok")


def test_a_made_to_order_line_is_ok_whatever_its_quantity() -> None:
    view = view_of(cart_with((1, 10, 250_000)), made_to_order_offer(1))

    assert view.lines[0].status == "ok"
    assert view.item_count == 10


def test_an_invisible_sold_out_offer_reports_unavailable_before_sold_out() -> None:
    view = view_of(cart_with((1, 1, 250_000)), make_offer(1, is_visible=False, available_units=0))

    assert view.lines[0].status == "unavailable"


# --- cart_view: price change --------------------------------------------------------------------


def test_a_changed_price_reports_what_the_buyer_last_saw() -> None:
    view = view_of(cart_with((1, 2, 250_000)), make_offer(1, price_cents=280_000))

    [line] = view.lines
    assert line.unit_price_cents == 280_000
    assert line.line_total_cents == 560_000
    assert line.price_changed_from_cents == 250_000
    assert view.subtotal_cents == 560_000


def test_a_price_that_dropped_is_also_reported() -> None:
    view = view_of(cart_with((1, 1, 250_000)), make_offer(1, price_cents=200_000))

    assert view.lines[0].price_changed_from_cents == 250_000


def test_an_unchanged_price_reports_no_change() -> None:
    view = view_of(cart_with((1, 1, 250_000)), make_offer(1, price_cents=250_000))

    assert view.lines[0].price_changed_from_cents is None


def test_acknowledging_the_prices_clears_the_change_in_the_view() -> None:
    cart = cart_with((1, 1, 250_000))
    offer = make_offer(1, price_cents=280_000)
    assert view_of(cart, offer).lines[0].price_changed_from_cents == 250_000

    cart.acknowledge_prices({offer.presentation_id: offer.price_cents}, LATER)

    assert view_of(cart, offer).lines[0].price_changed_from_cents is None


def test_a_line_that_is_not_ok_still_reports_its_price_change() -> None:
    view = view_of(
        cart_with((1, 1, 250_000)), make_offer(1, price_cents=280_000, available_units=0)
    )

    assert view.lines[0].status == "sold_out"
    assert view.lines[0].price_changed_from_cents == 250_000


# --- cart_view: totals only over ok lines -------------------------------------------------------


def test_the_subtotal_and_item_count_add_up_only_the_ok_lines() -> None:
    cart = cart_with((1, 2, 100_000), (2, 1, 50_000), (3, 4, 10_000), (4, 3, 20_000))

    view = view_of(
        cart,
        make_offer(1, price_cents=100_000),  # ok: 200_000
        make_offer(2, price_cents=50_000, available_units=0),  # sold out
        make_offer(3, price_cents=10_000, is_visible=False),  # unavailable
        make_offer(4, price_cents=20_000, available_units=2),  # insufficient (3 > 2)
    )

    assert [line.status for line in view.lines] == [
        "ok",
        "sold_out",
        "unavailable",
        "insufficient_stock",
    ]
    assert (view.subtotal_cents, view.item_count, view.has_problems) == (200_000, 2, True)


def test_a_line_that_comes_back_in_stock_counts_again() -> None:
    cart = cart_with((1, 2, 100_000))

    sold_out = view_of(cart, make_offer(1, price_cents=100_000, available_units=0))
    restocked = view_of(cart, make_offer(1, price_cents=100_000, available_units=9))

    assert (sold_out.subtotal_cents, sold_out.has_problems) == (0, True)
    assert (restocked.subtotal_cents, restocked.has_problems) == (200_000, False)


def test_the_lines_keep_the_order_the_cart_has() -> None:
    cart = cart_with((3, 1, 100), (1, 1, 100), (2, 1, 100))

    view = view_of(cart, make_offer(1), make_offer(2), make_offer(3))

    assert [line.presentation_id for line in view.lines] == [
        presentation_id(3),
        presentation_id(1),
        presentation_id(2),
    ]


# --- cart_view: lead time -----------------------------------------------------------------------


def test_the_lead_time_is_the_largest_over_ok_made_to_order_lines() -> None:
    cart = cart_with((1, 1, 100), (2, 1, 100), (3, 1, 100))

    view = view_of(
        cart,
        made_to_order_offer(1, max_days=3),
        made_to_order_offer(2, max_days=10),
        make_offer(3),
    )

    assert view.lead_time_max_days == 10


def test_the_lead_time_ignores_made_to_order_lines_that_are_not_ok() -> None:
    cart = cart_with((1, 1, 100), (2, 1, 100))

    view = view_of(
        cart,
        made_to_order_offer(1, max_days=3),
        made_to_order_offer(2, max_days=20, is_visible=False),
    )

    assert view.lead_time_max_days == 3


def test_the_lead_time_is_none_without_ok_made_to_order_lines() -> None:
    cart = cart_with((1, 1, 100), (2, 1, 100))

    view = view_of(cart, make_offer(1), made_to_order_offer(2, is_visible=False))

    assert view.lead_time_max_days is None
