"""Wiring of the orders module for the composition root."""

from fragancia_api.modules.catalog import PresentationOffers
from fragancia_api.modules.identity import OwnerDirectory
from fragancia_api.modules.inventory import StockReservations
from fragancia_api.modules.orders.application.commands.cart import (
    AcknowledgeCartPrices,
    PurgeCarts,
    RemoveCartLine,
    SetCartLine,
)
from fragancia_api.modules.orders.application.commands.checkout import PlaceOrder
from fragancia_api.modules.orders.application.commands.confirm_payment import (
    OrderPaymentConfirmations,
)
from fragancia_api.modules.orders.application.commands.customer_order import (
    CancelOrderByCustomer,
    LookUpOrder,
)
from fragancia_api.modules.orders.application.commands.delivery import UpdateDeliveryOptions
from fragancia_api.modules.orders.application.commands.lifecycle import (
    AddOrderNote,
    AdvanceOrder,
    CancelOverdueOrders,
    CancelPaidOrder,
    CancelUnpaidOrder,
    MarkOrderPaid,
    MarkOrderRefunded,
    OrderMove,
)
from fragancia_api.modules.orders.application.handlers.order_emails import (
    SendOrderPlacedEmail,
    SendOrderStatusEmail,
    SendPaidOrderToOwners,
)
from fragancia_api.modules.orders.application.queries.cart import GetCart
from fragancia_api.modules.orders.application.queries.customer_order import GetCustomerOrder
from fragancia_api.modules.orders.application.queries.delivery import (
    GetDeliveryOptions,
    GetPublicDeliveryOptions,
    QuoteDelivery,
)
from fragancia_api.modules.orders.application.queries.order_payments import OrderPayments
from fragancia_api.modules.orders.application.queries.orders import GetOrder, ListOrders
from fragancia_api.modules.orders.domain.cart import CART_LIFETIME
from fragancia_api.modules.orders.http.cart_cookie import CartCookie
from fragancia_api.modules.orders.http.router import routers
from fragancia_api.modules.orders.infrastructure.cart_tokens import SecureCartTokens
from fragancia_api.modules.orders.infrastructure.catalog_offers import CatalogOffersAdapter
from fragancia_api.modules.orders.infrastructure.identity_owners import IdentityOwners
from fragancia_api.modules.orders.infrastructure.order_links import HmacOrderLinks
from fragancia_api.modules.orders.infrastructure.sql_cart_repository import (
    SqlCartReader,
    SqlCartRepository,
)
from fragancia_api.modules.orders.infrastructure.sql_customer_orders import SqlCustomerOrders
from fragancia_api.modules.orders.infrastructure.sql_delivery_settings_repository import (
    SqlDeliverySettingsReader,
    SqlDeliverySettingsRepository,
)
from fragancia_api.modules.orders.infrastructure.sql_lookup_throttle import SqlLookupThrottle
from fragancia_api.modules.orders.infrastructure.sql_order_queries import SqlOrderQueries
from fragancia_api.modules.orders.infrastructure.sql_order_repository import (
    SqlOrderReader,
    SqlOrderRepository,
)
from fragancia_api.modules.orders.infrastructure.stock_holds import StockHoldsAdapter
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.module import AppModule, Platform, ScheduledJob

# Development and test only: production requires ORDER_LINK_SECRET (see `Settings`).
_DEV_ORDER_LINK_SECRET = "dev-order-link-secret-not-for-production"  # noqa: S105


def register(platform: Platform, services: ServiceRegistry) -> None:
    repository = SqlDeliverySettingsRepository(platform.database)
    reader = SqlDeliverySettingsReader(platform.database)

    services.add(
        UpdateDeliveryOptions,
        UpdateDeliveryOptions(repository=repository, transactions=platform.transactions),
    )
    services.add(GetDeliveryOptions, GetDeliveryOptions(reader))
    services.add(GetPublicDeliveryOptions, GetPublicDeliveryOptions(reader))
    services.add(QuoteDelivery, QuoteDelivery(reader))

    carts = SqlCartRepository(platform.database)
    cart_reader = SqlCartReader(platform.database)
    tokens = SecureCartTokens()
    secret = platform.settings.order_link_secret
    links = HmacOrderLinks(
        secret.get_secret_value()
        if secret is not None and secret.get_secret_value()
        else _DEV_ORDER_LINK_SECRET
    )
    catalog = CatalogOffersAdapter(services.get(PresentationOffers))
    services.add(
        SetCartLine,
        SetCartLine(
            carts=carts,
            tokens=tokens,
            offers=catalog,
            transactions=platform.transactions,
            clock=platform.clock,
        ),
    )
    services.add(
        RemoveCartLine,
        RemoveCartLine(
            carts=carts,
            tokens=tokens,
            offers=catalog,
            transactions=platform.transactions,
            clock=platform.clock,
        ),
    )
    services.add(
        AcknowledgeCartPrices,
        AcknowledgeCartPrices(
            carts=carts,
            tokens=tokens,
            offers=catalog,
            transactions=platform.transactions,
            clock=platform.clock,
        ),
    )
    services.add(
        GetCart,
        GetCart(reader=cart_reader, tokens=tokens, offers=catalog, clock=platform.clock),
    )
    services.add(
        PurgeCarts,
        PurgeCarts(carts=carts, transactions=platform.transactions, clock=platform.clock),
    )
    services.add(
        PlaceOrder,
        PlaceOrder(
            carts=carts,
            cart_reader=cart_reader,
            cart_tokens=tokens,
            links=links,
            order_reader=SqlOrderReader(platform.database),
            offers=catalog,
            delivery_settings=reader,
            orders=SqlOrderRepository(platform.database),
            stocks=StockHoldsAdapter(services.get(StockReservations)),
            transactions=platform.transactions,
            events=platform.events,
            clock=platform.clock,
        ),
    )
    order_repository = SqlOrderRepository(platform.database)
    stocks = StockHoldsAdapter(services.get(StockReservations))

    def move[C: OrderMove](command: type[C]) -> C:
        return command(
            orders=order_repository,
            stocks=stocks,
            transactions=platform.transactions,
            events=platform.events,
            clock=platform.clock,
        )

    services.add(MarkOrderPaid, move(MarkOrderPaid))
    services.add(AdvanceOrder, move(AdvanceOrder))
    services.add(CancelUnpaidOrder, move(CancelUnpaidOrder))
    services.add(CancelPaidOrder, move(CancelPaidOrder))
    services.add(MarkOrderRefunded, move(MarkOrderRefunded))
    services.add(CancelOverdueOrders, move(CancelOverdueOrders))
    services.add(OrderPaymentConfirmations, move(OrderPaymentConfirmations))
    services.add(
        AddOrderNote,
        AddOrderNote(
            orders=order_repository, transactions=platform.transactions, clock=platform.clock
        ),
    )
    order_queries = SqlOrderQueries(platform.database)
    services.add(ListOrders, ListOrders(order_queries))
    services.add(GetOrder, GetOrder(order_queries))
    customer_queries = SqlCustomerOrders(platform.database)
    services.add(GetCustomerOrder, GetCustomerOrder(queries=customer_queries, links=links))
    services.add(OrderPayments, OrderPayments(queries=customer_queries, links=links))
    services.add(
        LookUpOrder,
        LookUpOrder(
            queries=customer_queries,
            links=links,
            throttle=SqlLookupThrottle(platform.database),
            transactions=platform.transactions,
            clock=platform.clock,
        ),
    )
    services.add(
        CancelOrderByCustomer,
        CancelOrderByCustomer(
            queries=customer_queries, links=links, cancel=services.get(CancelUnpaidOrder)
        ),
    )
    # Identity registers before orders (MODULES order), so its facade is available.
    owners = IdentityOwners(services.get(OwnerDirectory))
    storefront_url = platform.settings.storefront_url
    platform.subscriptions.subscribe(
        "orders.order.placed",
        SendOrderPlacedEmail(
            queries=order_queries,
            links=links,
            email=platform.email,
            storefront_url=storefront_url,
        ),
    )
    platform.subscriptions.subscribe(
        "orders.order.status_changed",
        SendOrderStatusEmail(
            queries=order_queries,
            links=links,
            email=platform.email,
            storefront_url=storefront_url,
        ),
    )
    platform.subscriptions.subscribe(
        "orders.order.paid",
        SendPaidOrderToOwners(
            queries=order_queries,
            owners=owners,
            email=platform.email,
            admin_web_url=platform.settings.admin_web_url,
        ),
    )
    services.add(
        CartCookie,
        CartCookie(
            secure=platform.settings.is_production,
            max_age_seconds=int(CART_LIFETIME.total_seconds()),
        ),
    )


module = AppModule(
    name="orders",
    register=register,
    routers=routers,
    jobs=(
        ScheduledJob("orders_purge_carts", lambda s: s.get(PurgeCarts).run_once(), 60, 120),
        ScheduledJob(
            "orders_cancel_overdue", lambda s: s.get(CancelOverdueOrders).run_once(), 60, 120
        ),
    ),
)
