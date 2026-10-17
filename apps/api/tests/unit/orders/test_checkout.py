"""`PlaceOrder` (plan 003 step 3) over the in-memory adapters: the order of the refusals, address
rules by method, the cart-changed check, the stock hold (only in-stock lines, reference, expiry),
frozen prices, the number, the cart deleted and the event. The real rollback is in the
integration tests; here the fakes prove the ORDER of the steps, so a refusal writes nothing."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

import pytest

from fragancia_api.modules.orders.application.commands.checkout import PlaceOrder
from fragancia_api.modules.orders.contracts import (
    CheckoutAddress,
    CheckoutContact,
    CheckoutInvoice,
    CheckoutRequest,
    PlacedOrder,
)
from fragancia_api.modules.orders.domain.cart import Cart, CartLine, Offer
from fragancia_api.modules.orders.domain.delivery import DeliveryMethod, DeliverySettings
from fragancia_api.modules.orders.domain.order import OrderPlaced
from fragancia_api.modules.orders.infrastructure.in_memory import (
    InMemoryCarts,
    InMemoryCatalogOffers,
    InMemoryDeliverySettings,
    InMemoryOrders,
    InMemoryStockHolds,
)
from fragancia_api.shared.infrastructure.in_memory import (
    FixedClock,
    InMemoryTransactionRunner,
    RecordingEventPublisher,
)
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result
from tests.unit.orders.support import (
    NOW,
    FakeOrderLinks,
    build_settings,
    made_to_order_offer,
    make_offer,
    presentation_id,
)
from tests.unit.orders.test_cart_application import SequenceTokens

CART_TOKEN = "cart-token"
IN_STOCK_PRICE = 250_000
MADE_TO_ORDER_PRICE = 100_000
HOME_COST = 18_000


class Trace:
    """What happened and in which order; `inside` is true while the transaction runs."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.inside = False
        self.reads_inside: list[str] = []

    def read(self, name: str) -> None:
        self.events.append(name)
        if self.inside:
            self.reads_inside.append(name)


class TracedRunner(InMemoryTransactionRunner):
    def __init__(self, trace: Trace) -> None:
        self._trace = trace

    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
        self._trace.inside = True
        self._trace.events.append("begin")
        try:
            return await work()
        finally:
            self._trace.inside = False


class TracedCarts(InMemoryCarts):
    """Records the cart reads and the lock; `before_lock` runs between the plain read and the
    lock, standing for another request touching the cart in between."""

    def __init__(self, trace: Trace, *carts: Cart) -> None:
        super().__init__(*carts)
        self._trace = trace
        self.before_lock: Callable[[InMemoryCarts], None] | None = None

    async def by_token(self, token_digest: str) -> Cart | None:
        self._trace.read("cart_read")
        return await super().by_token(token_digest)

    async def get_by_token_for_update(self, token_digest: str) -> Cart | None:
        if self.before_lock is not None:
            self.before_lock(self)
        self._trace.events.append("cart_lock")
        return await super().get_by_token_for_update(token_digest)

    async def delete(self, cart: Cart) -> None:
        self._trace.events.append("cart_delete")
        await super().delete(cart)


class TracedOffers(InMemoryCatalogOffers):
    def __init__(self, trace: Trace, *offers: Offer) -> None:
        super().__init__(*offers)
        self._trace = trace

    async def offers(self, ids, now):  # type: ignore[no-untyped-def]
        self._trace.read("offers")
        return await super().offers(ids, now)


class TracedSettings(InMemoryDeliverySettings):
    def __init__(self, trace: Trace, settings: DeliverySettings) -> None:
        super().__init__(settings)
        self._trace = trace

    async def current(self) -> DeliverySettings:
        self._trace.read("settings")
        return await super().current()


class TracedOrders(InMemoryOrders):
    def __init__(self, trace: Trace) -> None:
        super().__init__()
        self._trace = trace

    async def next_number(self) -> int:
        self._trace.events.append("number")
        return await super().next_number()

    async def add(self, order) -> None:  # type: ignore[no-untyped-def]
        self._trace.events.append("order_add")
        await super().add(order)


class TracedHolds(InMemoryStockHolds):
    def __init__(self, trace: Trace, short_lines: list[dict[str, object]] | None = None) -> None:
        super().__init__(short_lines)
        self._trace = trace

    async def hold(self, reference, quantities, expires_at):  # type: ignore[no-untyped-def]
        self._trace.events.append("hold")
        return await super().hold(reference, quantities, expires_at)


@dataclass
class World:
    settings: DeliverySettings = field(default_factory=build_settings)

    def __post_init__(self) -> None:
        self.trace = Trace()
        self.carts = TracedCarts(self.trace)
        self.offers = TracedOffers(self.trace)
        self.delivery = TracedSettings(self.trace, self.settings)
        self.orders = TracedOrders(self.trace)
        self.stocks = TracedHolds(self.trace)
        self.events = RecordingEventPublisher()
        self.clock = FixedClock()
        self.cart_tokens = SequenceTokens()
        self.links = FakeOrderLinks()

    def place_order(self) -> PlaceOrder:
        return PlaceOrder(
            carts=self.carts,
            cart_reader=self.carts,
            cart_tokens=self.cart_tokens,
            links=self.links,
            order_reader=self.orders,
            offers=self.offers,
            delivery_settings=self.delivery,
            orders=self.orders,
            stocks=self.stocks,
            transactions=TracedRunner(self.trace),
            events=self.events,
            clock=self.clock,
        )

    def cart(self, token: str, *lines: tuple[int, int], offers: list[Offer] | None = None) -> Cart:
        """A stored cart behind `token` with `(n, quantity)` lines; `offers` go to the catalog."""
        cart = Cart(
            id=UUID(int=500 + len(self.carts.by_id)),
            token_digest=self.cart_tokens.digest(token),
            lines=[
                CartLine(presentation_id(n), quantity, 0, NOW + timedelta(seconds=i))
                for i, (n, quantity) in enumerate(lines)
            ],
            created_at=NOW,
            updated_at=NOW,
        )
        self.carts.by_id[cart.id] = cart
        for offer in offers or []:
            self.offers.by_id[offer.presentation_id] = offer
        return cart

    async def place(
        self, request: CheckoutRequest, token: str | None = CART_TOKEN
    ) -> Result[PlacedOrder, DomainError]:
        return await self.place_order().execute(token, request)


def in_stock(n: int = 1, **overrides: object) -> Offer:
    values: dict[str, object] = {"price_cents": IN_STOCK_PRICE, "regular_price_cents": 300_000}
    values.update(overrides)
    return make_offer(n, **values)


def made_to_order(n: int = 2, days: int = 10) -> Offer:
    return made_to_order_offer(
        n, max_days=days, price_cents=MADE_TO_ORDER_PRICE, regular_price_cents=MADE_TO_ORDER_PRICE
    )


def contact(**overrides: str) -> CheckoutContact:
    values = {"name": "Ana López", "email": "Ana@Example.com", "phone": "+52 (998) 123-4567"}
    values.update(overrides)
    return CheckoutContact(**values)


def home_address(**overrides: str | None) -> CheckoutAddress:
    values: dict[str, str | None] = {
        "street": "Av. Reforma",
        "exterior_number": "100",
        "interior_number": None,
        "neighborhood": "Juárez",
        "postal_code": "01000",
        "municipality": "Cuauhtémoc",
        "state": "CMX",
        "references": None,
    }
    values.update(overrides)
    return CheckoutAddress(**values)


def request(
    method: str = "home",
    total: int = 0,
    *,
    address: CheckoutAddress | None | str = "default",
    invoice: CheckoutInvoice | None = None,
    who: CheckoutContact | None = None,
) -> CheckoutRequest:
    if address == "default":
        address = None if method == "pickup" else home_address()
    return CheckoutRequest(
        contact=who or contact(),
        delivery_method=method,
        address=address,
        invoice=invoice,
        expected_total_cents=total,
    )


def ok(result: Result[PlacedOrder, DomainError]) -> PlacedOrder:
    assert isinstance(result, Ok), result
    return result.value


def refused(result: Result[PlacedOrder, DomainError]) -> DomainError:
    assert isinstance(result, Err), result
    return result.error


def full_cart(world: World) -> int:
    """In stock x2 (n=1) and made to order x1 (n=2): subtotal 600_000. Returns it."""
    world.cart(CART_TOKEN, (1, 2), (2, 1), offers=[in_stock(1), made_to_order(2)])
    return 2 * IN_STOCK_PRICE + MADE_TO_ORDER_PRICE


def assert_nothing_written(world: World) -> None:
    assert world.orders.orders == []
    assert world.orders.last_number == 1000
    assert world.stocks.calls == []
    assert world.events.published == []
    assert len(world.carts.by_id) == 1


# --- the happy path -----------------------------------------------------------------------------


async def test_a_home_order_returns_the_number_the_token_and_the_totals() -> None:
    world = World(build_settings(home_free_from_cents=None))
    subtotal = full_cart(world)

    placed = ok(await world.place(request("home", subtotal + HOME_COST)))

    assert placed.number == "LFI-1001"
    assert placed.access_token == f"link:{world.orders.orders[0].id}"
    assert placed.status == "pending_payment"
    assert (placed.subtotal_cents, placed.delivery_cents, placed.total_cents) == (
        600_000,
        HOME_COST,
        618_000,
    )
    assert placed.payment_due_at == NOW + timedelta(minutes=30)
    assert placed.lead_time_max_days == 10


async def test_the_stored_order_has_the_normalized_customer_and_only_the_digest_of_the_token() -> (
    None
):
    world = World(build_settings(home_free_from_cents=None))
    full_cart(world)

    await world.place(request("home", 618_000))

    [order] = world.orders.orders
    assert order.number == 1001
    assert (order.name.value, order.email.value, order.phone.value) == (
        "Ana López",
        "ana@example.com",
        "9981234567",
    )
    assert order.access_token_digest == f"digest:link:{order.id}"
    assert order.cart_token_digest == f"digest:{CART_TOKEN}"
    assert order.status.value == "pending_payment"
    assert order.invoice is None
    assert (order.created_at, order.payment_due_at) == (NOW, NOW + timedelta(minutes=30))
    assert order.delivery.method is DeliveryMethod.HOME
    assert order.delivery.cost_cents == HOME_COST
    assert order.delivery.address is not None
    assert order.delivery.address.state == "CMX"
    assert (order.delivery.pickup_address, order.delivery.pickup_hours) == (None, None)


async def test_the_lines_freeze_the_offer_at_checkout() -> None:
    world = World(build_settings(home_free_from_cents=None))
    full_cart(world)

    await world.place(request("home", 618_000))

    [order] = world.orders.orders
    first, second = order.lines
    assert (first.presentation_id, first.perfume_id) == (presentation_id(1), UUID(int=1001))
    assert (first.label, first.ml, first.availability) == (
        "Brand Perfume 1 EDT 100 ml",
        100,
        "in_stock",
    )
    assert (first.quantity, first.unit_price_cents, first.regular_price_cents) == (
        2,
        250_000,
        300_000,
    )
    assert first.line_total_cents == 500_000
    assert (first.lead_time_min_days, first.lead_time_max_days) == (None, None)
    assert (second.presentation_id, second.availability, second.quantity) == (
        presentation_id(2),
        "made_to_order",
        1,
    )
    assert (second.lead_time_min_days, second.lead_time_max_days) == (8, 10)
    assert second.line_total_cents == 100_000
    assert (order.subtotal_cents, order.total_cents, order.lead_time_max_days) == (
        600_000,
        618_000,
        10,
    )


async def test_one_reservation_holds_only_the_in_stock_lines_for_thirty_minutes() -> None:
    world = World(build_settings(home_free_from_cents=None))
    full_cart(world)

    await world.place(request("home", 618_000))

    [order] = world.orders.orders
    [(reference, quantities, expires_at)] = world.stocks.calls
    assert reference == f"order:{order.id}"
    assert quantities == {presentation_id(1): 2}
    assert expires_at == NOW + timedelta(minutes=30)
    assert order.reservation_id is not None


async def test_a_cart_of_only_made_to_order_lines_reserves_nothing() -> None:
    world = World(build_settings(home_free_from_cents=None))
    world.cart(CART_TOKEN, (2, 1), offers=[made_to_order(2, days=7)])

    placed = ok(await world.place(request("home", MADE_TO_ORDER_PRICE + HOME_COST)))

    assert world.stocks.calls == []
    [order] = world.orders.orders
    assert order.reservation_id is None
    assert placed.lead_time_max_days == 7


async def test_the_cart_is_deleted_and_one_order_placed_event_is_published() -> None:
    world = World(build_settings(home_free_from_cents=None))
    full_cart(world)

    await world.place(request("home", 618_000))

    assert world.carts.by_id == {}
    [event] = world.events.published
    [order] = world.orders.orders
    assert isinstance(event, OrderPlaced)
    assert (event.order_id, event.number) == (order.id, 1001)


async def test_the_second_order_takes_the_next_number() -> None:
    world = World(build_settings(home_free_from_cents=None))
    world.cart("first", (1, 1), offers=[in_stock(1)])
    world.cart("second", (1, 1))

    first = ok(await world.place(request("home", IN_STOCK_PRICE + HOME_COST), "first"))
    second = ok(await world.place(request("home", IN_STOCK_PRICE + HOME_COST), "second"))

    assert (first.number, second.number) == ("LFI-1001", "LFI-1002")
    first_order, second_order = world.orders.orders
    assert (first.access_token, second.access_token) == (
        f"link:{first_order.id}",
        f"link:{second_order.id}",
    )
    assert [o.number for o in world.orders.orders] == [1001, 1002]


async def test_free_home_delivery_above_the_threshold_costs_nothing() -> None:
    world = World()  # home is free from 250_000
    full_cart(world)

    placed = ok(await world.place(request("home", 600_000)))

    assert (placed.delivery_cents, placed.total_cents) == (0, 600_000)


async def test_a_local_order_pays_the_local_price_for_a_listed_postal_code() -> None:
    world = World()
    full_cart(world)

    placed = ok(
        await world.place(request("local", 609_000, address=home_address(postal_code="77500")))
    )

    assert (placed.delivery_cents, placed.total_cents) == (9_000, 609_000)


async def test_a_pickup_order_stores_no_address_but_the_pickup_point_and_the_invoice() -> None:
    world = World()
    full_cart(world)
    invoice = CheckoutInvoice(
        rfc="xaxx010101000",
        business_name="Ana  López",
        tax_regime="601",
        cfdi_use="g03",
        postal_code="01000",
    )

    placed = ok(await world.place(request("pickup", 600_000, invoice=invoice)))

    assert placed.delivery_cents == 0
    [order] = world.orders.orders
    assert order.delivery.address is None
    assert (order.delivery.pickup_address, order.delivery.pickup_hours) == (
        "Calle 1 #2, Cancun",
        "Lun-Vie 10-18",
    )
    assert order.invoice is not None
    assert (order.invoice.rfc, order.invoice.business_name) == ("XAXX010101000", "Ana López")
    assert (order.invoice.tax_regime, order.invoice.cfdi_use) == ("601", "G03")
    assert order.invoice.postal_code.value == "01000"


async def test_the_steps_run_in_the_documented_order_with_every_read_outside_the_transaction() -> (
    None
):
    world = World(build_settings(home_free_from_cents=None))
    full_cart(world)

    await world.place(request("home", 618_000))

    assert world.trace.events == [
        "cart_read",
        "offers",
        "settings",
        "begin",
        "cart_lock",
        "hold",
        "number",
        "order_add",
        "cart_delete",
    ]
    assert world.trace.reads_inside == []


# --- validation comes first ---------------------------------------------------------------------


async def test_an_invalid_phone_is_refused_before_the_cart_is_even_read() -> None:
    world = World()

    error = refused(await world.place(request("home", 0, who=contact(phone="12345")), None))

    assert error.code == "ORDERS_CUSTOMER_DATA_INVALID"
    assert error.details == {"field": "contact.phone"}
    assert world.trace.events == []


@pytest.mark.parametrize(
    ("who", "expected"),
    [
        (contact(name="A"), "contact.name"),
        (contact(email="nope"), "contact.email"),
        (contact(phone="123"), "contact.phone"),
    ],
)
async def test_each_contact_field_is_validated_with_its_path(
    who: CheckoutContact, expected: str
) -> None:
    world = World()
    full_cart(world)

    error = refused(await world.place(request("home", 0, who=who)))

    assert error.details == {"field": expected}
    assert_nothing_written(world)


async def test_the_contact_is_checked_before_the_address_and_the_address_before_the_invoice() -> (
    None
):
    world = World()
    bad_invoice = CheckoutInvoice(
        rfc="ABC123", business_name="x", tax_regime="601", cfdi_use="G03", postal_code="01000"
    )

    both = refused(
        await world.place(
            request(
                "home",
                0,
                who=contact(name="A"),
                address=home_address(state="XXX"),
                invoice=bad_invoice,
            )
        )
    )
    address_first = refused(
        await world.place(
            request("home", 0, address=home_address(state="XXX"), invoice=bad_invoice)
        )
    )
    invoice_last = refused(await world.place(request("home", 0, invoice=bad_invoice)))

    assert both.details == {"field": "contact.name"}
    assert address_first.details == {"field": "address.state"}
    assert invoice_last.details == {"field": "invoice.rfc"}


@pytest.mark.parametrize("method", ["home", "local"])
async def test_home_and_local_require_an_address(method: str) -> None:
    world = World()
    full_cart(world)

    error = refused(await world.place(request(method, 0, address=None)))

    assert error.code == "ORDERS_CUSTOMER_DATA_INVALID"
    assert error.details == {"field": "address"}
    assert_nothing_written(world)


async def test_pickup_refuses_an_address() -> None:
    world = World()
    full_cart(world)

    error = refused(await world.place(request("pickup", 0, address=home_address())))

    assert error.code == "ORDERS_CUSTOMER_DATA_INVALID"
    assert error.details == {"field": "address"}
    assert_nothing_written(world)


@pytest.mark.parametrize(
    ("invoice", "expected"),
    [
        (("ABC123", "601", "G03"), "invoice.rfc"),
        (("XAXX010101000", "999", "G03"), "invoice.tax_regime"),
        (("XAXX010101000", "601", "CP01"), "invoice.cfdi_use"),
    ],
)
async def test_an_invalid_invoice_is_refused_with_its_field(
    invoice: tuple[str, str, str], expected: str
) -> None:
    world = World()
    full_cart(world)
    rfc, regime, use = invoice

    error = refused(
        await world.place(
            request(
                "pickup",
                0,
                invoice=CheckoutInvoice(
                    rfc=rfc,
                    business_name="Ana",
                    tax_regime=regime,
                    cfdi_use=use,
                    postal_code="01000",
                ),
            )
        )
    )

    assert error.details == {"field": expected}
    assert_nothing_written(world)


# --- the cart -----------------------------------------------------------------------------------


async def test_no_cookie_is_an_empty_cart() -> None:
    world = World()
    full_cart(world)

    error = refused(await world.place(request("home", 0), None))

    assert error.code == "ORDERS_CHECKOUT_CART_EMPTY"
    assert world.trace.events == []


async def test_an_unknown_token_is_an_empty_cart() -> None:
    world = World()
    full_cart(world)

    assert refused(await world.place(request("home", 0), "stranger")).code == (
        "ORDERS_CHECKOUT_CART_EMPTY"
    )
    assert_nothing_written(world)


async def test_a_cart_without_lines_is_empty() -> None:
    world = World()
    world.cart(CART_TOKEN)

    assert refused(await world.place(request("home", 0))).code == "ORDERS_CHECKOUT_CART_EMPTY"
    assert world.trace.events == ["cart_read"]


@pytest.mark.parametrize(
    ("offer", "status"),
    [
        (in_stock(1, available_units=0), "sold_out"),
        (in_stock(1, available_units=1), "insufficient_stock"),
        (in_stock(1, is_visible=False), "unavailable"),
    ],
)
async def test_a_cart_with_problems_is_refused_listing_each_problem_line(
    offer: Offer, status: str
) -> None:
    world = World()
    world.cart(CART_TOKEN, (1, 2), (2, 1), offers=[offer, made_to_order(2)])

    error = refused(await world.place(request("home", 0)))

    assert error.code == "ORDERS_CHECKOUT_CART_HAS_PROBLEMS"
    assert error.details == {
        "lines": [{"presentation_id": str(presentation_id(1)), "status": status}]
    }
    assert_nothing_written(world)


async def test_a_line_whose_presentation_no_longer_exists_is_listed_as_unavailable() -> None:
    world = World()
    world.cart(CART_TOKEN, (1, 1), (9, 1), offers=[in_stock(1)])

    error = refused(await world.place(request("home", 0)))

    assert error.details == {
        "lines": [{"presentation_id": str(presentation_id(9)), "status": "unavailable"}]
    }


async def test_the_cart_problems_win_over_an_unavailable_delivery_and_a_wrong_total() -> None:
    world = World()
    world.cart(CART_TOKEN, (1, 1), offers=[in_stock(1, available_units=0)])

    error = refused(
        await world.place(request("local", 1, address=home_address(postal_code="99999")))
    )

    assert error.code == "ORDERS_CHECKOUT_CART_HAS_PROBLEMS"


# --- delivery and the total ---------------------------------------------------------------------


async def test_local_delivery_off_the_postal_code_list_is_unavailable() -> None:
    world = World()
    full_cart(world)

    error = refused(
        await world.place(request("local", 0, address=home_address(postal_code="01000")))
    )

    assert error.code == "ORDERS_CHECKOUT_DELIVERY_UNAVAILABLE"
    assert error.details == {"method": "local"}
    assert_nothing_written(world)


@pytest.mark.parametrize(
    ("method", "overrides"),
    [
        ("home", {"home_enabled": False}),
        ("local", {"local_enabled": False}),
        ("pickup", {"pickup_enabled": False}),
    ],
)
async def test_a_switched_off_method_is_unavailable(
    method: str, overrides: dict[str, object]
) -> None:
    world = World(build_settings(**overrides))
    full_cart(world)

    error = refused(
        await world.place(
            request(
                method, 0, address=None if method == "pickup" else home_address(postal_code="77500")
            )
        )
    )

    assert error.code == "ORDERS_CHECKOUT_DELIVERY_UNAVAILABLE"
    assert error.details == {"method": method}


async def test_an_unavailable_delivery_wins_over_a_wrong_total() -> None:
    world = World()
    full_cart(world)

    error = refused(
        await world.place(request("local", 1, address=home_address(postal_code="01000")))
    )

    assert error.code == "ORDERS_CHECKOUT_DELIVERY_UNAVAILABLE"


async def test_a_wrong_expected_total_is_refused_with_the_real_figures() -> None:
    world = World(build_settings(home_free_from_cents=None))
    full_cart(world)

    error = refused(await world.place(request("home", 600_000)))

    assert error.code == "ORDERS_CHECKOUT_TOTAL_CHANGED"
    assert error.details == {
        "subtotal_cents": 600_000,
        "delivery_cents": HOME_COST,
        "total_cents": 618_000,
    }
    assert_nothing_written(world)


async def test_an_expected_total_one_cent_off_is_refused() -> None:
    world = World(build_settings(home_free_from_cents=None))
    full_cart(world)

    assert refused(await world.place(request("home", 618_001))).code == (
        "ORDERS_CHECKOUT_TOTAL_CHANGED"
    )


async def test_a_subtotal_over_the_quote_limit_passes_the_delivery_error_through() -> None:
    world = World()
    world.cart(
        CART_TOKEN, (1, 10), offers=[in_stock(1, price_cents=10_000_001, available_units=None)]
    )

    error = refused(await world.place(request("home", 0)))

    assert error.code == "ORDERS_DELIVERY_OPTIONS_INVALID"
    assert_nothing_written(world)


# --- the cart changed between the read and the lock ---------------------------------------------


async def test_a_cart_deleted_before_the_lock_is_a_changed_cart() -> None:
    world = World(build_settings(home_free_from_cents=None))
    full_cart(world)
    world.carts.before_lock = lambda carts: carts.by_id.clear()

    error = refused(await world.place(request("home", 618_000)))

    assert error.code == "ORDERS_CHECKOUT_CART_CHANGED"
    assert world.orders.orders == []
    assert world.stocks.calls == []
    assert world.events.published == []


async def test_a_quantity_changed_before_the_lock_is_a_changed_cart() -> None:
    world = World(build_settings(home_free_from_cents=None))
    cart = full_cart(world)

    def change(carts: InMemoryCarts) -> None:
        stored = carts.by_id[UUID(int=500)]
        stored.lines[0] = CartLine(presentation_id(1), 3, 0, NOW)

    world.carts.before_lock = change

    error = refused(await world.place(request("home", cart + HOME_COST)))

    assert error.code == "ORDERS_CHECKOUT_CART_CHANGED"
    assert world.stocks.calls == []
    assert world.orders.last_number == 1000
    assert len(world.carts.by_id) == 1


async def test_a_line_added_before_the_lock_is_a_changed_cart() -> None:
    world = World(build_settings(home_free_from_cents=None))
    cart = full_cart(world)

    def add(carts: InMemoryCarts) -> None:
        carts.by_id[UUID(int=500)].lines.append(CartLine(presentation_id(5), 1, 0, NOW))

    world.carts.before_lock = add

    error = refused(await world.place(request("home", cart + HOME_COST)))

    assert error.code == "ORDERS_CHECKOUT_CART_CHANGED"


async def test_a_price_acknowledgement_before_the_lock_is_not_a_change() -> None:
    # Only the (presentation, quantity) pairs are compared; the seen price is not part of them.
    world = World(build_settings(home_free_from_cents=None))
    cart = full_cart(world)

    def acknowledge(carts: InMemoryCarts) -> None:
        stored = carts.by_id[UUID(int=500)]
        stored.lines[0] = CartLine(presentation_id(1), 2, 777, NOW)

    world.carts.before_lock = acknowledge

    assert isinstance(await world.place(request("home", cart + HOME_COST)), Ok)


# --- stock --------------------------------------------------------------------------------------


async def test_a_stock_refusal_returns_inventorys_lines_and_leaves_everything_untouched() -> None:
    world = World(build_settings(home_free_from_cents=None))
    short = [{"presentation_id": str(presentation_id(1)), "requested": 2, "available": 1}]
    world.stocks = TracedHolds(world.trace, short)
    full_cart(world)

    error = refused(await world.place(request("home", 618_000)))

    assert error.code == "ORDERS_CHECKOUT_STOCK_UNAVAILABLE"
    assert error.details == {"lines": short}
    assert world.orders.orders == []
    assert world.orders.last_number == 1000
    assert world.events.published == []
    assert len(world.carts.by_id) == 1
    assert world.trace.events[-2:] == ["cart_lock", "hold"]  # nothing after the refused hold


async def test_a_refused_checkout_does_not_burn_a_number() -> None:
    world = World(build_settings(home_free_from_cents=None))
    full_cart(world)
    world.stocks = TracedHolds(
        world.trace, [{"presentation_id": "x", "requested": 1, "available": 0}]
    )
    refused(await world.place(request("home", 618_000)))
    world.stocks = TracedHolds(world.trace)

    placed = ok(await world.place(request("home", 618_000)))

    assert placed.number == "LFI-1001"


# --- a retried checkout gets its order back (decision 31) ---------------------------------------


async def test_a_retry_within_30_minutes_answers_the_same_order_without_a_second_one() -> None:
    world = World(build_settings(home_free_from_cents=None))
    full_cart(world)
    first = ok(await world.place(request("home", 618_000)))
    world.clock.current = NOW + timedelta(minutes=29, seconds=59)

    again = ok(await world.place(request("home", 1)))  # the body is ignored

    assert again == first
    assert len(world.orders.orders) == 1
    assert len(world.stocks.calls) == 1
    assert len(world.events.published) == 1
    assert world.orders.last_number == 1001


async def test_no_replay_after_30_minutes() -> None:
    world = World(build_settings(home_free_from_cents=None))
    full_cart(world)
    ok(await world.place(request("home", 618_000)))
    world.clock.current = NOW + timedelta(minutes=30, seconds=1)

    error = refused(await world.place(request("home", 618_000)))

    assert error.code == "ORDERS_CHECKOUT_CART_EMPTY"


async def test_no_replay_for_another_cart() -> None:
    world = World(build_settings(home_free_from_cents=None))
    full_cart(world)
    ok(await world.place(request("home", 618_000)))

    error = refused(await world.place(request("home", 618_000), "another-cart"))

    assert error.code == "ORDERS_CHECKOUT_CART_EMPTY"


async def test_the_link_token_is_the_derived_one_and_only_its_digest_is_stored() -> None:
    world = World(build_settings(home_free_from_cents=None))
    full_cart(world)

    placed = ok(await world.place(request("home", 618_000)))

    [order] = world.orders.orders
    assert placed.access_token == world.links.token(order.id)
    assert order.access_token_digest == world.links.digest(placed.access_token)
    assert order.cart_token_digest == world.cart_tokens.digest(CART_TOKEN)
    assert placed.status == order.status.value
