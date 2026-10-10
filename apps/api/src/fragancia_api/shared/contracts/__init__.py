"""Response shapes shared by every module's contracts (Pydantic, no behavior)."""

from pydantic import BaseModel, Field

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_PAGE = 10_000  # keeps (page - 1) × size far inside the SQL OFFSET range


class Page[T](BaseModel):
    """One page of a list. `page` starts at 1; `total` counts every item of the list.

    Contracts subclass it with a concrete name so the OpenAPI schema (and the generated web
    client) reads well: `class AdminBrandPage(Page[AdminBrand]): ...`.
    """

    items: list[T]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    size: int = Field(ge=1, le=MAX_PAGE_SIZE)
