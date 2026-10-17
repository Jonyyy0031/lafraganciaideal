from dataclasses import dataclass
from uuid import UUID

from fragancia_api.modules.orders.application.ports import (
    CartReader,
    CartTokens,
    CatalogOffers,
    DeliverySettingsReader,
    OrderLinks,
    OrderReader,
    StockHolds,
)
from fragancia_api.modules.orders.contracts import (
    CheckoutAddress,
    CheckoutContact,
    CheckoutInvoice,
    CheckoutRequest,
    PlacedOrder,
)
from fragancia_api.modules.orders.domain.cart import Cart, Offer, cart_view
from fragancia_api.modules.orders.domain.customer import (
    Address,
    CustomerEmail,
    CustomerName,
    Invoice,
    Phone,
)
from fragancia_api.modules.orders.domain.delivery import (
    DeliveryMethod,
    PostalCode,
    Subtotal,
    quote,
)
from fragancia_api.modules.orders.domain.errors import (
    CheckoutCartChanged,
    CheckoutCartEmpty,
    CheckoutCartHasProblems,
    CheckoutDeliveryUnavailable,
    CheckoutTotalChanged,
    CustomerDataInvalid,
)
from fragancia_api.modules.orders.domain.order import (
    PAYMENT_WINDOW,
    Delivery,
    Order,
    OrderLine,
    format_order_number,
)
from fragancia_api.modules.orders.domain.repositories import CartRepository, OrderRepository
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.events import EventPublisher
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result, new_id


@dataclass(frozen=True, slots=True)
class _Customer:
    name: CustomerName
    email: CustomerEmail
    phone: Phone
    address: Address | None
    invoice: Invoice | None


def _validate_contact(
    contact: CheckoutContact,
) -> Result[tuple[CustomerName, CustomerEmail, Phone], CustomerDataInvalid]:
    match CustomerName.create(contact.name):
        case Err(error):
            return Err(error)
        case Ok(name):
            pass
    match CustomerEmail.create(contact.email):
        case Err(error):
            return Err(error)
        case Ok(email):
            pass
    match Phone.create(contact.phone):
        case Err(error):
            return Err(error)
        case Ok(phone):
            return Ok((name, email, phone))


def _validate_address(
    method: DeliveryMethod, raw: CheckoutAddress | None
) -> Result[Address | None, CustomerDataInvalid]:
    address: Address | None = None
    if raw is not None:
        match Address.create(
            street=raw.street,
            exterior_number=raw.exterior_number,
            interior_number=raw.interior_number,
            neighborhood=raw.neighborhood,
            postal_code=raw.postal_code,
            municipality=raw.municipality,
            state=raw.state,
            references=raw.references,
        ):
            case Err(error):
                return Err(error)
            case Ok(built):
                address = built
    # The rule "address required for home and local, forbidden for pickup" lives in
    # `Delivery.create`. The cost and the pickup point are not known yet, so it is checked here
    # with placeholders to keep the order of the first error: contact, address, invoice.
    match Delivery.create(method, 0, address, None):
        case Err(error):
            return Err(error)
        case Ok(_):
            return Ok(address)


def _validate_invoice(raw: CheckoutInvoice | None) -> Result[Invoice | None, CustomerDataInvalid]:
    if raw is None:
        return Ok(None)
    return Invoice.create(
        rfc=raw.rfc,
        business_name=raw.business_name,
        tax_regime=raw.tax_regime,
        cfdi_use=raw.cfdi_use,
        postal_code=raw.postal_code,
    )


def _validate(request: CheckoutRequest) -> Result[_Customer, CustomerDataInvalid]:
    match _validate_contact(request.contact):
        case Err(error):
            return Err(error)
        case Ok((name, email, phone)):
            pass
    match _validate_address(DeliveryMethod(request.delivery_method), request.address):
        case Err(error):
            return Err(error)
        case Ok(address):
            pass
    match _validate_invoice(request.invoice):
        case Err(error):
            return Err(error)
        case Ok(invoice):
            return Ok(_Customer(name, email, phone, address, invoice))


def _pairs(cart: Cart) -> set[tuple[UUID, int]]:
    return {(line.presentation_id, line.quantity) for line in cart.lines}


class PlaceOrder:
    """Command: turn the cart into an order (decisions 1, 3, 4, 8, 11, 13, 14, 16, 18, 20, 22,
    24 to 30). Validates the guest's data, refuses a cart with problems or a total that changed,
    reserves the in-stock units for 30 minutes (made-to-order lines reserve nothing), takes the
    next gapless number, stores the order with frozen prices, empties the cart and publishes
    `orders.order.placed`. The order stays `pending_payment`.

    Offers and delivery settings are read BEFORE the transaction: the adapters take their own
    pooled connections, and doing that while the cart's row lock holds another one could starve
    the pool. Lock order inside: the cart row, inventory's own order, then the number counter.
    """

    def __init__(
        self,
        *,
        carts: CartRepository,
        cart_reader: CartReader,
        cart_tokens: CartTokens,
        links: OrderLinks,
        order_reader: OrderReader,
        offers: CatalogOffers,
        delivery_settings: DeliverySettingsReader,
        orders: OrderRepository,
        stocks: StockHolds,
        transactions: TransactionRunner,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self._carts = carts
        self._cart_reader = cart_reader
        self._cart_tokens = cart_tokens
        self._links = links
        self._order_reader = order_reader
        self._offers = offers
        self._delivery_settings = delivery_settings
        self._orders = orders
        self._stocks = stocks
        self._transactions = transactions
        self._events = events
        self._clock = clock

    async def execute(
        self, token: str | None, request: CheckoutRequest
    ) -> Result[PlacedOrder, DomainError]:
        now = self._clock.now()
        digest = self._cart_tokens.digest(token) if token is not None else None
        cart = await self._cart_reader.by_token(digest) if digest is not None else None

        # A retry after a lost response (decision 31): the cart is gone because the order took
        # it, so answer that order again. The request body is ignored.
        if digest is not None and (cart is None or not cart.lines):
            placed = await self._order_reader.placed_from_cart(digest, now - PAYMENT_WINDOW)
            if placed is not None:
                return Ok(self._placed_order(placed))

        match _validate(request):
            case Err(invalid):
                return Err(invalid)
            case Ok(customer):
                pass

        if digest is None or cart is None or not cart.lines:
            return Err(CheckoutCartEmpty())
        cart_digest: str = digest

        offers = await self._offers.offers([line.presentation_id for line in cart.lines], now)
        view = cart_view(cart, offers, now)
        if view.has_problems:
            return Err(
                CheckoutCartHasProblems(
                    details={
                        "lines": [
                            {"presentation_id": str(line.presentation_id), "status": line.status}
                            for line in view.lines
                            if line.status != "ok"
                        ]
                    }
                )
            )

        settings = await self._delivery_settings.current()
        match Subtotal.create(view.subtotal_cents):
            case Err(invalid_subtotal):
                return Err(invalid_subtotal)
            case Ok(subtotal):
                pass
        method = DeliveryMethod(request.delivery_method)
        postal_code: PostalCode | None = (
            customer.address.postal_code if customer.address is not None else None
        )
        chosen = next(
            (q for q in quote(settings, postal_code, subtotal) if q.method == method), None
        )
        if chosen is None:
            return Err(CheckoutDeliveryUnavailable(details={"method": method.value}))
        delivery_cents = chosen.cost.cents
        total = view.subtotal_cents + delivery_cents
        if total != request.expected_total_cents:
            return Err(
                CheckoutTotalChanged(
                    details={
                        "subtotal_cents": view.subtotal_cents,
                        "delivery_cents": delivery_cents,
                        "total_cents": total,
                    }
                )
            )

        order_id = new_id()
        lines = [_order_line(line.presentation_id, line.quantity, offers) for line in cart.lines]
        in_stock = {
            line.presentation_id: line.quantity for line in lines if line.availability == "in_stock"
        }
        pickup = settings.pickup if method == DeliveryMethod.PICKUP else None
        match Delivery.create(method, delivery_cents, customer.address, pickup):
            case Err(invalid_delivery):
                return Err(invalid_delivery)
            case Ok(delivery):
                pass
        payment_due_at = now + PAYMENT_WINDOW

        async def work() -> Result[Order, DomainError]:
            locked = await self._carts.get_by_token_for_update(cart_digest)
            if locked is None or _pairs(locked) != _pairs(cart):
                return Err(CheckoutCartChanged())
            reservation_id: UUID | None = None
            if in_stock:
                match await self._stocks.hold(f"order:{order_id}", in_stock, payment_due_at):
                    case Err(refusal):
                        return Err(refusal)
                    case Ok(held):
                        reservation_id = held
            number = await self._orders.next_number()
            order = Order.place(
                id=order_id,
                number=number,
                access_token_digest=self._links.digest(self._links.token(order_id)),
                cart_token_digest=cart_digest,
                name=customer.name,
                email=customer.email,
                phone=customer.phone,
                delivery=delivery,
                invoice=customer.invoice,
                lines=lines,
                reservation_id=reservation_id,
                payment_due_at=payment_due_at,
                now=now,
            )
            await self._orders.add(order)
            await self._carts.delete(locked)
            await self._events.publish(order.pull_events())
            return Ok(order)

        match await self._transactions.run(work):
            case Err(error):
                return Err(error)
            case Ok(order):
                return Ok(self._placed_order(order))

    def _placed_order(self, order: Order) -> PlacedOrder:
        return PlacedOrder(
            number=format_order_number(order.number),
            access_token=self._links.token(order.id),
            status=order.status.value,
            subtotal_cents=order.subtotal_cents,
            delivery_cents=order.delivery.cost_cents,
            total_cents=order.total_cents,
            payment_due_at=order.payment_due_at,
            lead_time_max_days=order.lead_time_max_days,
        )


def _order_line(presentation_id: UUID, quantity: int, offers: dict[UUID, Offer]) -> OrderLine:
    offer = offers[presentation_id]
    return OrderLine.create(
        presentation_id=presentation_id,
        perfume_id=offer.perfume_id,
        label=offer.label,
        ml=offer.ml,
        availability=offer.availability,
        lead_time_min_days=offer.lead_time_min_days,
        lead_time_max_days=offer.lead_time_max_days,
        quantity=quantity,
        unit_price_cents=offer.price_cents,
        regular_price_cents=offer.regular_price_cents,
    )
