"""Wiring of the payments module for the composition root."""

from fragancia_api.modules.identity import OwnerDirectory
from fragancia_api.modules.orders import OrderPaymentConfirmations, OrderPayments
from fragancia_api.modules.payments.application.commands.receive_notification import (
    ReceivePaymentNotification,
)
from fragancia_api.modules.payments.application.commands.start_checkout import StartCheckout
from fragancia_api.modules.payments.application.handlers.cancel_ticket import CancelTicket
from fragancia_api.modules.payments.application.handlers.close_checkout import (
    CloseCheckout,
    RequestCheckoutClose,
)
from fragancia_api.modules.payments.application.handlers.flagged_payment_email import (
    SendFlaggedPaymentToOwners,
)
from fragancia_api.modules.payments.application.handlers.process_payment import (
    ProcessPaymentNotification,
)
from fragancia_api.modules.payments.application.queries.pending_tickets import GetPendingTickets
from fragancia_api.modules.payments.domain.events import (
    CheckoutCloseRequested,
    PaymentFlagged,
    PaymentNotificationReceived,
    TicketCancelRequested,
)
from fragancia_api.modules.payments.http.router import routers
from fragancia_api.modules.payments.infrastructure.identity_owners import IdentityOwners
from fragancia_api.modules.payments.infrastructure.mercadopago_gateway import MercadoPagoGateway
from fragancia_api.modules.payments.infrastructure.order_payments import OrderPaymentsAdapter
from fragancia_api.modules.payments.infrastructure.sql_checkout_repository import (
    SqlCheckoutRepository,
)
from fragancia_api.modules.payments.infrastructure.sql_payment_repository import (
    SqlPaymentQueries,
    SqlPaymentRepository,
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
    # Orders registers before payments (MODULES order), so its facades are available.
    orders = OrderPaymentsAdapter(
        services.get(OrderPayments), services.get(OrderPaymentConfirmations)
    )
    checkouts = SqlCheckoutRepository(platform.database)
    payments = SqlPaymentRepository(platform.database)
    services.add(
        StartCheckout,
        StartCheckout(
            orders=orders,
            checkouts=checkouts,
            gateway=gateway,
            transactions=platform.transactions,
            clock=platform.clock,
            back_url=f"{settings.storefront_url}/pedido/pago",
        ),
    )
    secret = settings.mercadopago_webhook_secret
    services.add(
        ReceivePaymentNotification,
        ReceivePaymentNotification(
            secret=secret.get_secret_value() if secret is not None else None,
            events=platform.events,
            transactions=platform.transactions,
        ),
    )
    platform.subscriptions.subscribe(
        PaymentNotificationReceived.name,
        ProcessPaymentNotification(
            gateway=gateway,
            payments=payments,
            orders=orders,
            transactions=platform.transactions,
            events=platform.events,
            clock=platform.clock,
        ),
    )
    services.add(
        GetPendingTickets,
        GetPendingTickets(
            orders=orders, queries=SqlPaymentQueries(platform.database), clock=platform.clock
        ),
    )
    # Decision 18: the status change only requests the close; the close (a Mercado Pago call)
    # has its own outbox row, so an outage never retries orders' emails.
    platform.subscriptions.subscribe(
        "orders.order.status_changed",
        RequestCheckoutClose(events=platform.events, transactions=platform.transactions),
    )
    platform.subscriptions.subscribe(
        CheckoutCloseRequested.name,
        CloseCheckout(
            gateway=gateway,
            checkouts=checkouts,
            payments=payments,
            transactions=platform.transactions,
            clock=platform.clock,
        ),
    )
    # Decision 20 for late notifications: a ticket that arrives for an order no longer pending
    # is cancelled in its own outbox row (plan 003 review R2).
    platform.subscriptions.subscribe(TicketCancelRequested.name, CancelTicket(gateway=gateway))
    # Identity registers before payments (MODULES order), so its facade is available.
    platform.subscriptions.subscribe(
        PaymentFlagged.name,
        SendFlaggedPaymentToOwners(
            owners=IdentityOwners(services.get(OwnerDirectory)), email=platform.email
        ),
    )


module = AppModule(name="payments", register=register, routers=routers)
