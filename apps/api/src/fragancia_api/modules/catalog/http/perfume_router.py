"""Back-office routes for perfumes and their presentations (`catalog:manage`)."""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, Path, Query, Request

from fragancia_api.modules.catalog.application.commands.perfumes import (
    ArchivePerfume,
    CreatePerfume,
    HidePerfume,
    PublishPerfume,
    RestorePerfume,
    UpdatePerfume,
)
from fragancia_api.modules.catalog.application.commands.photos import (
    AddPhoto,
    PhotoUploadLimit,
    RemovePhoto,
    ReorderPhotos,
)
from fragancia_api.modules.catalog.application.commands.presentations import (
    AddPresentation,
    ArchivePresentation,
    RestorePresentation,
    UpdatePresentation,
)
from fragancia_api.modules.catalog.application.ports import PublicPerfumeFilters
from fragancia_api.modules.catalog.application.queries.perfumes import (
    GetAdminPerfume,
    GetPublicPerfume,
    ListAdminPerfumes,
    ListPublicPerfumes,
)
from fragancia_api.modules.catalog.contracts import (
    AdminPerfume,
    AdminPerfumePage,
    CreatedResponse,
    PerfumeRequest,
    PresentationRequest,
    PublicPerfume,
    PublicPerfumePage,
    ReorderPhotosRequest,
)
from fragancia_api.modules.catalog.domain.errors import PhotoFormatInvalid, PhotoTooLarge
from fragancia_api.modules.catalog.http.router import CATALOG_MANAGE
from fragancia_api.shared.contracts import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE
from fragancia_api.shared.http import (
    ErrorResponse,
    admin_router,
    provide,
    public_router,
    require_permission,
    unwrap,
)
from fragancia_api.shared.kernel import Err

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
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
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


_PHOTO_CONTENT_TYPES = ("image/jpeg", "image/png", "image/webp")
_PHOTO_BODY_OPENAPI: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            content_type: {"schema": {"type": "string", "format": "binary"}}
            for content_type in _PHOTO_CONTENT_TYPES
        },
    }
}


@admin_perfumes.post(
    "/{perfume_id}/photos",
    status_code=201,
    responses=_404_409_422,
    openapi_extra=_PHOTO_BODY_OPENAPI,
)
async def add_photo(
    perfume_id: UUID,
    request: Request,
    use_case: Annotated[AddPhoto, Depends(provide(AddPhoto))],
    limit: Annotated[PhotoUploadLimit, Depends(provide(PhotoUploadLimit))],
) -> CreatedResponse:
    """Upload a photo as the perfume's next one: the raw image bytes are the request body, with
    `Content-Type` `image/jpeg`, `image/png` or `image/webp`. The body is read only after the
    admin checks pass, and is cut off at the size limit. 404 `CATALOG_PERFUME_NOT_FOUND`; 409
    `CATALOG_PHOTO_LIMIT_REACHED` with 3 photos already; 422 `CATALOG_PHOTO_TOO_LARGE`,
    `CATALOG_PHOTO_FORMAT_INVALID`, `CATALOG_PHOTO_TOO_SMALL` or `CATALOG_PERFUME_ARCHIVED`."""
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type not in _PHOTO_CONTENT_TYPES:
        unwrap(Err(PhotoFormatInvalid(details={"formats": ["jpeg", "png", "webp"]})))
    too_large = PhotoTooLarge(details={"max_bytes": limit.max_bytes})
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit.max_bytes:
        unwrap(Err(too_large))
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit.max_bytes:
            unwrap(Err(too_large))
        chunks.append(chunk)
    if total == 0:
        unwrap(Err(PhotoFormatInvalid(details={"formats": ["jpeg", "png", "webp"]})))
    return CreatedResponse(id=unwrap(await use_case.execute(perfume_id, b"".join(chunks))))


@admin_perfumes.put("/{perfume_id}/photos/order", status_code=204, responses=_404_422)
async def reorder_photos(
    perfume_id: UUID,
    body: ReorderPhotosRequest,
    use_case: Annotated[ReorderPhotos, Depends(provide(ReorderPhotos))],
) -> None:
    """Set the order of the photos: the full list of the perfume's photo ids. 404
    `CATALOG_PERFUME_NOT_FOUND`; 422 `CATALOG_PHOTO_ORDER_INVALID` or `CATALOG_PERFUME_ARCHIVED`."""
    unwrap(await use_case.execute(perfume_id, body.photo_ids))


@admin_perfumes.delete("/{perfume_id}/photos/{photo_id}", status_code=204, responses=_404_422)
async def remove_photo(
    perfume_id: UUID,
    photo_id: UUID,
    use_case: Annotated[RemovePhoto, Depends(provide(RemovePhoto))],
) -> None:
    """Remove a photo and delete its files; the rest are renumbered. 404
    `CATALOG_PERFUME_NOT_FOUND` or `CATALOG_PHOTO_NOT_FOUND`; 422 `CATALOG_PERFUME_ARCHIVED`."""
    unwrap(await use_case.execute(perfume_id, photo_id))


public_perfumes = public_router(prefix="/perfumes", tags=["catalog"])


@public_perfumes.get("")
async def list_public_perfumes(
    use_case: Annotated[ListPublicPerfumes, Depends(provide(ListPublicPerfumes))],
    q: Annotated[str | None, Query(max_length=100)] = None,
    brand: Annotated[list[str], Query()] = [],  # noqa: B006 - FastAPI copies query defaults
    family: Annotated[list[str], Query()] = [],  # noqa: B006
    gender: Annotated[list[Literal["women", "men", "unisex"]], Query()] = [],  # noqa: B006
    min_price_cents: Annotated[int | None, Query(ge=0)] = None,
    max_price_cents: Annotated[int | None, Query(ge=0)] = None,
    in_stock: bool = False,
    sort: Literal["name", "price_asc", "price_desc", "newest"] = "name",
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    size: Annotated[int, Query(ge=1, le=48)] = 24,
) -> PublicPerfumePage:
    """Published perfumes of active brands, filtered (`brand`, `family` and `gender` are
    repeatable; prices filter the "from" price), searched (`q`, accent and case insensitive over
    name, brand and notes) and sorted (default: brand, then name). The "from" price, the price
    filters and the price sort use only presentations that can be bought (made to order, or in
    stock with units), falling back to all of them when none can; `in_stock=true` hides perfumes
    with nothing to buy."""
    return await use_case.execute(
        PublicPerfumeFilters(
            q=q,
            brands=tuple(brand),
            families=tuple(family),
            genders=tuple(gender),
            min_price_cents=min_price_cents,
            max_price_cents=max_price_cents,
            sort=sort,
            page=page,
            size=size,
            in_stock=in_stock,
        )
    )


@public_perfumes.get("/{slug}", responses=_404)
async def get_public_perfume(
    slug: Annotated[str, Path(max_length=240)],
    use_case: Annotated[GetPublicPerfume, Depends(provide(GetPublicPerfume))],
) -> PublicPerfume:
    """A perfume with its active presentations. An old slug still finds it: the answer carries
    the current `slug`, and the web redirects when it differs from the requested one. 404
    `CATALOG_PERFUME_NOT_FOUND` for an unknown or non-visible perfume."""
    return unwrap(await use_case.execute(slug))


perfume_routers = (public_perfumes, admin_perfumes)
