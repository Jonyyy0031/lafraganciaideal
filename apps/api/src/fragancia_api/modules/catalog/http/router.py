from typing import Annotated

from fastapi import Depends, Query

from fragancia_api.modules.catalog.application.commands.create_brand import CreateBrand
from fragancia_api.modules.catalog.application.queries.list_brands import (
    ListAdminBrands,
    ListPublicBrands,
)
from fragancia_api.modules.catalog.contracts import (
    AdminBrandPage,
    CreateBrandRequest,
    CreatedResponse,
    PublicBrand,
)
from fragancia_api.shared.contracts import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from fragancia_api.shared.http import ErrorResponse, admin_router, provide, public_router, unwrap

public = public_router(prefix="/brands", tags=["catalog"])
admin = admin_router(prefix="/brands", tags=["catalog · admin"])


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
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> AdminBrandPage:
    """Every brand (active or not), ordered by name."""
    return await use_case.execute(page=page, size=size)


routers = (public, admin)
