"""`StockHoldsAdapter` (plan 003 step 4) over a stand-in for inventory's `StockReservations`: it
forwards the call and translates only inventory's stock refusal. The real facade is exercised
in the integration tests."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from fragancia_api.modules.orders.infrastructure.stock_holds import StockHoldsAdapter
from fragancia_api.shared.kernel import ConflictError, DomainError, Err, Ok, Result
from tests.unit.orders.support import NOW, presentation_id


@dataclass(frozen=True)
class InventoryError(ConflictError):
    code: str = "INVENTORY_STOCK_UNAVAILABLE"
    message: str = "inventory's own message"


class FakeReservations:
    def __init__(self, result: Result[UUID, DomainError]) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[UUID, int], Any]] = []

    async def reserve(
        self, reference: str, quantities: dict[UUID, int], expires_at: Any
    ) -> Result[UUID, DomainError]:
        self.calls.append((reference, dict(quantities), expires_at))
        return self.result


async def test_a_hold_forwards_the_reference_the_quantities_and_the_expiry() -> None:
    reservation = UUID(int=42)
    fake = FakeReservations(Ok(reservation))
    adapter = StockHoldsAdapter(fake)  # type: ignore[arg-type]
    expires = NOW + timedelta(minutes=30)

    result = await adapter.hold("order:abc", {presentation_id(1): 2}, expires)

    assert result == Ok(reservation)
    assert fake.calls == [("order:abc", {presentation_id(1): 2}, expires)]


async def test_inventorys_stock_refusal_becomes_the_checkout_stock_refusal_with_its_details() -> (
    None
):
    lines = [{"presentation_id": str(presentation_id(1)), "requested": 3, "available": 1}]
    fake = FakeReservations(Err(InventoryError(details={"lines": lines})))

    result = await StockHoldsAdapter(fake).hold("r", {presentation_id(1): 3}, NOW)  # type: ignore[arg-type]

    assert isinstance(result, Err)
    assert result.error.code == "ORDERS_CHECKOUT_STOCK_UNAVAILABLE"
    assert result.error.message == "Ya no hay suficientes unidades de algunos productos"
    assert result.error.details == {"lines": lines}


@pytest.mark.parametrize(
    "code", ["INVENTORY_PRESENTATION_NOT_STOCKED", "INVENTORY_PRESENTATION_NOT_FOUND"]
)
async def test_a_presentation_that_stopped_being_stocked_or_vanished_means_the_cart_changed(
    code: str,
) -> None:
    fake = FakeReservations(Err(InventoryError(code=code, message="inventory's own message")))

    result = await StockHoldsAdapter(fake).hold("r", {presentation_id(1): 1}, NOW)  # type: ignore[arg-type]

    assert isinstance(result, Err)
    assert result.error.code == "ORDERS_CHECKOUT_CART_CHANGED"
    assert result.error.details is None or result.error.details == {}


async def test_any_other_inventory_error_passes_through_unchanged() -> None:
    other = InventoryError(code="INVENTORY_SOMETHING_ELSE", message="other")
    fake = FakeReservations(Err(other))

    result = await StockHoldsAdapter(fake).hold("r", {presentation_id(1): 1}, NOW)  # type: ignore[arg-type]

    assert result == Err(other)
