"""In-memory catalog import adapters for unit tests. They honor the same contracts as the real
ones (without locking: tests run one claim at a time)."""

from collections.abc import Collection, Mapping
from copy import deepcopy
from datetime import datetime
from uuid import UUID

from fragancia_api.modules.catalog.application.ports import (
    ExistingPerfume,
    PerfumeIdentity,
    ReferenceItem,
    SheetRows,
)
from fragancia_api.modules.catalog.contracts import (
    CatalogImportIssue,
    CatalogImportResponse,
    CatalogImportSummary,
)
from fragancia_api.modules.catalog.domain.catalog_import import (
    CatalogImport,
    ImportIssue,
    ImportStatus,
)
from fragancia_api.modules.catalog.domain.errors import ImportImageUnreachable
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class FakeSheetReader:
    """Returns the given rows (or file-level issue) and records each call."""

    def __init__(self, result: SheetRows | ImportIssue) -> None:
        self.result = result
        self.calls: list[tuple[bytes, int]] = []

    async def read(self, data: bytes, *, max_rows: int) -> Result[SheetRows, ImportIssue]:
        self.calls.append((data, max_rows))
        if isinstance(self.result, ImportIssue):
            return Err(self.result)
        return Ok(self.result)


class FakeImageDownloader:
    """Returns bytes or an error per URL; an unknown URL is unreachable. Records each call."""

    def __init__(self, responses: Mapping[str, bytes | DomainError] | None = None) -> None:
        self.responses: dict[str, bytes | DomainError] = dict(responses or {})
        self.calls: list[str] = []

    async def fetch(self, url: str) -> Result[bytes, DomainError]:
        self.calls.append(url)
        response = self.responses.get(url)
        if response is None:
            return Err(ImportImageUnreachable())
        if isinstance(response, DomainError):
            return Err(response)
        return Ok(response)


class InMemoryImportReferences:
    """Given brands, families, concentrations and stored perfumes (by identity)."""

    def __init__(
        self,
        *,
        brands: list[ReferenceItem] | None = None,
        families: list[ReferenceItem] | None = None,
        concentrations: list[ReferenceItem] | None = None,
        perfumes: Mapping[PerfumeIdentity, ExistingPerfume] | None = None,
    ) -> None:
        self.brand_items = list(brands or [])
        self.family_items = list(families or [])
        self.concentration_items = list(concentrations or [])
        self.perfumes: dict[PerfumeIdentity, ExistingPerfume] = dict(perfumes or {})
        self.existing_calls: list[list[PerfumeIdentity]] = []

    async def brands(self) -> list[ReferenceItem]:
        return list(self.brand_items)

    async def families(self) -> list[ReferenceItem]:
        return list(self.family_items)

    async def concentrations(self) -> list[ReferenceItem]:
        return list(self.concentration_items)

    async def existing_perfumes(
        self, identities: Collection[PerfumeIdentity]
    ) -> dict[PerfumeIdentity, ExistingPerfume]:
        self.existing_calls.append(list(identities))
        return {key: value for key, value in self.perfumes.items() if key in identities}


class InMemoryCatalogImports:
    """Both the repository and the queries over one store. Like the SQL adapter, it stores
    and hands out copies: a mutation is only kept by `add` or `save`."""

    def __init__(self, *imports: CatalogImport) -> None:
        self.by_id: dict[UUID, CatalogImport] = {item.id: deepcopy(item) for item in imports}

    async def add(self, catalog_import: CatalogImport) -> None:
        self.by_id[catalog_import.id] = deepcopy(catalog_import)

    async def get_for_update(self, import_id: UUID) -> CatalogImport | None:
        found = self.by_id.get(import_id)
        return deepcopy(found) if found is not None else None

    async def claim_next_pending(self) -> CatalogImport | None:
        pending = [i for i in self.by_id.values() if i.status == ImportStatus.PENDING]
        if not pending:
            return None
        return deepcopy(min(pending, key=lambda item: item.created_at))

    async def list_stale_validating(self, before: datetime) -> list[CatalogImport]:
        return [
            deepcopy(item)
            for item in self.by_id.values()
            if item.status == ImportStatus.VALIDATING
            and item.started_at is not None
            and item.started_at < before
        ]

    async def list_expired_ready(self, now: datetime) -> list[CatalogImport]:
        return [
            deepcopy(item)
            for item in self.by_id.values()
            if item.status == ImportStatus.READY
            and item.expires_at is not None
            and item.expires_at <= now
        ]

    async def save(self, catalog_import: CatalogImport) -> None:
        self.by_id[catalog_import.id] = deepcopy(catalog_import)

    async def get(self, import_id: UUID) -> CatalogImportResponse | None:
        item = self.by_id.get(import_id)
        if item is None:
            return None
        summary = item.summary
        return CatalogImportResponse(
            id=item.id,
            status=item.status.value,
            created_at=item.created_at,
            finished_at=item.finished_at,
            expires_at=item.expires_at,
            row_count=item.row_count,
            summary=(
                CatalogImportSummary(
                    perfumes_new=summary.perfumes_new,
                    perfumes_updated=summary.perfumes_updated,
                    presentations_new=summary.presentations_new,
                    presentations_updated=summary.presentations_updated,
                    photos_new=summary.photos_new,
                )
                if summary is not None
                else None
            ),
            error_count=item.error_count or 0,
            errors=[
                CatalogImportIssue(
                    row=issue.row, column=issue.column, code=issue.code, message=issue.message
                )
                for issue in item.errors
            ],
        )
