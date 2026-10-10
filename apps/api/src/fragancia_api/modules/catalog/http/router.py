from typing import Annotated
from uuid import UUID

from fastapi import Depends, Query

from fragancia_api.modules.catalog.application.commands.brand_status import (
    ArchiveBrand,
    RestoreBrand,
)
from fragancia_api.modules.catalog.application.commands.concentration_status import (
    ArchiveConcentration,
    RestoreConcentration,
)
from fragancia_api.modules.catalog.application.commands.create_brand import CreateBrand
from fragancia_api.modules.catalog.application.commands.create_concentration import (
    CreateConcentration,
)
from fragancia_api.modules.catalog.application.commands.create_olfactory_family import (
    CreateOlfactoryFamily,
)
from fragancia_api.modules.catalog.application.commands.olfactory_family_status import (
    ArchiveOlfactoryFamily,
    RestoreOlfactoryFamily,
)
from fragancia_api.modules.catalog.application.commands.rename_brand import RenameBrand
from fragancia_api.modules.catalog.application.commands.rename_olfactory_family import (
    RenameOlfactoryFamily,
)
from fragancia_api.modules.catalog.application.commands.update_concentration import (
    UpdateConcentration,
)
from fragancia_api.modules.catalog.application.queries.list_brands import (
    ListAdminBrands,
    ListPublicBrands,
)
from fragancia_api.modules.catalog.application.queries.list_concentrations import (
    ListAdminConcentrations,
    ListPublicConcentrations,
)
from fragancia_api.modules.catalog.application.queries.list_olfactory_families import (
    ListAdminOlfactoryFamilies,
    ListPublicOlfactoryFamilies,
)
from fragancia_api.modules.catalog.contracts import (
    AdminBrandPage,
    AdminConcentrationPage,
    AdminOlfactoryFamilyPage,
    CreateBrandRequest,
    CreateConcentrationRequest,
    CreatedResponse,
    CreateOlfactoryFamilyRequest,
    PublicBrand,
    PublicConcentration,
    PublicOlfactoryFamily,
    RenameBrandRequest,
    RenameOlfactoryFamilyRequest,
    UpdateConcentrationRequest,
)
from fragancia_api.shared.contracts import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE
from fragancia_api.shared.http import (
    ErrorResponse,
    admin_router,
    provide,
    public_router,
    require_permission,
    unwrap,
)

# Permissions are atomic strings owned by identity (modules may not import each other).
CATALOG_MANAGE = "catalog:manage"

public = public_router(prefix="/brands", tags=["catalog"])
admin = admin_router(
    prefix="/brands",
    tags=["catalog · admin"],
    dependencies=[Depends(require_permission(CATALOG_MANAGE))],
)
public_families = public_router(prefix="/olfactory-families", tags=["catalog"])
admin_families = admin_router(
    prefix="/olfactory-families",
    tags=["catalog · admin"],
    dependencies=[Depends(require_permission(CATALOG_MANAGE))],
)
public_concentrations = public_router(prefix="/concentrations", tags=["catalog"])
admin_concentrations = admin_router(
    prefix="/concentrations",
    tags=["catalog · admin"],
    dependencies=[Depends(require_permission(CATALOG_MANAGE))],
)


@public.get("")
async def list_brands(
    use_case: Annotated[ListPublicBrands, Depends(provide(ListPublicBrands))],
) -> list[PublicBrand]:
    """Active brands, ordered by name."""
    return await use_case.execute()


@admin.post(
    "",
    status_code=201,
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def create_brand(
    body: CreateBrandRequest,
    use_case: Annotated[CreateBrand, Depends(provide(CreateBrand))],
) -> CreatedResponse:
    """Register a brand. 409 `CATALOG_BRAND_ALREADY_EXISTS` if its slug is taken, 422
    `CATALOG_BRAND_NAME_INVALID` if the name breaks the rules."""
    return CreatedResponse(id=unwrap(await use_case.execute(body.name)))


@admin.get("")
async def list_all_brands(
    use_case: Annotated[ListAdminBrands, Depends(provide(ListAdminBrands))],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> AdminBrandPage:
    """Every brand (active or not), ordered by name."""
    return await use_case.execute(page=page, size=size)


@admin.patch(
    "/{brand_id}",
    status_code=204,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
async def rename_brand(
    brand_id: UUID,
    body: RenameBrandRequest,
    use_case: Annotated[RenameBrand, Depends(provide(RenameBrand))],
) -> None:
    """Rename a brand; its slug follows. 404 `CATALOG_BRAND_NOT_FOUND`, 409
    `CATALOG_BRAND_ALREADY_EXISTS` if another brand has the slug, 422
    `CATALOG_BRAND_NAME_INVALID`."""
    unwrap(await use_case.execute(brand_id, body.name))


@admin.post("/{brand_id}/archive", status_code=204, responses={404: {"model": ErrorResponse}})
async def archive_brand(
    brand_id: UUID,
    use_case: Annotated[ArchiveBrand, Depends(provide(ArchiveBrand))],
) -> None:
    """Hide a brand from the storefront (idempotent). 404 `CATALOG_BRAND_NOT_FOUND`."""
    unwrap(await use_case.execute(brand_id))


@admin.post("/{brand_id}/restore", status_code=204, responses={404: {"model": ErrorResponse}})
async def restore_brand(
    brand_id: UUID,
    use_case: Annotated[RestoreBrand, Depends(provide(RestoreBrand))],
) -> None:
    """Show an archived brand again (idempotent). 404 `CATALOG_BRAND_NOT_FOUND`."""
    unwrap(await use_case.execute(brand_id))


@public_families.get("")
async def list_olfactory_families(
    use_case: Annotated[ListPublicOlfactoryFamilies, Depends(provide(ListPublicOlfactoryFamilies))],
) -> list[PublicOlfactoryFamily]:
    """Active olfactory families, ordered by name."""
    return await use_case.execute()


@admin_families.get("")
async def list_all_olfactory_families(
    use_case: Annotated[ListAdminOlfactoryFamilies, Depends(provide(ListAdminOlfactoryFamilies))],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> AdminOlfactoryFamilyPage:
    """Every olfactory family (active or not), ordered by name."""
    return await use_case.execute(page=page, size=size)


@admin_families.post(
    "",
    status_code=201,
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def create_olfactory_family(
    body: CreateOlfactoryFamilyRequest,
    use_case: Annotated[CreateOlfactoryFamily, Depends(provide(CreateOlfactoryFamily))],
) -> CreatedResponse:
    """Register an olfactory family. 409 `CATALOG_FAMILY_ALREADY_EXISTS` if its slug is taken,
    422 `CATALOG_FAMILY_NAME_INVALID` if the name breaks the rules."""
    return CreatedResponse(id=unwrap(await use_case.execute(body.name)))


@admin_families.patch(
    "/{family_id}",
    status_code=204,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
async def rename_olfactory_family(
    family_id: UUID,
    body: RenameOlfactoryFamilyRequest,
    use_case: Annotated[RenameOlfactoryFamily, Depends(provide(RenameOlfactoryFamily))],
) -> None:
    """Rename a family; its slug follows. 404 `CATALOG_FAMILY_NOT_FOUND`, 409
    `CATALOG_FAMILY_ALREADY_EXISTS` if another family has the slug, 422
    `CATALOG_FAMILY_NAME_INVALID`."""
    unwrap(await use_case.execute(family_id, body.name))


@admin_families.post(
    "/{family_id}/archive", status_code=204, responses={404: {"model": ErrorResponse}}
)
async def archive_olfactory_family(
    family_id: UUID,
    use_case: Annotated[ArchiveOlfactoryFamily, Depends(provide(ArchiveOlfactoryFamily))],
) -> None:
    """Hide a family from the storefront (idempotent). 404 `CATALOG_FAMILY_NOT_FOUND`."""
    unwrap(await use_case.execute(family_id))


@admin_families.post(
    "/{family_id}/restore", status_code=204, responses={404: {"model": ErrorResponse}}
)
async def restore_olfactory_family(
    family_id: UUID,
    use_case: Annotated[RestoreOlfactoryFamily, Depends(provide(RestoreOlfactoryFamily))],
) -> None:
    """Show an archived family again (idempotent). 404 `CATALOG_FAMILY_NOT_FOUND`."""
    unwrap(await use_case.execute(family_id))


@public_concentrations.get("")
async def list_concentrations(
    use_case: Annotated[ListPublicConcentrations, Depends(provide(ListPublicConcentrations))],
) -> list[PublicConcentration]:
    """Active concentrations, ordered by name."""
    return await use_case.execute()


@admin_concentrations.get("")
async def list_all_concentrations(
    use_case: Annotated[ListAdminConcentrations, Depends(provide(ListAdminConcentrations))],
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> AdminConcentrationPage:
    """Every concentration (active or not), ordered by name."""
    return await use_case.execute(page=page, size=size)


@admin_concentrations.post(
    "",
    status_code=201,
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def create_concentration(
    body: CreateConcentrationRequest,
    use_case: Annotated[CreateConcentration, Depends(provide(CreateConcentration))],
) -> CreatedResponse:
    """Register a concentration. 409 `CATALOG_CONCENTRATION_ALREADY_EXISTS` if its name slug is
    taken, 409 `CATALOG_CONCENTRATION_ABBREVIATION_TAKEN` if its abbreviation slug is, 422
    `CATALOG_CONCENTRATION_NAME_INVALID` or `CATALOG_CONCENTRATION_ABBREVIATION_INVALID` if a text
    breaks the rules."""
    return CreatedResponse(id=unwrap(await use_case.execute(body.name, body.abbreviation)))


@admin_concentrations.patch(
    "/{concentration_id}",
    status_code=204,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
async def update_concentration(
    concentration_id: UUID,
    body: UpdateConcentrationRequest,
    use_case: Annotated[UpdateConcentration, Depends(provide(UpdateConcentration))],
) -> None:
    """Change name and abbreviation together; their slugs follow. 404
    `CATALOG_CONCENTRATION_NOT_FOUND`, 409 `CATALOG_CONCENTRATION_ALREADY_EXISTS` or
    `CATALOG_CONCENTRATION_ABBREVIATION_TAKEN` if another concentration has the slug, 422
    `CATALOG_CONCENTRATION_NAME_INVALID` or `CATALOG_CONCENTRATION_ABBREVIATION_INVALID`."""
    unwrap(await use_case.execute(concentration_id, body.name, body.abbreviation))


@admin_concentrations.post(
    "/{concentration_id}/archive", status_code=204, responses={404: {"model": ErrorResponse}}
)
async def archive_concentration(
    concentration_id: UUID,
    use_case: Annotated[ArchiveConcentration, Depends(provide(ArchiveConcentration))],
) -> None:
    """Hide a concentration from the storefront (idempotent). 404
    `CATALOG_CONCENTRATION_NOT_FOUND`."""
    unwrap(await use_case.execute(concentration_id))


@admin_concentrations.post(
    "/{concentration_id}/restore", status_code=204, responses={404: {"model": ErrorResponse}}
)
async def restore_concentration(
    concentration_id: UUID,
    use_case: Annotated[RestoreConcentration, Depends(provide(RestoreConcentration))],
) -> None:
    """Show an archived concentration again (idempotent). 404 `CATALOG_CONCENTRATION_NOT_FOUND`."""
    unwrap(await use_case.execute(concentration_id))


routers = (
    public,
    admin,
    public_families,
    admin_families,
    public_concentrations,
    admin_concentrations,
)
