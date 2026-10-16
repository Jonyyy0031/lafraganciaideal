from dataclasses import dataclass
from uuid import UUID

from fragancia_api.modules.orders.application.ports import CartTokens, CatalogOffers
from fragancia_api.modules.orders.application.queries.cart import EMPTY_CART, to_contract
from fragancia_api.modules.orders.contracts import CartView
from fragancia_api.modules.orders.domain.cart import CART_LIFETIME, Cart, Offer, cart_view
from fragancia_api.modules.orders.domain.errors import (
    CartPresentationUnavailable,
    CartStockUnavailable,
)
from fragancia_api.modules.orders.domain.repositories import CartRepository
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result, new_id

PURGE_BATCH = 500


@dataclass(frozen=True, slots=True)
class CartOutcome:
    """The fresh cart view and the cookie token of the cart that exists (a new one, or the
    caller's own); None when there is no cart, so no cookie is set."""

    view: CartView
    token: str | None


class SetCartLine:
    """Command: put a presentation in the cart with a quantity (1 to 10), creating the cart
    when there is none. Refuses a presentation that is unknown or hidden, and more units than
    are available for an in-stock one (decision 21). It never reserves stock."""

    def __init__(
        self,
        *,
        carts: CartRepository,
        tokens: CartTokens,
        offers: CatalogOffers,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._carts = carts
        self._tokens = tokens
        self._offers = offers
        self._transactions = transactions
        self._clock = clock

    async def execute(
        self, token: str | None, presentation_id: UUID, quantity: int
    ) -> Result[CartOutcome, DomainError]:
        async def work() -> Result[tuple[Cart, str, dict[UUID, Offer]], DomainError]:
            now = self._clock.now()
            existing = None
            if token is not None:
                existing = await self._carts.get_by_token_for_update(self._tokens.digest(token))
            if existing is not None and token is not None:
                cart, active_token, is_new = existing, token, False
            else:
                active_token = self._tokens.new()
                cart = Cart.create(new_id(), self._tokens.digest(active_token), now)
                is_new = True
            ids = [line.presentation_id for line in cart.lines]
            if presentation_id not in ids:
                ids.append(presentation_id)
            offers = await self._offers.offers(ids, now)
            offer = offers.get(presentation_id)
            if offer is None or not offer.is_visible:
                return Err(CartPresentationUnavailable())
            if (
                offer.availability == "in_stock"
                and offer.available_units is not None
                and quantity > offer.available_units
            ):
                return Err(
                    CartStockUnavailable(
                        message=f"Solo hay {max(offer.available_units, 0)} disponibles",
                        details={"available": max(offer.available_units, 0)},
                    )
                )
            match cart.set_line(presentation_id, quantity, offer.price_cents, now):
                case Err(invalid):
                    return Err(invalid)
                case Ok(_):
                    pass
            if is_new:
                await self._carts.add(cart)
            else:
                await self._carts.save(cart)
            return Ok((cart, active_token, offers))

        match await self._transactions.run(work):
            case Err(error):
                return Err(error)
            case Ok((cart, active_token, offers)):
                view = cart_view(cart, offers, self._clock.now())
                return Ok(CartOutcome(to_contract(view), active_token))


class RemoveCartLine:
    """Command: remove a line. An absent line is a no-op; no token or an unknown one gives the
    empty cart."""

    def __init__(
        self,
        *,
        carts: CartRepository,
        tokens: CartTokens,
        offers: CatalogOffers,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._carts = carts
        self._tokens = tokens
        self._offers = offers
        self._transactions = transactions
        self._clock = clock

    async def execute(
        self, token: str | None, presentation_id: UUID
    ) -> Result[CartOutcome, DomainError]:
        if token is None:
            return Ok(CartOutcome(EMPTY_CART, None))

        async def work() -> Result[Cart | None, DomainError]:
            cart = await self._carts.get_by_token_for_update(self._tokens.digest(token))
            if cart is None:
                return Ok(None)
            cart.remove_line(presentation_id, self._clock.now())
            await self._carts.save(cart)
            return Ok(cart)

        match await self._transactions.run(work):
            case Err(error):
                return Err(error)
            case Ok(found):
                if found is None:
                    return Ok(CartOutcome(EMPTY_CART, None))
                now = self._clock.now()
                offers = await self._offers.offers(
                    [line.presentation_id for line in found.lines], now
                )
                return Ok(CartOutcome(to_contract(cart_view(found, offers, now)), token))


class AcknowledgeCartPrices:
    """Command: the buyer confirms the current prices ("Entendido", decision 23)."""

    def __init__(
        self,
        *,
        carts: CartRepository,
        tokens: CartTokens,
        offers: CatalogOffers,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._carts = carts
        self._tokens = tokens
        self._offers = offers
        self._transactions = transactions
        self._clock = clock

    async def execute(self, token: str | None) -> Result[CartOutcome, DomainError]:
        if token is None:
            return Ok(CartOutcome(EMPTY_CART, None))

        async def work() -> Result[tuple[Cart, dict[UUID, Offer]] | None, DomainError]:
            cart = await self._carts.get_by_token_for_update(self._tokens.digest(token))
            if cart is None:
                return Ok(None)
            now = self._clock.now()
            offers = await self._offers.offers([line.presentation_id for line in cart.lines], now)
            cart.acknowledge_prices({pid: offer.price_cents for pid, offer in offers.items()}, now)
            await self._carts.save(cart)
            return Ok((cart, offers))

        match await self._transactions.run(work):
            case Err(error):
                return Err(error)
            case Ok(found):
                if found is None:
                    return Ok(CartOutcome(EMPTY_CART, None))
                cart, offers = found
                view = cart_view(cart, offers, self._clock.now())
                return Ok(CartOutcome(to_contract(view), token))


class PurgeCarts:
    """Worker job: delete carts untouched for 30 days (decision 15), their lines with them.
    Returns how many were deleted."""

    def __init__(
        self, *, carts: CartRepository, transactions: TransactionRunner, clock: Clock
    ) -> None:
        self._carts = carts
        self._transactions = transactions
        self._clock = clock

    async def run_once(self) -> int:
        async def work() -> Result[int, DomainError]:
            cutoff = self._clock.now() - CART_LIFETIME
            return Ok(await self._carts.delete_untouched_before(cutoff, PURGE_BATCH))

        match await self._transactions.run(work):
            case Ok(deleted):
                return deleted
            case Err(_):
                return 0
