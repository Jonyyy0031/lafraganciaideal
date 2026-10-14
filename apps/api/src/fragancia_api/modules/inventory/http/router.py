from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Query, Request

from fragancia_api.modules.inventory.application.commands.stock import (
    RecordMovement,
    SetThreshold,
    UpdateInventorySettings,
)
from fragancia_api.modules.inventory.application.queries.stock import (
    GetInventorySettings,
    GetStock,
    ListMovements,
    ListReservationHolds,
    ListStock,
)
from fragancia_api.modules.inventory.contracts import (
    InventorySettingsRequest,
    InventorySettingsResponse,
    MovementCreated,
    MovementRequest,
    ReservationHoldPage,
    StockMovementPage,
    StockPage,
    StockResponse,
    ThresholdRequest,
)
from fragancia_api.shared.contracts import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE
from fragancia_api.shared.http import (
    ErrorResponse,
    admin_router,
    provide,
    require_permission,
    unwrap,
)

# Permissions are atomic strings owned by identity (modules may not import each other).
INVENTORY_MANAGE = "inventory:manage"

admin = admin_router(
    prefix="/inventory",
    tags=["inventory · admin"],
    dependencies=[Depends(require_permission(INVENTORY_MANAGE))],
)

type _Responses = dict[int | str, dict[str, Any]]

_404: _Responses = {404: {"model": ErrorResponse}}
_404_422: _Responses = {404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}}


@admin.post(
    "/presentations/{presentation_id}/movements",
    status_code=201,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
async def record_stock_movement(
    presentation_id: UUID,
    body: MovementRequest,
    request: Request,
    use_case: Annotated[RecordMovement, Depends(provide(RecordMovement))],
) -> MovementCreated:
    """Record a stock movement of an in-stock presentation. 404
    `INVENTORY_PRESENTATION_NOT_FOUND`; 409 `INVENTORY_STOCK_INSUFFICIENT` when the units on
    hand would go below 0; 422 `INVENTORY_PRESENTATION_NOT_STOCKED` (made to order),
    `INVENTORY_QUANTITY_INVALID` or `INVENTORY_REASON_INVALID`."""
    actor_id: str = request.state.actor.id
    return unwrap(await use_case.execute(presentation_id, body, actor_id))


@admin.get("/presentations/{presentation_id}", responses=_404_422)
async def get_stock(
    presentation_id: UUID,
    use_case: Annotated[GetStock, Depends(provide(GetStock))],
) -> StockResponse:
    """The units on hand, reserved and available, thresholds and low flag (by available) of an
    in-stock presentation. 404
    `INVENTORY_PRESENTATION_NOT_FOUND`, 422 `INVENTORY_PRESENTATION_NOT_STOCKED`."""
    return unwrap(await use_case.execute(presentation_id))


@admin.get("/presentations/{presentation_id}/movements", responses=_404)
async def list_stock_movements(
    presentation_id: UUID,
    use_case: Annotated[ListMovements, Depends(provide(ListMovements))],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> StockMovementPage:
    """The ledger of a presentation, newest first. 404 `INVENTORY_PRESENTATION_NOT_FOUND`."""
    return unwrap(await use_case.execute(presentation_id, page=page, size=size))


@admin.get("/presentations/{presentation_id}/reservations", responses=_404)
async def list_stock_reservations(
    presentation_id: UUID,
    use_case: Annotated[ListReservationHolds, Depends(provide(ListReservationHolds))],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ReservationHoldPage:
    """The active reservations holding units of a presentation, soonest expiry first. 404
    `INVENTORY_PRESENTATION_NOT_FOUND`."""
    return unwrap(await use_case.execute(presentation_id, page=page, size=size))


@admin.put("/presentations/{presentation_id}/threshold", status_code=204, responses=_404_422)
async def set_stock_threshold(
    presentation_id: UUID,
    body: ThresholdRequest,
    use_case: Annotated[SetThreshold, Depends(provide(SetThreshold))],
) -> None:
    """Set the low-stock threshold of a presentation, or clear it with `null`. 404
    `INVENTORY_PRESENTATION_NOT_FOUND`; 422 `INVENTORY_PRESENTATION_NOT_STOCKED` or
    `INVENTORY_THRESHOLD_INVALID`."""
    unwrap(await use_case.execute(presentation_id, body.threshold))


@admin.get("/stock")
async def list_stock(
    use_case: Annotated[ListStock, Depends(provide(ListStock))],
    low: bool = False,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> StockPage:
    """Every in-stock presentation with its units, in catalog order; `low=true` keeps the low
    ones (presentations never counted show 0 on hand)."""
    return await use_case.execute(low=low, page=page, size=size)


@admin.get("/settings")
async def get_inventory_settings(
    use_case: Annotated[GetInventorySettings, Depends(provide(GetInventorySettings))],
) -> InventorySettingsResponse:
    """The global low-stock threshold."""
    return await use_case.execute()


@admin.put("/settings", status_code=204, responses={422: {"model": ErrorResponse}})
async def update_inventory_settings(
    body: InventorySettingsRequest,
    use_case: Annotated[UpdateInventorySettings, Depends(provide(UpdateInventorySettings))],
) -> None:
    """Set the global low-stock threshold. 422 `INVENTORY_THRESHOLD_INVALID`."""
    unwrap(await use_case.execute(body.low_stock_threshold))


routers = (admin,)
