"""Plan 004 step 5: `StockHoldsAdapter.commit`, `release` and `return_sold` hand the call to
inventory's facade and give its result back unchanged (inventory's own code and details, no
translation to a checkout error)."""

from uuid import UUID

import pytest

from fragancia_api.modules.orders.infrastructure.stock_holds import StockHoldsAdapter
from fragancia_api.shared.kernel import ConflictError, DomainError, Err, Ok, Result

RESERVATION = UUID(int=42)


class FakeFacade:
    def __init__(self, result: Result[None, DomainError]) -> None:
        self.result = result
        self.calls: list[tuple[str, UUID]] = []

    async def commit(self, reservation_id: UUID) -> Result[None, DomainError]:
        self.calls.append(("commit", reservation_id))
        return self.result

    async def release(self, reservation_id: UUID) -> Result[None, DomainError]:
        self.calls.append(("release", reservation_id))
        return self.result

    async def return_sold(self, reservation_id: UUID) -> Result[None, DomainError]:
        self.calls.append(("return_sold", reservation_id))
        return self.result


@pytest.mark.parametrize("operation", ["commit", "release", "return_sold"])
async def test_each_operation_forwards_the_reservation_id_and_returns_ok(operation: str) -> None:
    facade = FakeFacade(Ok(None))
    adapter = StockHoldsAdapter(facade)  # type: ignore[arg-type]

    result = await getattr(adapter, operation)(RESERVATION)

    assert result == Ok(None)
    assert facade.calls == [(operation, RESERVATION)]


@pytest.mark.parametrize("operation", ["commit", "release", "return_sold"])
async def test_each_operation_passes_inventorys_refusal_through_unchanged(operation: str) -> None:
    refusal = ConflictError(
        code="INVENTORY_STOCK_UNAVAILABLE",
        message="inventory's own message",
        details={"lines": [{"requested": 2, "available": 0}]},
    )
    adapter = StockHoldsAdapter(FakeFacade(Err(refusal)))  # type: ignore[arg-type]

    result = await getattr(adapter, operation)(RESERVATION)

    assert result == Err(refusal)
    assert isinstance(result, Err)
    assert result.error is refusal
