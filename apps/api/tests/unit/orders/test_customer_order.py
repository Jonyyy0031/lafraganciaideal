"""Plan 005 step 2, application: the customer's view built from an order with history
(timeline from `created_at`, the refund row left out, `cancelled_by`, `can_cancel`, the exact key
set with no internal field), the lookup (match, normalization, wrong email and unknown number
answering the same error, unparsable input, the IP and number limits, the counts committed before
the refusal) and the customer cancel (unpaid, paid refused, unknown token)."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from fragancia_api.modules.orders.application.commands.customer_order import (
    CancelOrderByCustomer,
    LookUpOrder,
)
from fragancia_api.modules.orders.application.commands.lifecycle import (
    AdvanceOrder,
    CancelOverdueOrders,
    CancelPaidOrder,
    CancelUnpaidOrder,
    MarkOrderPaid,
    MarkOrderRefunded,
)
from fragancia_api.modules.orders.application.ports import PayableOrder
from fragancia_api.modules.orders.application.queries.customer_order import GetCustomerOrder
from fragancia_api.modules.orders.contracts import CustomerOrderView
from fragancia_api.modules.orders.domain.errors import (
    OrderLookupNotFound,
    OrderLookupRateLimited,
    OrderNotFound,
    OrderTransitionInvalid,
)
from fragancia_api.modules.orders.domain.order import (
    CUSTOMER_ACTOR,
    CUSTOMER_CANCEL_REASON,
    LOOKUP_IP_MAX,
    LOOKUP_NUMBER_MAX,
    LOOKUP_WINDOW,
    Order,
    OrderStatus,
)
from fragancia_api.modules.orders.infrastructure.in_memory import (
    InMemoryCustomerOrders,
    InMemoryLookupThrottle,
)
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result
from tests.unit.orders.support import NOW, FakeOrderLinks
from tests.unit.orders.test_order_lifecycle import World

S = OrderStatus
LINKS = FakeOrderLinks()

# The keys the customer may see, written out independently of the contract.
VIEW_KEYS = {
    "number",
    "status",
    "created_at",
    "payment_due_at",
    "customer_name",
    "customer_phone",
    "lines",
    "subtotal_cents",
    "delivery_cents",
    "total_cents",
    "lead_time_max_days",
    "delivery_method",
    "address",
    "pickup_address",
    "pickup_hours",
    "invoice",
    "shipping_carrier",
    "tracking_number",
    "refund_status",
    "cancelled_by",
    "timeline",
    "can_cancel",
}
LINE_KEYS = {
    "label",
    "ml",
    "quantity",
    "unit_price_cents",
    "line_total_cents",
    "availability",
    "lead_time_min_days",
    "lead_time_max_days",
}
ADDRESS_KEYS = {
    "street",
    "exterior_number",
    "interior_number",
    "neighborhood",
    "postal_code",
    "municipality",
    "state",
    "references",
}
STEP_KEYS = {"status", "at"}
FORBIDDEN = {
    "notes",
    "history",
    "actor",
    "cancel_reason",
    "reservation_id",
    "access_token_digest",
    "cart_token_digest",
    "customer_email",
    "updated_at",
    "id",
}


def refused(result: Result[Any, DomainError]) -> DomainError:
    assert isinstance(result, Err), result
    return result.error


def token_of(n: int = 1) -> str:
    return f"tok-{n}"


@dataclass
class Shop:
    world: World = field(default_factory=World)

    def __post_init__(self) -> None:
        self.customer = InMemoryCustomerOrders(self.world.orders)
        self.throttle = InMemoryLookupThrottle()

    async def store(
        self, status: OrderStatus = S.PENDING_PAYMENT, *, n: int = 1, **kw: Any
    ) -> Order:
        order = await self.world.store(status, n=n, **kw)
        self.world.current(n).access_token_digest = LINKS.digest(token_of(n))
        return order

    def view(self) -> GetCustomerOrder:
        return GetCustomerOrder(queries=self.customer, links=LINKS)

    def lookup(self) -> LookUpOrder:
        return LookUpOrder(
            queries=self.customer,
            links=LINKS,
            throttle=self.throttle,
            transactions=self.world.transactions,
            clock=self.world.clock,
        )

    def cancel(self) -> CancelOrderByCustomer:
        return CancelOrderByCustomer(
            queries=self.customer, links=LINKS, cancel=self.world.wire(CancelUnpaidOrder)
        )

    async def get(self, n: int = 1) -> CustomerOrderView:
        result = await self.view().execute(token_of(n))
        assert isinstance(result, Ok), result
        return result.value


# --- GetCustomerOrder --------------------------------------------------------------------------


async def test_an_unknown_token_is_order_not_found() -> None:
    shop = Shop()
    await shop.store()

    error = refused(await shop.view().execute("nope"))

    assert isinstance(error, OrderNotFound)
    assert error.code == "ORDERS_ORDER_NOT_FOUND"


async def test_the_view_is_found_by_the_digest_of_the_token_not_by_the_stored_digest() -> None:
    shop = Shop()
    await shop.store()

    # The stored value is the digest; presenting the digest itself must not open the order.
    error = refused(await shop.view().execute(LINKS.digest(token_of())))

    assert isinstance(error, OrderNotFound)


async def test_a_new_order_shows_its_data_a_one_step_timeline_and_can_be_cancelled() -> None:
    shop = Shop()
    await shop.store()

    view = await shop.get()

    assert (view.number, view.status) == ("LFI-1001", "pending_payment")
    assert view.created_at == NOW
    assert view.payment_due_at == NOW + timedelta(minutes=30)
    assert (view.customer_name, view.customer_phone) == ("Ana López", "9981234567")
    assert [(s.status, s.at) for s in view.timeline] == [("pending_payment", NOW)]
    assert view.can_cancel is True
    assert (view.cancelled_by, view.refund_status) == (None, None)
    assert (view.shipping_carrier, view.tracking_number) == (None, None)
    [line] = view.lines
    assert (line.label, line.ml, line.quantity) == ("Perfume 1", 100, 1)
    assert (line.unit_price_cents, line.line_total_cents) == (100_000, 100_000)
    assert view.subtotal_cents == 100_000
    assert view.delivery_method == "home"
    assert view.address is not None
    assert view.address.postal_code == "77500"
    assert view.invoice is None


async def test_the_view_has_exactly_the_customer_keys_and_nothing_internal() -> None:
    shop = Shop()
    await shop.store()
    world = shop.world
    await world.wire(MarkOrderPaid).execute(1001, "Transferencia directa", "owner-1")
    await world.orders.add_note(1001, "owner-1", "nota interna", NOW)

    dumped = (await shop.get()).model_dump(mode="json")

    assert set(dumped) == VIEW_KEYS
    assert set(dumped["lines"][0]) == LINE_KEYS
    assert set(dumped["address"]) == ADDRESS_KEYS
    assert all(set(step) == STEP_KEYS for step in dumped["timeline"])
    assert FORBIDDEN.isdisjoint(dumped)


async def test_no_internal_value_appears_anywhere_in_the_serialized_view() -> None:
    shop = Shop()
    await shop.store()
    world = shop.world
    await world.orders.add_note(1001, "owner-1", "nota interna secreta", NOW)
    await world.wire(CancelUnpaidOrder).execute(1001, "motivo privado de la tienda", "owner-1")
    stored = world.current()
    assert stored.access_token_digest is not None
    assert stored.cart_token_digest is not None

    text = (await shop.get()).model_dump_json()

    for secret in (
        "nota interna secreta",
        "motivo privado de la tienda",
        "owner-1",
        str(stored.reservation_id),
        stored.access_token_digest,
        stored.cart_token_digest,
        "ana@example.com",
    ):
        assert secret not in text, secret


async def test_the_timeline_follows_the_moves_with_their_times_and_the_shipping_data() -> None:
    shop = Shop()
    await shop.store()
    world = shop.world
    await world.wire(MarkOrderPaid).execute(1001, "Transferencia directa", "owner-1")
    world.clock.current = NOW + timedelta(hours=2)
    await world.wire(AdvanceOrder).execute(1001, "shipped", "Estafeta", "123456", "owner-1")

    view = await shop.get()

    assert [(s.status, s.at) for s in view.timeline] == [
        ("pending_payment", NOW),
        ("paid", NOW + timedelta(hours=1)),
        ("shipped", NOW + timedelta(hours=2)),
    ]
    assert (view.status, view.can_cancel) == ("shipped", False)
    assert (view.shipping_carrier, view.tracking_number) == ("Estafeta", "123456")
    assert view.cancelled_by is None


async def test_a_paid_order_cannot_be_cancelled_from_the_page() -> None:
    shop = Shop()
    await shop.store()
    await shop.world.wire(MarkOrderPaid).execute(1001, "x", "owner-1")

    assert (await shop.get()).can_cancel is False


async def test_the_refund_row_is_not_a_timeline_step_and_refund_status_says_it() -> None:
    shop = Shop()
    await shop.store()
    world = shop.world
    await world.wire(MarkOrderPaid).execute(1001, "x", "owner-1")
    await world.wire(CancelPaidOrder).execute(1001, "Sin producto", "owner-1")
    pending = await shop.get()
    await world.wire(MarkOrderRefunded).execute(1001, "Reembolsado", "owner-1")

    done = await shop.get()

    assert [s.status for s in pending.timeline] == ["pending_payment", "paid", "cancelled"]
    assert pending.refund_status == "pending"
    assert [s.status for s in done.timeline] == ["pending_payment", "paid", "cancelled"]
    assert done.refund_status == "done"
    assert (done.status, done.cancelled_by, done.can_cancel) == ("cancelled", "shop", False)


async def test_a_shop_cancellation_says_shop_and_never_its_reason() -> None:
    shop = Shop()
    await shop.store()
    await shop.world.wire(CancelUnpaidOrder).execute(1001, "El cliente lo pidió", "owner-1")

    view = await shop.get()

    assert view.cancelled_by == "shop"
    assert "El cliente lo pidió" not in view.model_dump_json()


async def test_the_overdue_job_cancellation_says_system() -> None:
    shop = Shop()
    await shop.store(due=timedelta(minutes=-10))
    await shop.world.wire(CancelOverdueOrders).run_once()

    view = await shop.get()

    assert (view.status, view.cancelled_by, view.can_cancel) == ("cancelled", "system", False)


# --- LookUpOrder -------------------------------------------------------------------------------


async def test_a_matching_number_and_email_answer_the_token_of_the_order() -> None:
    shop = Shop()
    order = await shop.store()

    result = await shop.lookup().execute("LFI-1001", "ana@example.com", "1.2.3.4")

    assert result == Ok(LINKS.token(order.id))


@pytest.mark.parametrize("number", ["LFI-1001", "lfi-1001", "1001", "  LFI-1001  ", " 1001 "])
async def test_the_number_is_accepted_with_or_without_prefix_in_any_case(number: str) -> None:
    shop = Shop()
    await shop.store()

    result = await shop.lookup().execute(number, "ana@example.com", "1.2.3.4")

    assert isinstance(result, Ok), result


@pytest.mark.parametrize("email", ["ANA@EXAMPLE.COM", "  ana@example.com  ", " Ana@Example.com"])
async def test_the_email_is_normalized_before_matching(email: str) -> None:
    shop = Shop()
    await shop.store()

    result = await shop.lookup().execute("LFI-1001", email, "1.2.3.4")

    assert isinstance(result, Ok), result


async def test_a_wrong_email_and_an_unknown_number_answer_the_same_error() -> None:
    shop = Shop()
    await shop.store()

    wrong_email = refused(await shop.lookup().execute("LFI-1001", "otro@example.com", "1.2.3.4"))
    unknown = refused(await shop.lookup().execute("LFI-9999", "ana@example.com", "1.2.3.4"))

    assert isinstance(wrong_email, OrderLookupNotFound)
    assert wrong_email == unknown
    assert wrong_email.code == "ORDERS_LOOKUP_NOT_FOUND"


@pytest.mark.parametrize(
    ("number", "email"),
    [
        ("abc", "ana@example.com"),
        ("", "ana@example.com"),
        ("LFI-", "ana@example.com"),
        ("LFI-10x1", "ana@example.com"),
        ("1" * 10, "ana@example.com"),
        ("LFI-1001", "not-an-email"),
        ("LFI-1001", ""),
        ("LFI-1001", "a" * 400 + "@example.com"),
    ],
)
async def test_unparsable_input_answers_the_same_not_found(number: str, email: str) -> None:
    shop = Shop()
    await shop.store()

    error = refused(await shop.lookup().execute(number, email, "1.2.3.4"))

    assert error == OrderLookupNotFound()


async def test_an_unparsable_number_only_counts_the_ip() -> None:
    shop = Shop()

    await shop.lookup().execute("abc", "ana@example.com", "1.2.3.4")

    assert set(shop.throttle.counters) == {"lookup-ip:1.2.3.4"}


async def test_a_parsed_number_counts_the_ip_and_the_number_with_the_same_window_start() -> None:
    shop = Shop()

    await shop.lookup().execute("LFI-1001", "ana@example.com", "1.2.3.4")

    now = shop.world.clock.now()
    assert shop.throttle.counters == {
        "lookup-ip:1.2.3.4": (1, now),
        "lookup-number:1001": (1, now),
    }


async def test_the_number_key_is_the_parsed_number_whatever_its_spelling() -> None:
    shop = Shop()

    for spelling in ("LFI-1001", "1001", "lfi-1001"):
        await shop.lookup().execute(spelling, "x@example.com", "1.2.3.4")

    assert shop.throttle.counters["lookup-number:1001"][0] == 3


async def test_a_missing_ip_counts_under_unknown() -> None:
    shop = Shop()

    await shop.lookup().execute("LFI-1001", "ana@example.com", None)

    assert "lookup-ip:unknown" in shop.throttle.counters


async def test_the_number_limit_allows_five_and_refuses_the_sixth() -> None:
    shop = Shop()
    await shop.store()
    lookup = shop.lookup()

    answers = [
        await lookup.execute("LFI-1001", "otro@example.com", f"10.0.0.{i}")
        for i in range(LOOKUP_NUMBER_MAX + 1)
    ]

    assert all(isinstance(refused(a), OrderLookupNotFound) for a in answers[:LOOKUP_NUMBER_MAX])
    assert isinstance(refused(answers[-1]), OrderLookupRateLimited)


async def test_the_sixth_attempt_is_refused_even_with_the_right_email() -> None:
    shop = Shop()
    await shop.store()
    lookup = shop.lookup()
    for i in range(LOOKUP_NUMBER_MAX):
        await lookup.execute("LFI-1001", "otro@example.com", f"10.0.0.{i}")

    error = refused(await lookup.execute("LFI-1001", "ana@example.com", "10.9.9.9"))

    assert error.code == "ORDERS_LOOKUP_RATE_LIMITED"


async def test_the_ip_limit_allows_twenty_and_refuses_the_twenty_first() -> None:
    shop = Shop()
    lookup = shop.lookup()

    answers = [
        await lookup.execute(f"LFI-{2000 + i}", "x@example.com", "9.9.9.9")
        for i in range(LOOKUP_IP_MAX + 1)
    ]

    assert all(isinstance(refused(a), OrderLookupNotFound) for a in answers[:LOOKUP_IP_MAX])
    assert isinstance(refused(answers[-1]), OrderLookupRateLimited)


async def test_the_ip_limit_applies_to_unparsable_numbers_too() -> None:
    shop = Shop()
    lookup = shop.lookup()
    for _ in range(LOOKUP_IP_MAX):
        await lookup.execute("abc", "x@example.com", "9.9.9.9")

    error = refused(await lookup.execute("abc", "x@example.com", "9.9.9.9"))

    assert isinstance(error, OrderLookupRateLimited)


async def test_another_ip_is_not_limited_by_the_first() -> None:
    shop = Shop()
    lookup = shop.lookup()
    for _ in range(LOOKUP_IP_MAX + 1):
        await lookup.execute("abc", "x@example.com", "9.9.9.9")

    error = refused(await lookup.execute("abc", "x@example.com", "8.8.8.8"))

    assert isinstance(error, OrderLookupNotFound)


async def test_another_number_is_not_limited_by_the_first() -> None:
    shop = Shop()
    lookup = shop.lookup()
    for i in range(LOOKUP_NUMBER_MAX + 1):
        await lookup.execute("LFI-1001", "x@example.com", f"10.0.0.{i}")

    error = refused(await lookup.execute("LFI-1002", "x@example.com", "10.1.1.1"))

    assert isinstance(error, OrderLookupNotFound)


async def test_the_window_resets_after_fifteen_minutes() -> None:
    shop = Shop()
    lookup = shop.lookup()
    for i in range(LOOKUP_NUMBER_MAX + 1):
        await lookup.execute("LFI-1001", "x@example.com", f"10.0.0.{i}")
    shop.world.clock.current = shop.world.clock.now() + LOOKUP_WINDOW

    error = refused(await lookup.execute("LFI-1001", "x@example.com", "10.1.1.1"))

    assert isinstance(error, OrderLookupNotFound)


class LoggingThrottle:
    """Records every hit in the shared log and answers the given counts (1 by default)."""

    def __init__(self, log: list[str], counts: dict[str, int]) -> None:
        self._log = log
        self._counts = counts

    async def hit(self, key: str, now: datetime, window: timedelta) -> int:
        self._log.append(f"hit {key}")
        return self._counts.get(key, 1)


class LoggingQueries:
    """Wraps the customer queries to record when `match` runs."""

    def __init__(self, log: list[str], inner: InMemoryCustomerOrders) -> None:
        self._log = log
        self._inner = inner

    async def match(self, number: int, email: str) -> tuple[UUID, int] | None:
        self._log.append("match")
        return await self._inner.match(number, email)

    async def by_access_digest(self, digest: str) -> CustomerOrderView | None:
        return await self._inner.by_access_digest(digest)

    async def payable_by_access_digest(self, digest: str) -> PayableOrder | None:
        return await self._inner.payable_by_access_digest(digest)


class LoggingRunner:
    """A runner that records `begin` and then `commit` (the work answered Ok) or `rollback`."""

    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def run(self, work: Any) -> Any:
        self._log.append("begin")
        result = await work()
        self._log.append("commit" if isinstance(result, Ok) else "rollback")
        return result


def logged_lookup(shop: Shop, log: list[str], counts: dict[str, int]) -> LookUpOrder:
    return LookUpOrder(
        queries=LoggingQueries(log, shop.customer),
        links=LINKS,
        throttle=LoggingThrottle(log, counts),
        transactions=LoggingRunner(log),
        clock=shop.world.clock,
    )


@pytest.mark.parametrize(
    "counts",
    [{"lookup-number:1001": LOOKUP_NUMBER_MAX + 1}, {"lookup-ip:1.2.3.4": LOOKUP_IP_MAX + 1}],
)
async def test_the_counts_commit_before_the_refusal_and_nothing_is_looked_up(
    counts: dict[str, int],
) -> None:
    log: list[str] = []
    shop = Shop()
    await shop.store()

    error = refused(
        await logged_lookup(shop, log, counts).execute("LFI-1001", "ana@example.com", "1.2.3.4")
    )

    assert isinstance(error, OrderLookupRateLimited)
    assert log == ["begin", "hit lookup-ip:1.2.3.4", "hit lookup-number:1001", "commit"]


async def test_the_counts_are_made_inside_one_transaction_before_the_match() -> None:
    log: list[str] = []
    shop = Shop()
    await shop.store()

    result = await logged_lookup(shop, log, {}).execute("LFI-1001", "ana@example.com", "1.2.3.4")

    assert isinstance(result, Ok)
    assert log == ["begin", "hit lookup-ip:1.2.3.4", "hit lookup-number:1001", "commit", "match"]


# --- CancelOrderByCustomer ---------------------------------------------------------------------


async def test_the_customer_cancels_an_unpaid_order_and_the_reservation_is_released() -> None:
    shop = Shop()
    await shop.store()

    result = await shop.cancel().execute(token_of())

    assert isinstance(result, Ok), result
    stored = shop.world.current()
    assert (stored.status, stored.cancel_reason) == (S.CANCELLED, CUSTOMER_CANCEL_REASON)
    assert shop.world.history() == [(S.PENDING_PAYMENT, S.CANCELLED, CUSTOMER_ACTOR)]
    assert shop.world.stocks.operations == [("release", UUID(int=71))]
    assert shop.world.statuses() == [("pending_payment", "cancelled")]


async def test_after_the_customer_cancels_the_page_says_so() -> None:
    shop = Shop()
    await shop.store()
    await shop.cancel().execute(token_of())

    view = await shop.get()

    assert (view.status, view.cancelled_by, view.can_cancel) == ("cancelled", "customer", False)


async def test_a_paid_order_is_refused_with_transition_invalid_and_stays_untouched() -> None:
    shop = Shop()
    await shop.store(S.PAID)

    error = refused(await shop.cancel().execute(token_of()))

    assert isinstance(error, OrderTransitionInvalid)
    assert error.code == "ORDERS_TRANSITION_INVALID"
    assert shop.world.current().status is S.PAID
    assert shop.world.stocks.operations == []
    assert shop.world.history() == []


async def test_cancelling_twice_refuses_the_second_time() -> None:
    shop = Shop()
    await shop.store()
    await shop.cancel().execute(token_of())

    error = refused(await shop.cancel().execute(token_of()))

    assert isinstance(error, OrderTransitionInvalid)
    assert shop.world.stocks.operations == [("release", UUID(int=71))]


async def test_an_unknown_token_is_order_not_found_and_changes_nothing() -> None:
    shop = Shop()
    await shop.store()

    error = refused(await shop.cancel().execute("nope"))

    assert isinstance(error, OrderNotFound)
    assert shop.world.current().status is S.PENDING_PAYMENT
    assert shop.world.stocks.operations == []


async def test_cancelling_one_order_leaves_the_others() -> None:
    shop = Shop()
    await shop.store(n=1)
    await shop.store(n=2)

    await shop.cancel().execute(token_of(2))

    assert [o.status for o in shop.world.orders.orders] == [S.PENDING_PAYMENT, S.CANCELLED]
