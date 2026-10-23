"""Wiring of the payments module for the composition root."""

from fragancia_api.modules.orders import OrderPayments
from fragancia_api.modules.payments.application.commands.start_checkout import StartCheckout
from fragancia_api.modules.payments.http.router import routers
from fragancia_api.modules.payments.infrastructure.mercadopago_gateway import MercadoPagoGateway
from fragancia_api.modules.payments.infrastructure.order_payments import OrderPaymentsAdapter
from fragancia_api.modules.payments.infrastructure.sql_checkout_repository import (
    SqlCheckoutRepository,
)
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.module import AppModule, Platform


def register(platform: Platform, services: ServiceRegistry) -> None:
    settings = platform.settings
    token = settings.mercadopago_access_token
    gateway = MercadoPagoGateway(
        api_url=settings.mercadopago_api_url,
        access_token=token.get_secret_value() if token is not None else None,
        sandbox=settings.mercadopago_sandbox,
    )
    # Orders registers before payments (MODULES order), so its facade is available.
    services.add(
        StartCheckout,
        StartCheckout(
            orders=OrderPaymentsAdapter(services.get(OrderPayments)),
            checkouts=SqlCheckoutRepository(platform.database),
            gateway=gateway,
            transactions=platform.transactions,
            clock=platform.clock,
            back_url=f"{settings.storefront_url}/pedido/pago",
        ),
    )


module = AppModule(name="payments", register=register, routers=routers)
