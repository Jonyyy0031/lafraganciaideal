"""The server-side cart: lines a guest intends to buy, plus the pure view computed from the
current catalog offers. A cart never reserves stock."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from fragancia_api.modules.orders.domain.errors import CartQuantityInvalid, CartTooManyLines
from fragancia_api.shared.kernel import AggregateRoot, Err, Ok, Result

LINE_QUANTITY_MAX = 10
CART_LINES_MAX = 30
CART_LIFETIME = timedelta(days=30)

type LineStatus = Literal["ok", "unavailable", "sold_out", "insufficient_stock"]


@dataclass(frozen=True, slots=True)
class Offer:
    """What the catalog offers for a presentation right now (orders' own copy of the facts)."""

    presentation_id: UUID
    perfume_id: UUID
    perfume_slug: str
    label: str
    ml: int
    card_url: str | None
    price_cents: int
    regular_price_cents: int
    availability: Literal["in_stock", "made_to_order"]
    lead_time_min_days: int | None
    lead_time_max_days: int | None
    is_visible: bool
    available_units: int | None


@dataclass(frozen=True, slots=True)
class CartLine:
    presentation_id: UUID
    quantity: int
    seen_price_cents: int
    added_at: datetime


class Cart(AggregateRoot):
    def __init__(
        self,
        *,
        id: UUID,
        token_digest: str,
        lines: Sequence[CartLine],
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        super().__init__()
        self.id = id
        self.token_digest = token_digest
        self.lines: list[CartLine] = sorted(lines, key=lambda line: line.added_at)
        self.created_at = created_at
        self.updated_at = updated_at

    @classmethod
    def create(cls, id: UUID, token_digest: str, now: datetime) -> Cart:
        return cls(id=id, token_digest=token_digest, lines=[], created_at=now, updated_at=now)

    def set_line(
        self, presentation_id: UUID, quantity: int, current_price_cents: int, now: datetime
    ) -> Result[None, CartQuantityInvalid | CartTooManyLines]:
        """Set a line's quantity. Changing it acknowledges the current price (decision 23)."""
        if not 1 <= quantity <= LINE_QUANTITY_MAX:
            return Err(
                CartQuantityInvalid(details={"min": 1, "max": LINE_QUANTITY_MAX}),
            )
        for index, line in enumerate(self.lines):
            if line.presentation_id == presentation_id:
                self.lines[index] = CartLine(
                    presentation_id, quantity, current_price_cents, line.added_at
                )
                self.updated_at = now
                return Ok(None)
        if len(self.lines) >= CART_LINES_MAX:
            return Err(CartTooManyLines(details={"max": CART_LINES_MAX}))
        self.lines.append(CartLine(presentation_id, quantity, current_price_cents, now))
        self.updated_at = now
        return Ok(None)

    def remove_line(self, presentation_id: UUID, now: datetime) -> None:
        """Remove a line; nothing happens when it is absent."""
        kept = [line for line in self.lines if line.presentation_id != presentation_id]
        if len(kept) != len(self.lines):
            self.lines = kept
            self.updated_at = now

    def acknowledge_prices(self, current: Mapping[UUID, int], now: datetime) -> None:
        """Take the current price as seen for every line present in `current`."""
        self.lines = [
            CartLine(
                line.presentation_id,
                line.quantity,
                current.get(line.presentation_id, line.seen_price_cents),
                line.added_at,
            )
            for line in self.lines
        ]
        self.updated_at = now


@dataclass(frozen=True, slots=True)
class CartLineView:
    presentation_id: UUID
    quantity: int
    status: LineStatus
    label: str | None
    perfume_slug: str | None
    card_url: str | None
    ml: int | None
    availability: Literal["in_stock", "made_to_order"] | None
    lead_time_min_days: int | None
    lead_time_max_days: int | None
    available_units: int | None
    unit_price_cents: int | None
    line_total_cents: int | None
    price_changed_from_cents: int | None


@dataclass(frozen=True, slots=True)
class CartView:
    lines: list[CartLineView]
    subtotal_cents: int
    item_count: int
    has_problems: bool
    lead_time_max_days: int | None


def _status(quantity: int, offer: Offer | None) -> LineStatus:
    if offer is None or not offer.is_visible:
        return "unavailable"
    if offer.availability == "in_stock" and offer.available_units is not None:
        if offer.available_units <= 0:
            return "sold_out"
        if offer.available_units < quantity:
            return "insufficient_stock"
    return "ok"


def cart_view(cart: Cart, offers: Mapping[UUID, Offer], now: datetime) -> CartView:
    """The cart as the buyer sees it now. The subtotal, the item count and the lead time count
    only the `ok` lines (decision 22). `now` is the moment the offers were priced at."""
    del now  # the offers already carry their prices
    lines: list[CartLineView] = []
    subtotal = 0
    items = 0
    lead_time: int | None = None
    for line in cart.lines:
        offer = offers.get(line.presentation_id)
        status = _status(line.quantity, offer)
        if offer is None:
            lines.append(
                CartLineView(
                    presentation_id=line.presentation_id,
                    quantity=line.quantity,
                    status=status,
                    label=None,
                    perfume_slug=None,
                    card_url=None,
                    ml=None,
                    availability=None,
                    lead_time_min_days=None,
                    lead_time_max_days=None,
                    available_units=None,
                    unit_price_cents=None,
                    line_total_cents=None,
                    price_changed_from_cents=None,
                )
            )
            continue
        total = offer.price_cents * line.quantity
        lines.append(
            CartLineView(
                presentation_id=line.presentation_id,
                quantity=line.quantity,
                status=status,
                label=offer.label,
                perfume_slug=offer.perfume_slug,
                card_url=offer.card_url,
                ml=offer.ml,
                availability=offer.availability,
                lead_time_min_days=offer.lead_time_min_days,
                lead_time_max_days=offer.lead_time_max_days,
                available_units=offer.available_units,
                unit_price_cents=offer.price_cents,
                line_total_cents=total,
                price_changed_from_cents=(
                    line.seen_price_cents if line.seen_price_cents != offer.price_cents else None
                ),
            )
        )
        if status == "ok":
            subtotal += total
            items += line.quantity
            if offer.availability == "made_to_order" and offer.lead_time_max_days is not None:
                lead_time = max(lead_time or 0, offer.lead_time_max_days)
    return CartView(
        lines=lines,
        subtotal_cents=subtotal,
        item_count=items,
        has_problems=any(line.status != "ok" for line in lines),
        lead_time_max_days=lead_time,
    )
