"""Back-office routes of the catalog import from an .xlsx file (`catalog:manage`)."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Request, Response

from fragancia_api.modules.catalog.application.commands.catalog_imports import (
    ConfirmCatalogImport,
    DiscardCatalogImport,
    ImportUploadLimit,
    StartCatalogImport,
)
from fragancia_api.modules.catalog.application.queries.catalog_imports import (
    DownloadCatalogTemplate,
    GetCatalogImport,
)
from fragancia_api.modules.catalog.contracts import CatalogImportResponse, CreatedResponse
from fragancia_api.modules.catalog.domain.errors import ImportFormatInvalid, ImportTooLarge
from fragancia_api.modules.catalog.http.router import CATALOG_MANAGE
from fragancia_api.shared.http import (
    ErrorResponse,
    admin_router,
    provide,
    require_permission,
    unwrap,
)
from fragancia_api.shared.http.errors import Forbidden
from fragancia_api.shared.kernel import Err

# The identity permission name (modules may not import each other), like `CATALOG_MANAGE`.
INVENTORY_MANAGE = "inventory:manage"

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

admin_imports = admin_router(
    prefix="/catalog-imports",
    tags=["catalog · import"],
    dependencies=[Depends(require_permission(CATALOG_MANAGE))],
)

_IMPORT_BODY_OPENAPI: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {XLSX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
    }
}


@admin_imports.get(
    "/template",
    response_class=Response,
    responses={
        200: {"content": {XLSX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}}}
    },
)
async def download_catalog_import_template(
    use_case: Annotated[DownloadCatalogTemplate, Depends(provide(DownloadCatalogTemplate))],
) -> Response:
    """The .xlsx import template: one row per presentation, with dropdowns of the active
    olfactory families and concentrations, gender, availability and published."""
    return Response(
        content=await use_case.execute(),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="plantilla-catalogo.xlsx"'},
    )


@admin_imports.post(
    "",
    status_code=202,
    responses={422: {"model": ErrorResponse}},
    openapi_extra=_IMPORT_BODY_OPENAPI,
)
async def start_catalog_import(
    request: Request,
    use_case: Annotated[StartCatalogImport, Depends(provide(StartCatalogImport))],
    limit: Annotated[ImportUploadLimit, Depends(provide(ImportUploadLimit))],
) -> CreatedResponse:
    """Upload a filled template to validate: the raw .xlsx bytes are the request body, with
    `Content-Type` `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`. The
    body is read only after the admin checks pass, and is cut off at the size limit. The
    worker validates it; poll `GET /admin/catalog-imports/{id}` for the report. 422
    `CATALOG_IMPORT_TOO_LARGE` or `CATALOG_IMPORT_FORMAT_INVALID`."""
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != XLSX_MEDIA_TYPE:
        unwrap(Err(ImportFormatInvalid()))
    too_large = ImportTooLarge(details={"max_bytes": limit.max_bytes})
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
        unwrap(Err(ImportFormatInvalid()))
    created_by: str = request.state.actor.id
    return CreatedResponse(id=unwrap(await use_case.execute(b"".join(chunks), created_by)))


@admin_imports.get("/{import_id}", responses={404: {"model": ErrorResponse}})
async def get_catalog_import(
    import_id: UUID,
    use_case: Annotated[GetCatalogImport, Depends(provide(GetCatalogImport))],
) -> CatalogImportResponse:
    """An import with its status and, once validated, its report: `ready` with the summary
    of what confirming does, or `invalid` with one issue per problem (Excel row, header text,
    code). 404 `CATALOG_IMPORT_NOT_FOUND`."""
    return unwrap(await use_case.execute(import_id))


@admin_imports.post(
    "/{import_id}/confirm",
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
async def confirm_catalog_import(
    import_id: UUID,
    request: Request,
    use_case: Annotated[ConfirmCatalogImport, Depends(provide(ConfirmCatalogImport))],
    query: Annotated[GetCatalogImport, Depends(provide(GetCatalogImport))],
) -> CatalogImportResponse:
    """Apply a ready import, all or nothing, in one transaction; answers the import, now
    `applied`. 403 `FORBIDDEN` when the file has Existencias and the actor lacks
    `inventory:manage`; 404 `CATALOG_IMPORT_NOT_FOUND`; 409 `CATALOG_IMPORT_NOT_READY` (not
    ready, or past `expires_at`) or `CATALOG_IMPORT_STALE` when the catalog changed since the
    validation: nothing is written, the import stays `ready` and `details.issues` lists each
    affected row (`row`, `column`, `code`, `message`); upload the file again."""
    actor_id: str = request.state.actor.id
    if INVENTORY_MANAGE not in request.state.actor.permissions:
        # Decision 21: counts need `inventory:manage`. The plan of a ready import never
        # changes, so this check cannot race the confirm.
        current = unwrap(await query.execute(import_id))  # an unknown id still answers 404
        if current.summary is not None and current.summary.stock_counts > 0:
            raise Forbidden
    unwrap(await use_case.execute(import_id, actor_id))
    return unwrap(await query.execute(import_id))


@admin_imports.post(
    "/{import_id}/discard",
    status_code=204,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def discard_catalog_import(
    import_id: UUID,
    use_case: Annotated[DiscardCatalogImport, Depends(provide(DiscardCatalogImport))],
) -> None:
    """Drop a ready import early: it becomes `discarded` and its staged images are deleted.
    404 `CATALOG_IMPORT_NOT_FOUND`; 409 `CATALOG_IMPORT_NOT_READY`."""
    unwrap(await use_case.execute(import_id))


import_routers = (admin_imports,)
