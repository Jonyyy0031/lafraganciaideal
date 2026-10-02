"""Response shapes shared by every module's contracts (Pydantic, no behavior)."""

from pydantic import BaseModel, Field

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class Page[T](BaseModel):
    """One page of a list. `page` starts at 1; `total` counts every item of the list."""

    items: list[T]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    size: int = Field(ge=1, le=MAX_PAGE_SIZE)
