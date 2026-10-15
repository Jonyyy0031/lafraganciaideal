from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Query

from fragancia_api.shared.application.events import OutboxAdmin, OutboxEventNotFound
from fragancia_api.shared.contracts import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE
from fragancia_api.shared.contracts.outbox import FailedEventPage
from fragancia_api.shared.http.access import admin_router, require_permission
from fragancia_api.shared.http.errors import DomainErrorRaised, ErrorResponse
from fragancia_api.shared.http.services import provide

# Permissions are atomic strings owned by identity (modules may not import each other).
OUTBOX_MANAGE = "outbox:manage"

router = admin_router(
    prefix="/outbox",
    tags=["platform · outbox"],
    dependencies=[Depends(require_permission(OUTBOX_MANAGE))],
)

_404: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse}}


@router.get("/failed")
async def list_failed_events(
    admin: Annotated[OutboxAdmin, Depends(provide(OutboxAdmin))],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> FailedEventPage:
    """Events that failed delivery for about 24 hours and were given up on, newest first."""
    return await admin.failed(page=page, size=size)


@router.post("/{event_id}/retry", status_code=204, responses=_404)
async def retry_failed_event(
    event_id: UUID, admin: Annotated[OutboxAdmin, Depends(provide(OutboxAdmin))]
) -> None:
    """Retry a failed event: it restarts the about-24-hour retry window. 404
    `OUTBOX_EVENT_NOT_FOUND` when no failed event has that id."""
    if not await admin.retry(event_id):
        raise DomainErrorRaised(OutboxEventNotFound())
