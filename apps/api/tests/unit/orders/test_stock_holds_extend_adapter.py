"""Payments plan 003 step 3: `StockHoldsAdapter.extend` forwards to inventory's
`StockReservations.extend` and passes its answer, success or refusal, through unchanged. The
real facade is exercised in the integration tests."""

from datetime import datetime, timedelta
from uuid import UUID

from fragancia_api.modules.orders.infrastructure.stock_holds import StockHoldsAdapter
from fragancia_api.shared.kernel import ConflictError, DomainError, Err, Ok, Result
from tests.unit.orders.support import NOW


class FakeReservations:
    def __init__(self, result: Result[None, DomainError]) -> None:
        self.result = result
        self.calls: list[tuple[UUID, datetime]] = []

    async def extend(self, reservation_id: UUID, expires_at: datetime) -> Result[None, DomainError]:
        self.calls.append((reservation_id, expires_at))
        return self.result


async def test_extend_forwards_the_reservation_and_the_new_expiry() -> None:
    fake = FakeReservations(Ok(None))
    expires = NOW + timedelta(hours=48)

    result = await StockHoldsAdapter(fake).extend(UUID(int=42), expires)  # type: ignore[arg-type]

    assert result == Ok(None)
    assert fake.calls == [(UUID(int=42), expires)]


async def test_extend_passes_an_inventory_refusal_through_with_its_own_code() -> None:
    refusal = ConflictError(code="INVENTORY_RESERVATION_EXPIRED", message="inventory's own")
    fake = FakeReservations(Err(refusal))

    result = await StockHoldsAdapter(fake).extend(UUID(int=42), NOW)  # type: ignore[arg-type]

    assert isinstance(result, Err)
    assert result.error is refusal
