"""Back-office routes for perfumes and their presentations (`catalog:manage`)."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Query

from fragancia_api.modules.catalog.application.commands.perfumes import (
    ArchivePerfume,
    CreatePerfume,
    HidePerfume,
    PublishPerfume,
    RestorePerfume,
    UpdatePerfume,
)
from fragancia_api.modules.catalog.application.commands.presentations import (
    AddPresentation,
    ArchivePresentation,
    RestorePresentation,
    UpdatePresentation,
)
from fragancia_api.modules.catalog.application.queries.perfumes import (
    GetAdminPerfume,
    ListAdminPerfumes,
)
from fragancia_api.modules.catalog.contracts import (
    AdminPerfume,
    AdminPerfumePage,
    CreatedResponse,
    PerfumeRequest,
    PresentationRequest,
)
from fragancia_api.modules.catalog.http.router import CATALOG_MANAGE
from fragancia_api.shared.contracts import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from fragancia_api.shared.http import (
    ErrorResponse,
    admin_router,
    provide,
    require_permission,
    unwrap,
)

admin_perfumes = admin_router(
    prefix="/perfumes",
    tags=["catalog · perfumes"],
    dependencies=[Depends(require_permission(CATALOG_MANAGE))],
)

type _Responses = dict[int | str, dict[str, Any]]

_404: _Responses = {404: {"model": ErrorResponse}}
_404_422: _Responses = {404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}}
_404_409_422: _Responses = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@admin_perfumes.get("")
async def list_admin_perfumes(
    use_case: Annotated[ListAdminPerfumes, Depends(provide(ListAdminPerfumes))],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    archived: bool = False,
) -> AdminPerfumePage:
    """Non-archived perfumes (the archived ones with `archived=true`), ordered by brand, then
    name, with their count of active presentations."""
    return await use_case.execute(page=page, size=size, archived=archived)


@admin_perfumes.post(
    "",
    status_code=201,
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def create_perfume(
    body: PerfumeRequest,
    use_case: Annotated[CreatePerfume, Depends(provide(CreatePerfume))],
) -> CreatedResponse:
    """Register a perfume, hidden and without presentations. 409
    `CATALOG_PERFUME_ALREADY_EXISTS` for the same brand, name and concentration; 422
    `CATALOG_PERFUME_NAME_INVALID`, `CATALOG_PERFUME_DESCRIPTION_TOO_LONG`,
    `CATALOG_PERFUME_NOTES_INVALID`, or `CATALOG_PERFUME_BRAND_UNAVAILABLE`,
    `CATALOG_PERFUME_FAMILY_UNAVAILABLE`, `CATALOG_PERFUME_CONCENTRATION_UNAVAILABLE` when the
    reference is missing or archived."""
    return CreatedResponse(id=unwrap(await use_case.execute(body)))


@admin_perfumes.get("/{perfume_id}", responses=_404)
async def get_admin_perfume(
    perfume_id: UUID,
    use_case: Annotated[GetAdminPerfume, Depends(provide(GetAdminPerfume))],
) -> AdminPerfume:
    """A perfume with every presentation, ordered by ml. 404 `CATALOG_PERFUME_NOT_FOUND`."""
    return unwrap(await use_case.execute(perfume_id))


@admin_perfumes.put("/{perfume_id}", status_code=204, responses=_404_409_422)
async def update_perfume(
    perfume_id: UUID,
    body: PerfumeRequest,
    use_case: Annotated[UpdatePerfume, Depends(provide(UpdatePerfume))],
) -> None:
    """Replace every field of a perfume; its slug is recomputed. 404
    `CATALOG_PERFUME_NOT_FOUND`; 409 `CATALOG_PERFUME_ALREADY_EXISTS`; 422 the create errors
    (only a changed brand, family or concentration must be active) or
    `CATALOG_PERFUME_ARCHIVED`."""
    unwrap(await use_case.execute(perfume_id, body))


@admin_perfumes.post("/{perfume_id}/publish", status_code=204, responses=_404_422)
async def publish_perfume(
    perfume_id: UUID,
    use_case: Annotated[PublishPerfume, Depends(provide(PublishPerfume))],
) -> None:
    """Show a perfume in the storefront (idempotent). 404 `CATALOG_PERFUME_NOT_FOUND`; 422
    `CATALOG_PERFUME_NOTHING_TO_SELL` without an active presentation, or
    `CATALOG_PERFUME_ARCHIVED`."""
    unwrap(await use_case.execute(perfume_id))


@admin_perfumes.post("/{perfume_id}/hide", status_code=204, responses=_404)
async def hide_perfume(
    perfume_id: UUID,
    use_case: Annotated[HidePerfume, Depends(provide(HidePerfume))],
) -> None:
    """Hide a perfume from the storefront (idempotent). 404 `CATALOG_PERFUME_NOT_FOUND`."""
    unwrap(await use_case.execute(perfume_id))


@admin_perfumes.post("/{perfume_id}/archive", status_code=204, responses=_404)
async def archive_perfume(
    perfume_id: UUID,
    use_case: Annotated[ArchivePerfume, Depends(provide(ArchivePerfume))],
) -> None:
    """Archive a perfume: it is hidden and becomes read-only (idempotent). 404
    `CATALOG_PERFUME_NOT_FOUND`."""
    unwrap(await use_case.execute(perfume_id))


@admin_perfumes.post("/{perfume_id}/restore", status_code=204, responses=_404)
async def restore_perfume(
    perfume_id: UUID,
    use_case: Annotated[RestorePerfume, Depends(provide(RestorePerfume))],
) -> None:
    """Bring an archived perfume back, still hidden (idempotent). 404
    `CATALOG_PERFUME_NOT_FOUND`."""
    unwrap(await use_case.execute(perfume_id))


@admin_perfumes.post("/{perfume_id}/presentations", status_code=201, responses=_404_409_422)
async def add_presentation(
    perfume_id: UUID,
    body: PresentationRequest,
    use_case: Annotated[AddPresentation, Depends(provide(AddPresentation))],
) -> CreatedResponse:
    """Add a presentation to a perfume. 404 `CATALOG_PERFUME_NOT_FOUND`; 409
    `CATALOG_PRESENTATION_ALREADY_EXISTS` when the perfume has that ml; 422
    `CATALOG_PRESENTATION_ML_INVALID`, `CATALOG_PRESENTATION_PRICE_INVALID`,
    `CATALOG_PRESENTATION_SALE_INVALID`, `CATALOG_PRESENTATION_AVAILABILITY_INVALID` or
    `CATALOG_PERFUME_ARCHIVED`."""
    return CreatedResponse(id=unwrap(await use_case.execute(perfume_id, body)))


@admin_perfumes.put(
    "/{perfume_id}/presentations/{presentation_id}",
    status_code=204,
    responses=_404_409_422,
)
async def update_presentation(
    perfume_id: UUID,
    presentation_id: UUID,
    body: PresentationRequest,
    use_case: Annotated[UpdatePresentation, Depends(provide(UpdatePresentation))],
) -> None:
    """Replace ml, price, sale and availability of a presentation. 404
    `CATALOG_PERFUME_NOT_FOUND` or `CATALOG_PRESENTATION_NOT_FOUND`; 409
    `CATALOG_PRESENTATION_ALREADY_EXISTS` when another presentation has that ml; 422 the add
    errors."""
    unwrap(await use_case.execute(perfume_id, presentation_id, body))


@admin_perfumes.post(
    "/{perfume_id}/presentations/{presentation_id}/archive",
    status_code=204,
    responses=_404_409_422,
)
async def archive_presentation(
    perfume_id: UUID,
    presentation_id: UUID,
    use_case: Annotated[ArchivePresentation, Depends(provide(ArchivePresentation))],
) -> None:
    """Stop selling a presentation (idempotent). 404 `CATALOG_PERFUME_NOT_FOUND` or
    `CATALOG_PRESENTATION_NOT_FOUND`; 409 `CATALOG_PERFUME_LAST_PRESENTATION` for the last
    active presentation of a published perfume; 422 `CATALOG_PERFUME_ARCHIVED`."""
    unwrap(await use_case.execute(perfume_id, presentation_id))


@admin_perfumes.post(
    "/{perfume_id}/presentations/{presentation_id}/restore",
    status_code=204,
    responses=_404_422,
)
async def restore_presentation(
    perfume_id: UUID,
    presentation_id: UUID,
    use_case: Annotated[RestorePresentation, Depends(provide(RestorePresentation))],
) -> None:
    """Sell an archived presentation again (idempotent). 404 `CATALOG_PERFUME_NOT_FOUND` or
    `CATALOG_PRESENTATION_NOT_FOUND`; 422 `CATALOG_PERFUME_ARCHIVED`."""
    unwrap(await use_case.execute(perfume_id, presentation_id))


perfume_routers = (admin_perfumes,)
