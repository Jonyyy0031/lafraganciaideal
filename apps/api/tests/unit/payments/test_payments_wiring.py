"""The real container resolves the payments service, and its gateway follows the settings."""

import pytest

from fragancia_api.container import MODULES, build_container
from fragancia_api.modules.payments.application.commands.start_checkout import StartCheckout
from fragancia_api.modules.payments.infrastructure.mercadopago_gateway import MercadoPagoGateway
from fragancia_api.modules.payments.module import module as payments_module
from tests.support import make_settings


def test_payments_is_registered_after_orders() -> None:
    names = [m.name for m in MODULES]

    assert "payments" in names
    assert names.index("payments") > names.index("orders")
    assert payments_module in MODULES


async def test_the_container_resolves_start_checkout() -> None:
    container = build_container(make_settings())

    assert isinstance(container.services.get(StartCheckout), StartCheckout)
    await container.close()


async def test_the_gateway_takes_its_settings_and_the_back_url_the_storefront() -> None:
    container = build_container(
        make_settings(
            mercadopago_access_token="TEST-token",
            mercadopago_api_url="http://127.0.0.1:9/",
            mercadopago_sandbox=False,
            storefront_url="https://shop.test",
        )
    )

    use_case = container.services.get(StartCheckout)
    gateway = use_case._gateway  # noqa: SLF001
    assert isinstance(gateway, MercadoPagoGateway)
    assert gateway._api_url == "http://127.0.0.1:9"  # noqa: SLF001
    assert gateway._access_token == "TEST-token"  # noqa: SLF001
    assert gateway._sandbox is False  # noqa: SLF001
    assert use_case._back_url == "https://shop.test/pedido/pago"  # noqa: SLF001
    await container.close()


@pytest.mark.parametrize("token", [None])
async def test_without_a_token_the_container_still_builds_outside_production(
    token: str | None,
) -> None:
    container = build_container(make_settings(mercadopago_access_token=token))

    gateway = container.services.get(StartCheckout)._gateway  # noqa: SLF001
    assert isinstance(gateway, MercadoPagoGateway)
    assert gateway._access_token is None  # noqa: SLF001
    await container.close()
