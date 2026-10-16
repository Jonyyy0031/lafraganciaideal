"""Cart use cases over the in-memory adapters: new cart and token, unknown token, refusals,
price acknowledgement, the read query and the purge job."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from fragancia_api.modules.orders.application.commands.cart import (
    PURGE_BATCH,
    AcknowledgeCartPrices,
    CartOutcome,
    PurgeCarts,
    RemoveCartLine,
    SetCartLine,
)
from fragancia_api.modules.orders.application.queries.cart import EMPTY_CART, GetCart
from fragancia_api.modules.orders.domain.cart import CART_LIFETIME, Cart, CartLine, Offer
from fragancia_api.modules.orders.domain.errors import (
    CartPresentationUnavailable,
    CartQuantityInvalid,
    CartStockUnavailable,
    CartTooManyLines,
)
from fragancia_api.modules.orders.infrastructure.in_memory import (
    InMemoryCarts,
    InMemoryCatalogOffers,
)
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result
from tests.unit.orders.support import NOW, made_to_order_offer, make_offer, presentation_id


class SequenceTokens:
    """Deterministic tokens: `token-1`, `token-2`, ...; the digest is `digest:<token>`."""

    def __init__(self) -> None:
        self.issued = 0

    def new(self) -> str:
        self.issued += 1
        return f"token-{self.issued}"

    def digest(self, token: str) -> str:
        return f"digest:{token}"


class CountingCarts(InMemoryCarts):
    """Records the writes, to prove what must not write does not."""

    def __init__(self, *carts: Cart) -> None:
        super().__init__(*carts)
        self.writes: list[str] = []

    async def add(self, cart: Cart) -> None:
        self.writes.append("add")
        await super().add(cart)

    async def save(self, cart: Cart) -> None:
        self.writes.append("save")
        await super().save(cart)


@dataclass
class World:
    offers: InMemoryCatalogOffers = field(default_factory=InMemoryCatalogOffers)
    carts: CountingCarts = field(default_factory=CountingCarts)
    tokens: SequenceTokens = field(default_factory=SequenceTokens)
    clock: FixedClock = field(default_factory=FixedClock)

    def __post_init__(self) -> None:
        self.transactions = InMemoryTransactionRunner()

    def set_line(self) -> SetCartLine:
        return SetCartLine(
            carts=self.carts,
            tokens=self.tokens,
            offers=self.offers,
            transactions=self.transactions,
            clock=self.clock,
        )

    def remove_line(self) -> RemoveCartLine:
        return RemoveCartLine(
            carts=self.carts,
            tokens=self.tokens,
            offers=self.offers,
            transactions=self.transactions,
            clock=self.clock,
        )

    def acknowledge(self) -> AcknowledgeCartPrices:
        return AcknowledgeCartPrices(
            carts=self.carts,
            tokens=self.tokens,
            offers=self.offers,
            transactions=self.transactions,
            clock=self.clock,
        )

    def get_cart(self) -> GetCart:
        return GetCart(reader=self.carts, tokens=self.tokens, offers=self.offers, clock=self.clock)

    def purge(self) -> PurgeCarts:
        return PurgeCarts(carts=self.carts, transactions=self.transactions, clock=self.clock)

    def stored(self, token: str) -> Cart:
        found = [c for c in self.carts.by_id.values() if c.token_digest == f"digest:{token}"]
        assert len(found) == 1
        return found[0]

    def seed_cart(
        self, token: str, *lines: tuple[int, int, int], updated_at: datetime = NOW
    ) -> Cart:
        """A stored cart behind `token` with `(n, quantity, seen_price_cents)` lines."""
        cart = Cart(
            id=UUID(int=500 + len(self.carts.by_id)),
            token_digest=f"digest:{token}",
            lines=[CartLine(presentation_id(n), q, seen, NOW) for n, q, seen in lines],
            created_at=updated_at,
            updated_at=updated_at,
        )
        self.carts.by_id[cart.id] = cart
        return cart


def ok(result: Result[CartOutcome, DomainError]) -> CartOutcome:
    assert isinstance(result, Ok), result
    return result.value


def refused(result: Result[CartOutcome, DomainError]) -> DomainError:
    assert isinstance(result, Err), result
    return result.error


# --- SetCartLine --------------------------------------------------------------------------------


async def test_the_first_line_creates_a_cart_and_a_token_stores_only_its_digest() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1)))

    outcome = ok(await world.set_line().execute(None, presentation_id(1), 2))

    assert outcome.token == "token-1"
    assert [(line.presentation_id, line.quantity) for line in outcome.view.lines] == [
        (presentation_id(1), 2)
    ]
    [cart] = world.carts.by_id.values()
    assert cart.token_digest == "digest:token-1"
    assert cart.token_digest != "token-1"
    assert (cart.created_at, cart.updated_at) == (NOW, NOW)
    assert world.carts.writes == ["add"]


async def test_the_returned_view_prices_the_line_and_totals_it() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1, price_cents=300_000)))

    outcome = ok(await world.set_line().execute(None, presentation_id(1), 2))

    [line] = outcome.view.lines
    assert (line.status, line.unit_price_cents, line.line_total_cents) == ("ok", 300_000, 600_000)
    assert (outcome.view.subtotal_cents, outcome.view.item_count) == (600_000, 2)


async def test_a_second_line_goes_in_the_callers_cart_and_returns_the_same_token() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1), make_offer(2)))
    first = ok(await world.set_line().execute(None, presentation_id(1), 1))
    world.clock.current = NOW + timedelta(minutes=5)

    second = ok(await world.set_line().execute(first.token, presentation_id(2), 3))

    assert second.token == first.token
    assert len(world.carts.by_id) == 1
    assert [(line.presentation_id, line.quantity) for line in second.view.lines] == [
        (presentation_id(1), 1),
        (presentation_id(2), 3),
    ]
    assert world.stored("token-1").updated_at == NOW + timedelta(minutes=5)
    assert world.carts.writes == ["add", "save"]


async def test_an_unknown_token_gets_a_new_cart_and_a_fresh_token() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1)))

    outcome = ok(await world.set_line().execute("stale-cookie", presentation_id(1), 1))

    assert outcome.token == "token-1"
    assert outcome.token != "stale-cookie"
    assert len(world.carts.by_id) == 1


async def test_changing_the_quantity_acknowledges_the_price_the_buyer_now_sees() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1, price_cents=280_000)))
    world.seed_cart("t", (1, 2, 250_000))

    outcome = ok(await world.set_line().execute("t", presentation_id(1), 3))

    [line] = outcome.view.lines
    assert line.quantity == 3
    assert line.price_changed_from_cents is None
    assert world.stored("t").lines[0].seen_price_cents == 280_000


async def test_another_line_does_not_acknowledge_the_price_of_the_untouched_ones() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1, price_cents=280_000), make_offer(2)))
    world.seed_cart("t", (1, 1, 250_000))

    outcome = ok(await world.set_line().execute("t", presentation_id(2), 1))

    by_id = {line.presentation_id: line for line in outcome.view.lines}
    assert by_id[presentation_id(1)].price_changed_from_cents == 250_000
    assert by_id[presentation_id(2)].price_changed_from_cents is None


async def test_an_unknown_presentation_is_refused_and_nothing_is_stored() -> None:
    world = World()

    error = refused(await world.set_line().execute(None, presentation_id(1), 1))

    assert isinstance(error, CartPresentationUnavailable)
    assert error.code == "ORDERS_CART_PRESENTATION_UNAVAILABLE"
    assert world.carts.by_id == {}
    assert world.carts.writes == []


async def test_a_hidden_presentation_is_refused() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1, is_visible=False)))

    error = refused(await world.set_line().execute(None, presentation_id(1), 1))

    assert isinstance(error, CartPresentationUnavailable)
    assert world.carts.by_id == {}


async def test_a_refused_line_leaves_the_existing_cart_untouched() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(2, available_units=1)))
    world.seed_cart("t", (1, 1, 100))
    before = world.stored("t")

    refused(await world.set_line().execute("t", presentation_id(2), 2))

    assert world.stored("t").lines == before.lines
    assert world.stored("t").updated_at == before.updated_at
    assert world.carts.writes == []


async def test_more_units_than_available_is_refused_with_the_available_figure() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1, available_units=3)))

    error = refused(await world.set_line().execute(None, presentation_id(1), 4))

    assert isinstance(error, CartStockUnavailable)
    assert error.code == "ORDERS_CART_STOCK_UNAVAILABLE"
    assert error.details == {"available": 3}
    assert world.carts.by_id == {}


async def test_exactly_the_available_units_is_accepted() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1, available_units=3)))

    outcome = ok(await world.set_line().execute(None, presentation_id(1), 3))

    assert outcome.view.lines[0].status == "ok"


async def test_a_sold_out_presentation_is_refused_with_zero_available() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1, available_units=0)))

    error = refused(await world.set_line().execute(None, presentation_id(1), 1))

    assert isinstance(error, CartStockUnavailable)
    assert error.details == {"available": 0}


async def test_a_negative_stock_copy_is_reported_as_zero_available() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1, available_units=-2)))

    error = refused(await world.set_line().execute(None, presentation_id(1), 1))

    assert isinstance(error, CartStockUnavailable)
    assert error.details == {"available": 0}


async def test_made_to_order_accepts_up_to_ten_whatever_the_stock() -> None:
    world = World(offers=InMemoryCatalogOffers(made_to_order_offer(1, max_days=7)))

    outcome = ok(await world.set_line().execute(None, presentation_id(1), 10))

    assert outcome.view.lines[0].status == "ok"
    assert outcome.view.lead_time_max_days == 7


async def test_a_quantity_of_eleven_is_refused_by_the_domain_and_nothing_is_stored() -> None:
    world = World(offers=InMemoryCatalogOffers(made_to_order_offer(1)))

    error = refused(await world.set_line().execute(None, presentation_id(1), 11))

    assert isinstance(error, CartQuantityInvalid)
    assert error.details == {"min": 1, "max": 10}
    assert world.carts.by_id == {}


async def test_a_quantity_of_zero_is_refused() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1)))

    error = refused(await world.set_line().execute(None, presentation_id(1), 0))

    assert isinstance(error, CartQuantityInvalid)


async def test_a_thirty_first_line_is_refused() -> None:
    offers = [make_offer(n) for n in range(1, 32)]
    world = World(offers=InMemoryCatalogOffers(*offers))
    world.seed_cart("t", *[(n, 1, 250_000) for n in range(1, 31)])

    error = refused(await world.set_line().execute("t", presentation_id(31), 1))

    assert isinstance(error, CartTooManyLines)
    assert len(world.stored("t").lines) == 30


async def test_the_view_flags_the_other_lines_that_went_bad() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1, available_units=0), make_offer(2)))
    world.seed_cart("t", (1, 1, 250_000))

    outcome = ok(await world.set_line().execute("t", presentation_id(2), 1))

    assert [line.status for line in outcome.view.lines] == ["sold_out", "ok"]
    assert outcome.view.has_problems is True
    assert outcome.view.subtotal_cents == 250_000


# --- RemoveCartLine -----------------------------------------------------------------------------


async def test_removing_a_line_returns_the_remaining_cart_and_the_token() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1), make_offer(2)))
    world.seed_cart("t", (1, 1, 250_000), (2, 2, 250_000))

    outcome = ok(await world.remove_line().execute("t", presentation_id(1)))

    assert outcome.token == "t"
    assert [line.presentation_id for line in outcome.view.lines] == [presentation_id(2)]
    assert outcome.view.subtotal_cents == 500_000
    assert [line.presentation_id for line in world.stored("t").lines] == [presentation_id(2)]


async def test_removing_an_absent_line_is_a_no_op_that_still_answers_the_cart() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1)))
    world.seed_cart("t", (1, 1, 250_000), updated_at=NOW - timedelta(days=1))

    outcome = ok(await world.remove_line().execute("t", presentation_id(9)))

    assert [line.presentation_id for line in outcome.view.lines] == [presentation_id(1)]
    assert outcome.token == "t"
    assert world.stored("t").updated_at == NOW - timedelta(days=1)


async def test_removing_the_last_line_leaves_an_empty_cart_that_still_has_its_token() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1)))
    world.seed_cart("t", (1, 1, 250_000))

    outcome = ok(await world.remove_line().execute("t", presentation_id(1)))

    assert outcome.view == EMPTY_CART
    assert outcome.token == "t"
    assert len(world.carts.by_id) == 1


async def test_removing_without_a_token_answers_the_empty_cart_without_a_cookie() -> None:
    world = World()

    outcome = ok(await world.remove_line().execute(None, presentation_id(1)))

    assert outcome == CartOutcome(EMPTY_CART, None)
    assert world.carts.writes == []


async def test_removing_with_an_unknown_token_answers_the_empty_cart_without_a_cookie() -> None:
    world = World()

    outcome = ok(await world.remove_line().execute("nobody", presentation_id(1)))

    assert outcome == CartOutcome(EMPTY_CART, None)
    assert world.carts.writes == []


# --- AcknowledgeCartPrices ----------------------------------------------------------------------


async def test_acknowledging_clears_every_price_change_and_keeps_the_token() -> None:
    world = World(
        offers=InMemoryCatalogOffers(
            make_offer(1, price_cents=280_000), make_offer(2, price_cents=100_000)
        )
    )
    world.seed_cart("t", (1, 1, 250_000), (2, 1, 120_000))

    outcome = ok(await world.acknowledge().execute("t"))

    assert outcome.token == "t"
    assert [line.price_changed_from_cents for line in outcome.view.lines] == [None, None]
    assert [line.seen_price_cents for line in world.stored("t").lines] == [280_000, 100_000]
    assert world.stored("t").updated_at == NOW


async def test_acknowledging_leaves_a_line_without_an_offer_as_it_was() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1, price_cents=280_000)))
    world.seed_cart("t", (1, 1, 250_000), (2, 1, 120_000))

    ok(await world.acknowledge().execute("t"))

    assert [line.seen_price_cents for line in world.stored("t").lines] == [280_000, 120_000]


async def test_acknowledging_without_a_token_answers_the_empty_cart_without_a_cookie() -> None:
    world = World()

    assert ok(await world.acknowledge().execute(None)) == CartOutcome(EMPTY_CART, None)
    assert world.carts.writes == []


async def test_acknowledging_with_an_unknown_token_answers_the_empty_cart_without_a_cookie() -> (
    None
):
    world = World()

    assert ok(await world.acknowledge().execute("nobody")) == CartOutcome(EMPTY_CART, None)
    assert world.carts.writes == []


# --- GetCart ------------------------------------------------------------------------------------


async def test_getting_a_cart_shows_current_prices_and_availability() -> None:
    world = World(
        offers=InMemoryCatalogOffers(
            make_offer(1, price_cents=280_000),
            make_offer(2, available_units=0),
            made_to_order_offer(3, max_days=6),
        )
    )
    world.seed_cart("t", (1, 2, 250_000), (2, 1, 250_000), (3, 1, 250_000))

    view = await world.get_cart().execute("t")

    assert [line.status for line in view.lines] == ["ok", "sold_out", "ok"]
    assert view.lines[0].price_changed_from_cents == 250_000
    assert (view.subtotal_cents, view.item_count) == (560_000 + 250_000, 3)
    assert view.has_problems is True
    assert view.lead_time_max_days == 6


async def test_getting_without_a_token_is_the_empty_cart_and_writes_nothing() -> None:
    world = World()

    assert await world.get_cart().execute(None) == EMPTY_CART
    assert world.carts.writes == []
    assert world.tokens.issued == 0


async def test_getting_with_an_unknown_token_is_the_empty_cart_and_writes_nothing() -> None:
    world = World()

    assert await world.get_cart().execute("nobody") == EMPTY_CART
    assert world.carts.writes == []
    assert world.tokens.issued == 0


async def test_getting_a_cart_never_touches_it() -> None:
    world = World(offers=InMemoryCatalogOffers(make_offer(1)))
    world.seed_cart("t", (1, 1, 100), updated_at=NOW - timedelta(days=3))

    await world.get_cart().execute("t")

    assert world.carts.writes == []
    assert world.stored("t").updated_at == NOW - timedelta(days=3)


async def test_the_empty_cart_has_no_lines_and_no_problems() -> None:
    assert EMPTY_CART.lines == []
    assert (EMPTY_CART.subtotal_cents, EMPTY_CART.item_count) == (0, 0)
    assert (EMPTY_CART.has_problems, EMPTY_CART.lead_time_max_days) == (False, None)


# --- PurgeCarts ---------------------------------------------------------------------------------


async def test_the_purge_deletes_carts_untouched_for_more_than_thirty_days() -> None:
    world = World()
    stale = world.seed_cart(
        "stale", (1, 1, 100), updated_at=NOW - CART_LIFETIME - timedelta(days=1)
    )
    fresh = world.seed_cart("fresh", (1, 1, 100), updated_at=NOW - timedelta(days=29))

    deleted = await world.purge().run_once()

    assert deleted == 1
    assert stale.id not in world.carts.by_id
    assert fresh.id in world.carts.by_id


async def test_a_cart_updated_exactly_at_the_cutoff_is_kept() -> None:
    world = World()
    boundary = world.seed_cart("edge", (1, 1, 100), updated_at=NOW - CART_LIFETIME)

    deleted = await world.purge().run_once()

    assert deleted == 0
    assert boundary.id in world.carts.by_id


async def test_the_purge_with_nothing_stale_deletes_nothing() -> None:
    assert await World().purge().run_once() == 0


async def test_the_purge_stops_at_its_batch_size_and_a_second_run_takes_the_rest() -> None:
    world = World()
    old = NOW - CART_LIFETIME - timedelta(days=1)
    for index in range(PURGE_BATCH + 3):
        cart = Cart(
            id=UUID(int=10_000 + index),
            token_digest=f"digest:{index}",
            lines=[],
            created_at=old,
            updated_at=old + timedelta(seconds=index),
        )
        world.carts.by_id[cart.id] = cart

    first = await world.purge().run_once()
    second = await world.purge().run_once()

    assert (first, second) == (PURGE_BATCH, 3)
    assert world.carts.by_id == {}


# --- no catalog read while the cart's row lock is held --------------------------------------------


class LockWindowRunner(InMemoryTransactionRunner):
    """Marks the span of each transaction, standing in for the pooled connection holding a lock."""

    def __init__(self) -> None:
        self.open = False

    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
        self.open = True
        try:
            return await work()
        finally:
            self.open = False


class WatchedOffers(InMemoryCatalogOffers):
    """Records whether each catalog read happened inside a transaction."""

    def __init__(self, runner: LockWindowRunner, *offers: Offer) -> None:
        super().__init__(*offers)
        self.runner = runner
        self.reads_inside_a_transaction = 0

    async def offers(self, ids: Sequence[UUID], now: datetime) -> dict[UUID, Offer]:
        if self.runner.open:
            self.reads_inside_a_transaction += 1
        return await super().offers(ids, now)


def watched_world(*offers: Offer) -> tuple[World, WatchedOffers]:
    runner = LockWindowRunner()
    watched = WatchedOffers(runner, *offers)
    world = World(offers=watched)
    world.transactions = runner
    return world, watched


async def test_setting_a_line_reads_the_catalog_outside_the_transaction() -> None:
    world, watched = watched_world(make_offer(1), make_offer(2))
    world.seed_cart("t", (2, 1, 250_000))

    ok(await world.set_line().execute("t", presentation_id(1), 2))

    assert watched.reads_inside_a_transaction == 0


async def test_acknowledging_reads_the_catalog_outside_the_transaction() -> None:
    world, watched = watched_world(make_offer(1, price_cents=280_000))
    world.seed_cart("t", (1, 1, 250_000))

    outcome = ok(await world.acknowledge().execute("t"))

    assert watched.reads_inside_a_transaction == 0
    assert [line.price_changed_from_cents for line in outcome.view.lines] == [None]
    assert world.stored("t").lines[0].seen_price_cents == 280_000


# --- AcknowledgeCartPrices: the window between listing the ids and locking -----------------------


class MeddlingOffers(InMemoryCatalogOffers):
    """Runs `meddle` once, during the first catalog read: the window in which another request
    may change the cart, because `AcknowledgeCartPrices` holds no lock while it reads offers."""

    def __init__(self, meddle: Callable[[], None], *offers: Offer) -> None:
        super().__init__(*offers)
        self.meddle: Callable[[], None] | None = meddle

    async def offers(self, ids: Sequence[UUID], now: datetime) -> dict[UUID, Offer]:
        found = await super().offers(ids, now)
        if self.meddle is not None:
            meddle, self.meddle = self.meddle, None
            meddle()
        return found


def meddling_world(meddle: Callable[[World], None], *offers: Offer) -> World:
    holder: list[World] = []
    world = World(offers=MeddlingOffers(lambda: meddle(holder[0]), *offers))
    holder.append(world)
    return world


async def test_a_line_added_while_acknowledging_keeps_the_price_its_buyer_saw() -> None:
    def add_a_line(world: World) -> None:
        world.stored("t").set_line(presentation_id(2), 1, 200_000, NOW)

    world = meddling_world(
        add_a_line, make_offer(1, price_cents=280_000), make_offer(2, price_cents=300_000)
    )
    world.seed_cart("t", (1, 1, 250_000))

    outcome = ok(await world.acknowledge().execute("t"))

    seen = {line.presentation_id: line.seen_price_cents for line in world.stored("t").lines}
    assert seen == {presentation_id(1): 280_000, presentation_id(2): 200_000}
    changed = {line.presentation_id: line.price_changed_from_cents for line in outcome.view.lines}
    assert changed == {presentation_id(1): None, presentation_id(2): 200_000}
    assert outcome.token == "t"


async def test_a_line_removed_while_acknowledging_is_ignored() -> None:
    def remove_a_line(world: World) -> None:
        world.stored("t").remove_line(presentation_id(2), NOW)

    world = meddling_world(
        remove_a_line, make_offer(1, price_cents=280_000), make_offer(2, price_cents=300_000)
    )
    world.seed_cart("t", (1, 1, 250_000), (2, 1, 250_000))

    outcome = ok(await world.acknowledge().execute("t"))

    assert [line.presentation_id for line in outcome.view.lines] == [presentation_id(1)]
    assert [(line.presentation_id, line.seen_price_cents) for line in world.stored("t").lines] == [
        (presentation_id(1), 280_000)
    ]


async def test_a_cart_purged_while_acknowledging_answers_the_empty_cart_without_a_cookie() -> None:
    def purge(world: World) -> None:
        world.carts.by_id.clear()

    world = meddling_world(purge, make_offer(1, price_cents=280_000))
    world.seed_cart("t", (1, 1, 250_000))

    outcome = ok(await world.acknowledge().execute("t"))

    assert outcome == CartOutcome(EMPTY_CART, None)
    assert world.carts.writes == []
