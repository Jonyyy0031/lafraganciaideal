"""Wiring of the orders module for the composition root."""

from fragancia_api.modules.catalog import PresentationOffers
from fragancia_api.modules.orders.application.commands.cart import (
    AcknowledgeCartPrices,
    PurgeCarts,
    RemoveCartLine,
    SetCartLine,
)
from fragancia_api.modules.orders.application.commands.delivery import UpdateDeliveryOptions
from fragancia_api.modules.orders.application.queries.cart import GetCart
from fragancia_api.modules.orders.application.queries.delivery import (
    GetDeliveryOptions,
    GetPublicDeliveryOptions,
    QuoteDelivery,
)
from fragancia_api.modules.orders.domain.cart import CART_LIFETIME
from fragancia_api.modules.orders.http.cart_cookie import CartCookie
from fragancia_api.modules.orders.http.router import routers
from fragancia_api.modules.orders.infrastructure.cart_tokens import SecureCartTokens
from fragancia_api.modules.orders.infrastructure.catalog_offers import CatalogOffersAdapter
from fragancia_api.modules.orders.infrastructure.sql_cart_repository import (
    SqlCartReader,
    SqlCartRepository,
)
from fragancia_api.modules.orders.infrastructure.sql_delivery_settings_repository import (
    SqlDeliverySettingsReader,
    SqlDeliverySettingsRepository,
)
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.module import AppModule, Platform, ScheduledJob


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
    jobs=(ScheduledJob("orders_purge_carts", lambda s: s.get(PurgeCarts).run_once(), 60, 120),),
)
