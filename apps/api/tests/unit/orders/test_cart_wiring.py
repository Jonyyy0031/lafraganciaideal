"""The real container resolves the cart services; the token, cookie and catalog adapters."""

from datetime import timedelta
from uuid import UUID

import pytest

from fragancia_api.container import MODULES, build_container
from fragancia_api.modules.catalog import PresentationOffer, PresentationOffers
from fragancia_api.modules.orders import module as orders_module
from fragancia_api.modules.orders.application.commands.cart import (
    AcknowledgeCartPrices,
    PurgeCarts,
    RemoveCartLine,
    SetCartLine,
)
from fragancia_api.modules.orders.application.commands.checkout import PlaceOrder
from fragancia_api.modules.orders.application.queries.cart import GetCart
from fragancia_api.modules.orders.domain.cart import CART_LIFETIME, Cart
from fragancia_api.modules.orders.http.cart_cookie import CartCookie
from fragancia_api.modules.orders.infrastructure.cart_tokens import SecureCartTokens
from fragancia_api.modules.orders.infrastructure.catalog_offers import CatalogOffersAdapter
from fragancia_api.modules.orders.infrastructure.in_memory import InMemoryCarts
from fragancia_api.modules.orders.infrastructure.order_links import HmacOrderLinks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from tests.support import make_settings
from tests.unit.orders.support import NOW


@pytest.mark.parametrize(
    "service", [SetCartLine, RemoveCartLine, AcknowledgeCartPrices, GetCart, PurgeCarts, CartCookie]
)
async def test_the_container_resolves_every_cart_service(service: type) -> None:
    container = build_container(make_settings())

    assert isinstance(container.services.get(service), service)
    await container.close()


@pytest.mark.parametrize(("environment", "secure"), [("production", True), ("development", False)])
async def test_the_registered_cookie_is_secure_only_in_production(
    environment: str, secure: bool
) -> None:
    container = build_container(make_settings(app_env=environment, order_link_secret="x" * 32))

    cookie = container.services.get(CartCookie)

    assert cookie.secure is secure
    assert cookie.max_age_seconds == int(CART_LIFETIME.total_seconds())
    await container.close()


# --- the order-link secret reaches `HmacOrderLinks` through the wiring (decision 31) -------------

ORDER_ID = UUID(int=7)
CONFIGURED_SECRET = "configured-order-link-secret-0123456789"
DEV_LINKS = HmacOrderLinks(orders_module._DEV_ORDER_LINK_SECRET)  # noqa: SLF001


async def wired_links(**settings: object) -> HmacOrderLinks:
    container = build_container(make_settings(**settings))
    try:
        links = container.services.get(PlaceOrder)._links  # noqa: SLF001
        assert isinstance(links, HmacOrderLinks)
        return links
    finally:
        await container.close()


@pytest.mark.parametrize("environment", ["development", "test", "production"])
async def test_the_configured_order_link_secret_is_what_the_links_are_derived_from(
    environment: str,
) -> None:
    links = await wired_links(app_env=environment, order_link_secret=CONFIGURED_SECRET)

    assert links.token(ORDER_ID) == HmacOrderLinks(CONFIGURED_SECRET).token(ORDER_ID)
    assert links.token(ORDER_ID) != DEV_LINKS.token(ORDER_ID)


@pytest.mark.parametrize("secret", [None, ""])
@pytest.mark.parametrize("environment", ["development", "test"])
async def test_without_a_secret_outside_production_the_development_value_is_used(
    environment: str, secret: str | None
) -> None:
    links = await wired_links(app_env=environment, order_link_secret=secret)

    assert links.token(ORDER_ID) == DEV_LINKS.token(ORDER_ID)


def test_orders_schedules_the_cart_purge_and_the_overdue_cancel_jobs() -> None:
    orders = next(module for module in MODULES if module.name == "orders")

    assert [job.name for job in orders.jobs] == ["orders_purge_carts", "orders_cancel_overdue"]


async def test_the_scheduled_job_runs_the_purge_and_returns_how_many_it_deleted() -> None:
    orders = next(module for module in MODULES if module.name == "orders")
    job = next(job for job in orders.jobs if job.name == "orders_purge_carts")
    carts = InMemoryCarts(
        Cart(
            id=UUID(int=1),
            token_digest="a" * 64,
            lines=[],
            created_at=NOW - timedelta(days=40),
            updated_at=NOW - timedelta(days=40),
        )
    )
    services = ServiceRegistry()
    services.add(
        PurgeCarts,
        PurgeCarts(carts=carts, transactions=InMemoryTransactionRunner(), clock=FixedClock()),
    )

    assert (job.every_seconds, job.timeout_seconds) == (60, 120)
    assert await job.run(services) == 1
    assert carts.by_id == {}


# --- tokens -------------------------------------------------------------------------------------


def test_a_token_is_random_and_url_safe() -> None:
    tokens = SecureCartTokens()

    first, second = tokens.new(), tokens.new()

    assert first != second
    assert len(first) >= 43  # 32 random bytes, base64url without padding
    assert set(first) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_the_digest_is_the_sha256_hex_of_the_token_and_is_not_the_token() -> None:
    tokens = SecureCartTokens()

    digest = tokens.digest("abc")

    assert digest == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert len(tokens.digest(tokens.new())) == 64
    assert tokens.digest("abc") == digest
    assert tokens.digest("abd") != digest


# --- catalog adapter ----------------------------------------------------------------------------


class FakeOffersQueries:
    def __init__(self, *offers: PresentationOffer) -> None:
        self.by_id = {offer.presentation_id: offer for offer in offers}
        self.calls: list[tuple[list[UUID], object]] = []

    async def offers(self, ids, now):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN201
        self.calls.append((list(ids), now))
        return {i: self.by_id[i] for i in ids if i in self.by_id}


async def test_the_adapter_maps_every_field_of_the_catalog_offer() -> None:
    source = PresentationOffer(
        presentation_id=UUID(int=1),
        perfume_id=UUID(int=2),
        perfume_slug="versace-eros-edt",
        label="Versace Eros EDT 100 ml",
        ml=100,
        card_url="http://media.test/card.webp",
        price_cents=200_000,
        regular_price_cents=250_000,
        availability="made_to_order",
        lead_time_min_days=3,
        lead_time_max_days=5,
        is_visible=True,
        available_units=None,
    )
    queries = FakeOffersQueries(source)
    adapter = CatalogOffersAdapter(PresentationOffers(queries))

    found = await adapter.offers([UUID(int=1), UUID(int=9)], NOW)

    assert list(found) == [UUID(int=1)]
    offer = found[UUID(int=1)]
    for name in source.__dataclass_fields__:
        assert getattr(offer, name) == getattr(source, name), name
    assert queries.calls == [([UUID(int=1), UUID(int=9)], NOW)]


def test_the_cart_lifetime_is_thirty_days_in_seconds() -> None:
    assert CART_LIFETIME == timedelta(days=30)
    assert int(CART_LIFETIME.total_seconds()) == 2_592_000
