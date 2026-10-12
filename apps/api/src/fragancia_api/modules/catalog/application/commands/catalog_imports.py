"""Commands of the catalog import: upload, validate in the worker, and expire.

The import row is its own job (a database-backed queue): the upload stores the file as a
pending import, a worker cron claims the oldest pending one with `FOR UPDATE SKIP LOCKED`,
validates it outside any transaction and writes the result back in one short transaction.
Object storage is not transactional: staged objects of an invalid or failed validation are
deleted at once, those of an expired import after the commit.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import structlog

from fragancia_api.modules.catalog.application.import_validation import ImportValidator
from fragancia_api.modules.catalog.application.ports import (
    CatalogImportRepository,
    MediaStorage,
    SpreadsheetReader,
)
from fragancia_api.modules.catalog.domain.catalog_import import (
    CatalogImport,
    ImportIssue,
    ImportStatus,
)
from fragancia_api.modules.catalog.domain.errors import (
    ImportFailed,
    ImportFormatInvalid,
    ImportInterrupted,
)
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result

log = structlog.get_logger(__name__)

_ZIP_SIGNATURE = b"PK\x03\x04"  # every .xlsx file is a ZIP archive

type _Change = Callable[[CatalogImport], None]


@dataclass(frozen=True, slots=True)
class ImportUploadLimit:
    """The largest accepted import file, for the HTTP layer to enforce before reading it all."""

    max_bytes: int


async def _delete_quietly(storage: MediaStorage, keys: Sequence[str]) -> None:
    """Delete objects, logging (never raising) a failure: a leftover object is an orphan."""
    if not keys:
        return
    try:
        await storage.delete(keys)
    except Exception:
        log.warning("catalog_import_objects_not_deleted", keys=list(keys), exc_info=True)


class StartCatalogImport:
    """Command: store an uploaded .xlsx file as a pending import for the worker to validate.

    The size limit is checked by the caller, before the bytes reach this command.
    """

    def __init__(
        self, *, imports: CatalogImportRepository, transactions: TransactionRunner, clock: Clock
    ) -> None:
        self._imports = imports
        self._transactions = transactions
        self._clock = clock

    async def execute(self, data: bytes, created_by: str) -> Result[UUID, DomainError]:
        if not data.startswith(_ZIP_SIGNATURE):
            return Err(ImportFormatInvalid())

        async def work() -> Result[UUID, DomainError]:
            catalog_import = CatalogImport.create(data, created_by, self._clock.now())
            await self._imports.add(catalog_import)
            return Ok(catalog_import.id)

        return await self._transactions.run(work)


class ValidateNextCatalogImport:
    """Worker job: validate the oldest pending import. Returns how many it took (0 or 1) and
    never raises: an unexpected error marks the import failed."""

    def __init__(
        self,
        *,
        imports: CatalogImportRepository,
        reader: SpreadsheetReader,
        validator: ImportValidator,
        storage: MediaStorage,
        transactions: TransactionRunner,
        clock: Clock,
        max_rows: int,
        ttl: timedelta,
    ) -> None:
        self._imports = imports
        self._reader = reader
        self._validator = validator
        self._storage = storage
        self._transactions = transactions
        self._clock = clock
        self._max_rows = max_rows
        self._ttl = ttl

    async def run_once(self) -> int:
        async def claim() -> Result[CatalogImport | None, DomainError]:
            catalog_import = await self._imports.claim_next_pending()
            if catalog_import is None:
                return Ok(None)
            catalog_import.start(self._clock.now())
            await self._imports.save(catalog_import)
            return Ok(catalog_import)

        result = await self._transactions.run(claim)
        claimed = result.value if isinstance(result, Ok) else None
        if claimed is None:
            return 0

        uploaded: list[str] = []
        try:
            await self._validate(claimed, uploaded)
        except Exception:
            log.error("catalog_import_failed", import_id=str(claimed.id), exc_info=True)
            await _delete_quietly(self._storage, uploaded)
            await self._fail(claimed.id)
        return 1

    async def _validate(self, claimed: CatalogImport, uploaded: list[str]) -> None:
        file = claimed.file or b""
        match await self._reader.read(file, max_rows=self._max_rows):
            case Err(file_issue):
                await self._save(
                    claimed.id, lambda loaded: self._invalid(loaded, None, [file_issue])
                )
                return
            case Ok(sheet):
                pass
        issues, summary, plan, keys = await self._validator.validate(sheet)
        uploaded.extend(keys)
        row_count = len(sheet.rows)
        if issues or summary is None or plan is None:
            await self._save(claimed.id, lambda loaded: self._invalid(loaded, row_count, issues))
            await _delete_quietly(self._storage, uploaded)
            uploaded.clear()
            return
        await self._save(
            claimed.id,
            lambda loaded: loaded.finish_ready(
                self._clock.now(), self._ttl, row_count, summary, plan
            ),
        )

    def _invalid(
        self, loaded: CatalogImport, row_count: int | None, issues: list[ImportIssue]
    ) -> None:
        loaded.finish_invalid(self._clock.now(), row_count, issues)

    async def _save(self, import_id: UUID, change: _Change) -> None:
        async def work() -> Result[None, DomainError]:
            loaded = await self._imports.get_for_update(import_id)
            if loaded is None:
                raise RuntimeError(f"Import {import_id} disappeared while validating")
            change(loaded)
            await self._imports.save(loaded)
            return Ok(None)

        await self._transactions.run(work)

    async def _fail(self, import_id: UUID) -> None:
        issue = ImportIssue.of(ImportFailed(), row=None, column=None)

        def fail(loaded: CatalogImport) -> None:
            if loaded.status in (ImportStatus.PENDING, ImportStatus.VALIDATING):
                loaded.fail(self._clock.now(), issue)

        try:
            await self._save(import_id, fail)
        except Exception:
            log.error("catalog_import_not_marked_failed", import_id=str(import_id), exc_info=True)


class ExpireCatalogImports:
    """Worker job: fail imports whose validation was interrupted, and expire ready imports
    nobody confirmed (deleting their staged objects after the commit). Returns how many
    imports changed."""

    def __init__(
        self,
        *,
        imports: CatalogImportRepository,
        storage: MediaStorage,
        transactions: TransactionRunner,
        clock: Clock,
        stale_after: timedelta,
    ) -> None:
        self._imports = imports
        self._storage = storage
        self._transactions = transactions
        self._clock = clock
        self._stale_after = stale_after

    async def run_once(self) -> int:
        async def work() -> Result[tuple[int, list[str]], DomainError]:
            now = self._clock.now()
            changed = 0
            for stale in await self._imports.list_stale_validating(now - self._stale_after):
                # Objects it staged may be left as orphans: the documented risk.
                log.warning("catalog_import_interrupted", import_id=str(stale.id))
                stale.fail(now, ImportIssue.of(ImportInterrupted(), row=None, column=None))
                await self._imports.save(stale)
                changed += 1
            keys: list[str] = []
            for expired in await self._imports.list_expired_ready(now):
                keys.extend(expired.expire())
                await self._imports.save(expired)
                changed += 1
            return Ok((changed, keys))

        match await self._transactions.run(work):
            case Err(_):
                return 0
            case Ok((changed, keys)):
                await _delete_quietly(self._storage, keys)
                return changed
