"""Plan 002 step 3, domain: `Payment` (refresh, settle once), `amount_to_cents`, the webhook
signature (payments/domain/webhook_signature.py) and the two events."""

import hashlib
import hmac
from dataclasses import FrozenInstanceError
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from fragancia_api.modules.payments.domain.errors import PaymentWebhookSignatureInvalid
from fragancia_api.modules.payments.domain.events import (
    PaymentFlagged,
    PaymentNotificationReceived,
)
from fragancia_api.modules.payments.domain.payment import (
    APPROVED,
    Payment,
    PaymentOutcome,
    amount_to_cents,
)
from fragancia_api.modules.payments.domain.webhook_signature import signature_valid
from fragancia_api.shared.kernel import UnauthenticatedError
from tests.unit.payments.support import NOW, SECRET, TS, sign

DATA_ID = "123456789"
REQUEST_ID = "req-abc-1"


def make_payment(**overrides: object) -> Payment:
    values: dict[str, object] = {
        "payment_id": DATA_ID,
        "order_id": UUID(int=900),
        "status": "pending",
        "status_detail": "pending_waiting_payment",
        "amount_cents": 100_000,
        "currency": "MXN",
        "payment_type": "credit_card",
        "approved_at": None,
        "outcome": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return Payment(**values)  # type: ignore[arg-type]


def v1_of(header: str) -> str:
    return header.split("v1=")[1]


# --- Payment -----------------------------------------------------------------------------------


def test_refresh_takes_the_reported_fields_and_the_time_only() -> None:
    payment = make_payment()
    later = NOW + timedelta(minutes=1)

    payment.refresh("approved", "accredited", later, later)

    assert (payment.status, payment.status_detail) == ("approved", "accredited")
    assert (payment.approved_at, payment.updated_at) == (later, later)
    assert (payment.amount_cents, payment.currency, payment.order_id) == (
        100_000,
        "MXN",
        UUID(int=900),
    )
    assert payment.created_at == NOW
    assert payment.outcome is None


def test_refresh_does_not_touch_an_outcome_already_set() -> None:
    payment = make_payment(outcome=PaymentOutcome.APPLIED)

    payment.refresh("refunded", None, None, NOW + timedelta(hours=1))

    assert payment.status == "refunded"
    assert payment.outcome is PaymentOutcome.APPLIED


@pytest.mark.parametrize("outcome", list(PaymentOutcome))
def test_settle_sets_the_outcome_and_the_time(outcome: PaymentOutcome) -> None:
    payment = make_payment()
    later = NOW + timedelta(seconds=5)

    payment.settle(outcome, later)

    assert payment.outcome is outcome
    assert payment.updated_at == later


def test_settle_twice_raises_and_keeps_the_first_outcome() -> None:
    payment = make_payment()
    payment.settle(PaymentOutcome.APPLIED, NOW)

    with pytest.raises(ValueError, match="already has an outcome"):
        payment.settle(PaymentOutcome.REFUND_DUE, NOW + timedelta(seconds=1))

    assert payment.outcome is PaymentOutcome.APPLIED
    assert payment.updated_at == NOW


def test_the_outcomes_and_the_approved_status_are_the_stored_words() -> None:
    assert [o.value for o in PaymentOutcome] == ["applied", "refund_due", "amount_mismatch"]
    assert APPROVED == "approved"


# --- amount_to_cents ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "cents"),
    [
        (0, 0),
        (1, 100),
        (2500, 250_000),
        (2680.0, 268_000),
        (10.05, 1005),
        (0.1, 10),
        (19.99, 1999),
        (1234.56, 123_456),
        (Decimal("0.005"), 1),
        (Decimal("0.015"), 2),
        (Decimal("0.004"), 0),
        (0.005, 1),
        (1.005, 101),
    ],
)
def test_amount_to_cents_rounds_half_up_through_the_decimal_text(
    value: Decimal | float | int, cents: int
) -> None:
    assert amount_to_cents(value) == cents


def test_amount_to_cents_returns_an_int() -> None:
    assert type(amount_to_cents(12.5)) is int


# --- webhook signature -------------------------------------------------------------------------


def test_a_signature_made_with_the_documented_manifest_is_valid() -> None:
    header = sign(DATA_ID, REQUEST_ID)

    assert signature_valid(SECRET, header, REQUEST_ID, DATA_ID) is True


def test_a_known_vector_matches_a_hmac_computed_here() -> None:
    manifest = f"id:{DATA_ID};request-id:{REQUEST_ID};ts:{TS};"
    v1 = hmac.new(b"s3cret", manifest.encode(), hashlib.sha256).hexdigest()

    assert signature_valid("s3cret", f"ts={TS},v1={v1}", REQUEST_ID, DATA_ID) is True


def test_without_a_request_id_the_manifest_has_only_the_id_and_the_timestamp() -> None:
    v1 = hmac.new(SECRET.encode(), f"id:{DATA_ID};ts:{TS};".encode(), hashlib.sha256).hexdigest()

    assert signature_valid(SECRET, f"ts={TS},v1={v1}", None, DATA_ID) is True
    assert signature_valid(SECRET, f"ts={TS},v1={v1}", "", DATA_ID) is True
    # The same header does not validate once a request id is present.
    assert signature_valid(SECRET, f"ts={TS},v1={v1}", REQUEST_ID, DATA_ID) is False


def test_without_a_data_id_the_manifest_has_only_the_request_id_and_the_timestamp() -> None:
    v1 = hmac.new(
        SECRET.encode(), f"request-id:{REQUEST_ID};ts:{TS};".encode(), hashlib.sha256
    ).hexdigest()

    assert signature_valid(SECRET, f"ts={TS},v1={v1}", REQUEST_ID, None) is True
    assert signature_valid(SECRET, f"ts={TS},v1={v1}", REQUEST_ID, "") is True


def test_with_neither_id_the_manifest_is_the_timestamp_alone() -> None:
    v1 = hmac.new(SECRET.encode(), f"ts:{TS};".encode(), hashlib.sha256).hexdigest()

    assert signature_valid(SECRET, f"ts={TS},v1={v1}", None, None) is True


def test_the_parts_may_come_in_any_order_with_upper_case_keys() -> None:
    v1 = v1_of(sign(DATA_ID, REQUEST_ID))

    assert signature_valid(SECRET, f"V1={v1},TS={TS}", REQUEST_ID, DATA_ID) is True


@pytest.mark.parametrize("header", [None, ""])
def test_a_missing_or_empty_header_is_invalid(header: str | None) -> None:
    assert signature_valid(SECRET, header, REQUEST_ID, DATA_ID) is False


def test_a_header_with_only_v2_is_invalid() -> None:
    v1 = v1_of(sign(DATA_ID, REQUEST_ID))

    assert signature_valid(SECRET, f"ts={TS},v2={v1}", REQUEST_ID, DATA_ID) is False


@pytest.mark.parametrize("ts", ["", "abc", "12.5", "-1", "１２３"])
def test_a_missing_or_non_numeric_timestamp_is_invalid(ts: str) -> None:
    v1 = v1_of(sign(DATA_ID, REQUEST_ID))

    assert signature_valid(SECRET, f"ts={ts},v1={v1}", REQUEST_ID, DATA_ID) is False


def test_a_header_without_a_timestamp_is_invalid() -> None:
    v1 = v1_of(sign(DATA_ID, REQUEST_ID))

    assert signature_valid(SECRET, f"v1={v1}", REQUEST_ID, DATA_ID) is False


def test_a_missing_or_empty_v1_is_invalid() -> None:
    assert signature_valid(SECRET, f"ts={TS}", REQUEST_ID, DATA_ID) is False
    assert signature_valid(SECRET, f"ts={TS},v1=", REQUEST_ID, DATA_ID) is False


def test_a_header_without_key_value_pairs_is_invalid() -> None:
    assert signature_valid(SECRET, "garbage", REQUEST_ID, DATA_ID) is False


def test_a_different_secret_is_invalid() -> None:
    header = sign(DATA_ID, REQUEST_ID, secret="other")

    assert signature_valid(SECRET, header, REQUEST_ID, DATA_ID) is False


@pytest.mark.parametrize(
    ("data_id", "request_id"), [("999", REQUEST_ID), (DATA_ID, "req-other"), (None, REQUEST_ID)]
)
def test_changing_the_data_id_or_the_request_id_is_invalid(
    data_id: str | None, request_id: str | None
) -> None:
    header = sign(DATA_ID, REQUEST_ID)

    assert signature_valid(SECRET, header, request_id, data_id) is False


def test_changing_the_timestamp_is_invalid() -> None:
    v1 = v1_of(sign(DATA_ID, REQUEST_ID))

    assert signature_valid(SECRET, f"ts=1700000001,v1={v1}", REQUEST_ID, DATA_ID) is False


def test_a_truncated_or_upper_case_digest_is_invalid() -> None:
    v1 = v1_of(sign(DATA_ID, REQUEST_ID))

    assert signature_valid(SECRET, f"ts={TS},v1={v1[:-1]}", REQUEST_ID, DATA_ID) is False
    assert signature_valid(SECRET, f"ts={TS},v1={v1.upper()}", REQUEST_ID, DATA_ID) is False


def test_a_non_ascii_digest_is_a_mismatch_not_an_error() -> None:
    assert signature_valid(SECRET, f"ts={TS},v1=ñandú", REQUEST_ID, DATA_ID) is False


def test_there_is_no_timestamp_tolerance() -> None:
    # An old but well-formed timestamp still validates: replays only make us re-read Mercado Pago.
    header = sign(DATA_ID, REQUEST_ID, ts="1")

    assert signature_valid(SECRET, header, REQUEST_ID, DATA_ID) is True


# --- errors and events -------------------------------------------------------------------------


def test_the_invalid_signature_error_is_a_401_category_with_its_code() -> None:
    error = PaymentWebhookSignatureInvalid()

    assert isinstance(error, UnauthenticatedError)
    assert (error.code, error.message) == ("PAYMENTS_WEBHOOK_SIGNATURE_INVALID", "Firma inválida")


def test_the_events_have_their_names_and_payloads() -> None:
    received = PaymentNotificationReceived(payment_id="1")
    flagged = PaymentFlagged(
        payment_id="1", number=1001, reason="stock_gone", amount_cents=500, currency="MXN"
    )

    assert PaymentNotificationReceived.name == "payments.notification.received"
    assert PaymentFlagged.name == "payments.payment.flagged"
    assert received.payment_id == "1"
    assert (flagged.number, flagged.reason, flagged.amount_cents, flagged.currency) == (
        1001,
        "stock_gone",
        500,
        "MXN",
    )
    with pytest.raises(FrozenInstanceError):
        received.payment_id = "2"  # type: ignore[misc]
