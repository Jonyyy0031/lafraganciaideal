from fragancia_api.modules.orders.application.ports import CartReader, CartTokens, CatalogOffers
from fragancia_api.modules.orders.contracts import CartLineView, CartView
from fragancia_api.modules.orders.domain import cart as domain
from fragancia_api.shared.application.clock import Clock


def to_contract(view: domain.CartView) -> CartView:
    return CartView(
        lines=[
            CartLineView(
                presentation_id=line.presentation_id,
                quantity=line.quantity,
                status=line.status,
                label=line.label,
                perfume_slug=line.perfume_slug,
                card_url=line.card_url,
                ml=line.ml,
                availability=line.availability,
                lead_time_min_days=line.lead_time_min_days,
                lead_time_max_days=line.lead_time_max_days,
                available_units=line.available_units,
                unit_price_cents=line.unit_price_cents,
                line_total_cents=line.line_total_cents,
                price_changed_from_cents=line.price_changed_from_cents,
            )
            for line in view.lines
        ],
        subtotal_cents=view.subtotal_cents,
        item_count=view.item_count,
        has_problems=view.has_problems,
        lead_time_max_days=view.lead_time_max_days,
    )


EMPTY_CART = CartView(
    lines=[], subtotal_cents=0, item_count=0, has_problems=False, lead_time_max_days=None
)


class GetCart:
    """Query: the cart behind a cookie token, with current prices and availability. No token
    or an unknown one gives the empty cart, and nothing is written."""

    def __init__(
        self, *, reader: CartReader, tokens: CartTokens, offers: CatalogOffers, clock: Clock
    ) -> None:
        self._reader = reader
        self._tokens = tokens
        self._offers = offers
        self._clock = clock

    async def execute(self, token: str | None) -> CartView:
        if token is None:
            return EMPTY_CART
        cart = await self._reader.by_token(self._tokens.digest(token))
        if cart is None:
            return EMPTY_CART
        now = self._clock.now()
        offers = await self._offers.offers([line.presentation_id for line in cart.lines], now)
        return to_contract(domain.cart_view(cart, offers, now))
