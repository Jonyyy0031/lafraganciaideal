"""Plan 003 steps 2 and 6: the Mercado Pago adapter's ticket fields in `get_payment` (payment
method, `date_of_expiration`, `transaction_details.external_resource_url`) and `close_checkout`
(`PUT /checkout/preferences/{id}`), against `httpx.MockTransport`; and `StartCheckout` giving
the gateway a cash expiry 48 hours after the link was created."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from fragancia_api.modules.payments.application.commands.start_checkout import CASH_TICKET_TTL
from fragancia_api.modules.payments.domain.errors import PaymentGatewayUnavailable
from fragancia_api.modules.payments.infrastructure.mercadopago_gateway import TIMEOUT_SECONDS
from fragancia_api.shared.kernel import Err, Ok
from tests.unit.payments.support import NOW, TOKEN, World, make_payable
from tests.unit.payments.test_mercadopago_gateway import (
    ACCESS_TOKEN,
    API,
    Recorder,
    answering,
    body_of,
    gateway_for,
    raising,
)
from tests.unit.payments.test_mercadopago_get_payment import PAID

OXXO = {
    "id": 777,
    "status": "pending",
    "status_detail": "pending_waiting_payment",
    "external_reference": "00000000-0000-0000-0000-000000000384",
    "transaction_amount": 2680.0,
    "currency_id": "MXN",
    "payment_type_id": "ticket",
    "payment_method_id": "oxxo",
    "date_of_expiration": "2026-10-29T12:00:00.000-06:00",
    "transaction_details": {"external_resource_url": "https://mp.test/ticket/777"},
}
AT = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)


async def fetched(body: dict[str, Any]):  # type: ignore[no-untyped-def]
    result = await gateway_for(answering(200, body)).get_payment("777")
    assert isinstance(result, Ok), result
    assert result.value is not None
    return result.value


# --- get_payment: the ticket fields ------------------------------------------------------------


async def test_a_pending_oxxo_ticket_maps_its_method_expiry_and_page() -> None:
    payment = await fetched(OXXO)

    assert payment.payment_type == "ticket"
    assert payment.payment_method == "oxxo"
    assert payment.expires_at == datetime(2026, 10, 29, 12, 0, tzinfo=timezone(timedelta(hours=-6)))
    assert payment.expires_at == datetime(2026, 10, 29, 18, 0, tzinfo=UTC)
    assert payment.ticket_url == "https://mp.test/ticket/777"
    assert payment.status == "pending"


async def test_a_spei_transfer_maps_the_same_fields() -> None:
    payment = await fetched(
        {**OXXO, "payment_type_id": "bank_transfer", "payment_method_id": "clabe"}
    )

    assert (payment.payment_type, payment.payment_method) == ("bank_transfer", "clabe")


async def test_a_card_payment_has_no_ticket_fields() -> None:
    payment = await fetched(PAID)

    assert payment.payment_method is None
    assert payment.expires_at is None
    assert payment.ticket_url is None


async def test_a_card_payment_reports_its_method_when_mercado_pago_sends_it() -> None:
    payment = await fetched({**PAID, "payment_method_id": "visa"})

    assert payment.payment_method == "visa"


@pytest.mark.parametrize("value", [None, "", 5, True, [], {}, "tomorrow", "2026-10-29T12:00:00"])
async def test_an_expiry_that_is_missing_malformed_or_without_an_offset_is_none(
    value: object,
) -> None:
    payment = await fetched({**OXXO, "date_of_expiration": value})

    assert payment.expires_at is None
    assert payment.payment_method == "oxxo"


@pytest.mark.parametrize("value", [None, "", 5, ["x"], {"a": 1}])
async def test_a_method_of_the_wrong_type_or_empty_is_none(value: object) -> None:
    payment = await fetched({**OXXO, "payment_method_id": value})

    assert payment.payment_method is None


@pytest.mark.parametrize(
    "details",
    [None, "x", 5, [], {}, {"external_resource_url": None}, {"external_resource_url": ""}],
)
async def test_a_page_that_is_missing_or_of_the_wrong_type_is_none(details: object) -> None:
    payment = await fetched({**OXXO, "transaction_details": details})

    assert payment.ticket_url is None


async def test_a_bad_ticket_field_never_makes_the_payment_unavailable() -> None:
    result = await gateway_for(
        answering(
            200,
            {**OXXO, "payment_method_id": 1, "date_of_expiration": 2, "transaction_details": 3},
        )
    ).get_payment("777")

    assert isinstance(result, Ok)
    assert result.value is not None
    assert result.value.status == "pending"


# --- close_checkout ----------------------------------------------------------------------------


async def test_close_puts_the_expiry_to_the_preference_with_a_bearer_token() -> None:
    recorder = answering(200, {"id": "pref-1"})

    result = await gateway_for(recorder).close_checkout("pref-1", AT)

    assert result == Ok(True)
    sent = recorder.sent
    assert sent.method == "PUT"
    assert str(sent.url) == f"{API}/checkout/preferences/pref-1"
    assert sent.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert sent.headers["Content-Type"] == "application/json"
    # Plan 003 step 10 (V1): no `date_of_expiration`, Mercado Pago refuses it set to now.
    assert body_of(sent) == {
        "expires": True,
        "expiration_date_to": "2026-10-27T15:00:00.000+00:00",
    }
    assert sent.extensions["timeout"] == {
        "connect": TIMEOUT_SECONDS,
        "read": TIMEOUT_SECONDS,
        "write": TIMEOUT_SECONDS,
        "pool": TIMEOUT_SECONDS,
    }


async def test_close_keeps_the_offset_of_the_given_instant() -> None:
    recorder = answering(200, {})
    at = datetime(2026, 10, 27, 9, 0, tzinfo=timezone(timedelta(hours=-6)))

    await gateway_for(recorder).close_checkout("pref-1", at)

    assert body_of(recorder.sent)["expiration_date_to"] == "2026-10-27T09:00:00.000-06:00"


async def test_close_accepts_any_2xx() -> None:
    assert await gateway_for(answering(204, None)).close_checkout("p", AT) == Ok(True)


async def test_close_of_a_preference_mercado_pago_does_not_know_is_ok_false() -> None:
    recorder = answering(404, {"message": "not found"})

    assert await gateway_for(recorder).close_checkout("gone", AT) == Ok(False)


@pytest.mark.parametrize("status", [401, 403, 408, 429, 500, 502, 503])
async def test_close_with_another_failing_status_is_unavailable_and_logs_the_status_only(
    status: int,
) -> None:
    with capture_logs() as logs:
        result = await gateway_for(answering(status, {"secret": ACCESS_TOKEN})).close_checkout(
            "p", AT
        )

    assert result == Err(PaymentGatewayUnavailable())
    [log] = logs
    assert (log["event"], log["status"]) == ("payments.gateway_error", status)
    assert ACCESS_TOKEN not in str(logs)


@pytest.mark.parametrize("error", [httpx.ConnectError("boom"), httpx.ReadTimeout("slow")])
async def test_close_with_a_transport_error_is_unavailable_and_logs_its_class(
    error: Exception,
) -> None:
    with capture_logs() as logs:
        result = await gateway_for(raising(error)).close_checkout("p", AT)

    assert result == Err(PaymentGatewayUnavailable())
    [log] = logs
    assert (log["event"], log["status"], log["error"]) == (
        "payments.gateway_error",
        None,
        type(error).__name__,
    )


@pytest.mark.parametrize("token", [None, ""])
async def test_close_without_an_access_token_sends_nothing_and_logs_once(token: str | None) -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json={}))

    with capture_logs() as logs:
        result = await gateway_for(recorder, token=token).close_checkout("p", AT)

    assert result == Err(PaymentGatewayUnavailable())
    assert recorder.requests == []
    assert [entry["event"] for entry in logs] == ["payments.gateway_not_configured"]


async def test_close_uses_the_configured_api_url_without_doubling_the_slash() -> None:
    recorder = answering(200, {})

    await gateway_for(recorder, api_url=f"{API}/").close_checkout("pref-9", AT)

    assert str(recorder.sent.url) == f"{API}/checkout/preferences/pref-9"


# --- what only Mercado Pago can confirm --------------------------------------------------------


@pytest.mark.skip(
    reason="NOT CONFIRMED: that the sandbox checkout offers OXXO and SPEI for a preference with "
    "only `atm` excluded and `date_of_expiration` set, and that the ticket expires at that "
    "instant, needs the real Mercado Pago sandbox (plan 003 acceptance; verifier)"
)
def test_the_sandbox_offers_oxxo_and_spei_and_honors_the_cash_expiry() -> None:
    raise AssertionError("not runnable locally")


@pytest.mark.skip(
    reason="NOT CONFIRMED: that `PUT /checkout/preferences/{id}` with `expires` makes the old "
    "link refuse payments and tickets needs the real Mercado Pago sandbox (plan 003 step 6; "
    "verifier)"
)
def test_a_closed_preference_no_longer_accepts_a_payment() -> None:
    raise AssertionError("not runnable locally")


# --- StartCheckout: the cash expiry ------------------------------------------------------------


def test_the_cash_ticket_ttl_is_forty_eight_hours() -> None:
    assert CASH_TICKET_TTL == timedelta(hours=48)


async def test_an_order_held_for_a_ticket_gets_a_new_link_for_its_extended_window() -> None:
    world = World()
    world.put(make_payable(due=timedelta(minutes=30)))
    await world.start_checkout().execute(TOKEN)
    # A cash ticket moved the deadline 47 hours on: the stored link no longer matches it.
    held = make_payable(due=timedelta(hours=47))
    world.put(held)

    url = await world.start_checkout().execute(TOKEN)

    assert isinstance(url, Ok)
    assert len(world.gateway.requests) == 2
    assert world.gateway.requests[1].expires_at == held.payment_due_at
    assert world.checkouts.by_order[held.order_id].expires_at == held.payment_due_at


async def test_a_new_link_asks_for_cash_tickets_expiring_48_hours_after_it_is_created() -> None:
    world = World()
    world.put(make_payable())

    await world.start_checkout().execute(TOKEN)

    [request] = world.gateway.requests
    assert request.cash_expires_at == NOW + timedelta(hours=48)
    assert request.cash_expires_at > request.expires_at
