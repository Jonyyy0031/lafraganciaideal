"""Plan 007: start, validate (worker job), expire, and the import queries (use cases over
in-memory adapters and fakes)."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog.testing

from fragancia_api.modules.catalog.application.commands.catalog_imports import (
    ExpireCatalogImports,
    ImportUploadLimit,
    StartCatalogImport,
    ValidateNextCatalogImport,
)
from fragancia_api.modules.catalog.application.import_validation import (
    ImportValidator,
    ValidationResult,
)
from fragancia_api.modules.catalog.application.ports import SheetRows
from fragancia_api.modules.catalog.application.queries.catalog_imports import (
    DownloadCatalogTemplate,
    GetCatalogImport,
)
from fragancia_api.modules.catalog.domain.catalog_import import (
    CatalogImport,
    ImportIssue,
    ImportStatus,
    ImportSummary,
)
from fragancia_api.modules.catalog.domain.errors import ImportFileUnreadable, ImportTooManyRows
from fragancia_api.modules.catalog.infrastructure.in_memory import (
    InMemoryConcentrations,
    InMemoryOlfactoryFamilies,
)
from fragancia_api.modules.catalog.infrastructure.in_memory_imports import (
    FakeSheetReader,
    InMemoryCatalogImports,
)
from fragancia_api.modules.catalog.infrastructure.in_memory_media import InMemoryMediaStorage
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from fragancia_api.shared.kernel import Ok, Result
from tests.unit.catalog.import_support import (
    PLAN,
    URL_1,
    ValidatorRig,
    make_rig,
    row,
    sheet,
)
from tests.unit.catalog.perfume_support import (
    error_code,
    make_concentration,
    make_family,
    unwrap_ok,
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=60)
STALE_AFTER = timedelta(minutes=30)
XLSX = b"PK\x03\x04 pretend workbook"
MAX_ROWS = 2000


def _clock() -> FixedClock:
    return FixedClock(NOW)


def _pending(
    created_at: datetime = NOW - timedelta(minutes=5), by: str = "admin-1"
) -> CatalogImport:
    return CatalogImport.create(XLSX, by, created_at)


def _validating(started_at: datetime) -> CatalogImport:
    catalog_import = _pending(started_at - timedelta(minutes=1))
    catalog_import.start(started_at)
    return catalog_import


def _ready(expires_at: datetime, keys: tuple[str, ...] = ()) -> CatalogImport:
    catalog_import = _validating(NOW - timedelta(hours=2))
    catalog_import.finish_ready(expires_at - TTL, TTL, 1, ImportSummary(1, 0, 1, 0, 0), PLAN)
    return catalog_import


class _Tracking(InMemoryTransactionRunner):
    """Tells whether code runs inside a transaction."""

    def __init__(self) -> None:
        self.inside = False
        self.entered = 0

    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
        self.inside = True
        self.entered += 1
        try:
            return await work()
        finally:
            self.inside = False


# --- StartCatalogImport -----------------------------------------------------------------------


async def test_start_stores_a_pending_import_with_the_file_the_actor_and_the_clock() -> None:
    store = InMemoryCatalogImports()
    command = StartCatalogImport(
        imports=store, transactions=InMemoryTransactionRunner(), clock=_clock()
    )

    import_id = unwrap_ok(await command.execute(XLSX, "admin-7"))

    stored = store.by_id[import_id]
    assert stored.status is ImportStatus.PENDING
    assert (stored.file, stored.created_by, stored.created_at) == (XLSX, "admin-7", NOW)


@pytest.mark.parametrize(
    "data", [b"", b"not a zip", b"%PDF-1.7", b"PK", b"PK\x05\x06", b" PK\x03\x04"]
)
async def test_start_rejects_data_that_does_not_begin_with_the_zip_signature(data: bytes) -> None:
    store = InMemoryCatalogImports()
    command = StartCatalogImport(
        imports=store, transactions=InMemoryTransactionRunner(), clock=_clock()
    )

    result = await command.execute(data, "admin-1")

    assert error_code(result) == "CATALOG_IMPORT_FORMAT_INVALID"
    assert store.by_id == {}


async def test_start_only_checks_the_signature_the_worker_judges_the_content() -> None:
    store = InMemoryCatalogImports()
    command = StartCatalogImport(
        imports=store, transactions=InMemoryTransactionRunner(), clock=_clock()
    )

    result = await command.execute(b"PK\x03\x04garbage", "admin-1")

    assert isinstance(result, Ok)


def test_the_upload_limit_carries_the_max_bytes() -> None:
    assert ImportUploadLimit(max_bytes=5).max_bytes == 5


# --- ValidateNextCatalogImport ----------------------------------------------------------------


class _Job:
    def __init__(
        self,
        rig: ValidatorRig,
        reader: Any,
        store: InMemoryCatalogImports,
        transactions: InMemoryTransactionRunner | None = None,
        validator: Any = None,
    ) -> None:
        self.rig = rig
        self.store = store
        self.reader = reader
        self.transactions = transactions or InMemoryTransactionRunner()
        self.command = ValidateNextCatalogImport(
            imports=store,
            reader=reader,
            validator=validator or rig.validator,
            storage=rig.storage,
            transactions=self.transactions,
            clock=_clock(),
            max_rows=MAX_ROWS,
            ttl=TTL,
        )

    def only(self) -> CatalogImport:
        [item] = self.store.by_id.values()
        return item


def _job(
    data: SheetRows | ImportIssue,
    *imports: CatalogImport,
    rig: ValidatorRig | None = None,
    transactions: InMemoryTransactionRunner | None = None,
    validator: Any = None,
) -> _Job:
    return _Job(
        rig or make_rig(downloads={URL_1: b"png"}),
        FakeSheetReader(data),
        InMemoryCatalogImports(*imports),
        transactions,
        validator,
    )


async def test_nothing_pending_returns_zero_and_reads_nothing() -> None:
    job = _job(sheet(row()), _validating(NOW), _ready(NOW + TTL))

    assert await job.command.run_once() == 0

    assert job.reader.calls == []


async def test_an_empty_store_returns_zero() -> None:
    assert await _job(sheet(row())).command.run_once() == 0


async def test_it_takes_the_oldest_pending_import_and_leaves_the_rest() -> None:
    older = _pending(NOW - timedelta(minutes=10))
    newer = _pending(NOW - timedelta(minutes=1))
    job = _job(sheet(row()), newer, older)

    assert await job.command.run_once() == 1

    assert job.store.by_id[older.id].status is ImportStatus.READY
    assert job.store.by_id[newer.id].status is ImportStatus.PENDING
    assert job.reader.calls == [(XLSX, MAX_ROWS)]


async def test_a_valid_file_becomes_ready_with_summary_plan_and_expiry() -> None:
    pending = _pending()
    job = _job(sheet(row(), row(ML=50), row(Nombre="Bleu", Imagen_1=URL_1)), pending)

    assert await job.command.run_once() == 1

    item = job.only()
    assert item.status is ImportStatus.READY
    assert (item.row_count, item.error_count, item.errors) == (3, 0, ())
    assert item.summary == ImportSummary(2, 0, 3, 0, 1)
    assert item.plan is not None
    assert len(item.plan.perfumes) == 2
    assert (item.finished_at, item.expires_at) == (NOW, NOW + TTL)
    assert item.file is None
    # The staged objects stay in the bucket until the import expires or is confirmed.
    assert sorted(job.rig.storage.objects) == sorted(item.plan.staged_keys())
    assert len(job.rig.storage.objects) == 2
    assert job.rig.storage.deleted == []


async def test_a_file_level_issue_makes_the_import_invalid_without_validating_rows() -> None:
    issue = ImportIssue.of(ImportTooManyRows(details={"max_rows": 2000}), row=None, column=None)
    job = _job(issue, _pending())

    assert await job.command.run_once() == 1

    item = job.only()
    assert item.status is ImportStatus.INVALID
    assert item.errors == (issue,)
    assert (item.error_count, item.row_count) == (1, None)
    assert item.file is None
    assert item.finished_at == NOW
    assert job.rig.references.existing_calls == []
    assert job.rig.downloader.calls == []


async def test_an_unreadable_file_is_an_invalid_import_not_a_failed_one() -> None:
    job = _job(ImportIssue.of(ImportFileUnreadable(), row=None, column=None), _pending())

    await job.command.run_once()

    item = job.only()
    assert item.status is ImportStatus.INVALID
    assert item.errors[0].code == "CATALOG_IMPORT_FILE_UNREADABLE"


async def test_row_issues_make_it_invalid_keep_the_report_and_delete_the_staged_objects() -> None:
    bad = row(Nombre="Malo", ML="abc")
    good = row(Nombre="Bueno", Imagen_1=URL_1)  # staged in the bucket, then must be removed
    job = _job(sheet(bad, good), _pending())

    assert await job.command.run_once() == 1

    item = job.only()
    assert item.status is ImportStatus.INVALID
    assert [(e.row, e.column, e.code) for e in item.errors] == [
        (2, "ML", "CATALOG_IMPORT_VALUE_INVALID")
    ]
    assert (item.error_count, item.row_count) == (1, 2)
    assert item.plan is None
    assert item.file is None
    assert job.rig.storage.objects == {}  # nothing left behind
    assert len(job.rig.storage.deleted) == 2


async def test_an_invalid_import_still_succeeds_when_deleting_the_objects_fails() -> None:
    job = _job(sheet(row(Nombre="Malo", ML="abc"), row(Nombre="Bueno", Imagen_1=URL_1)), _pending())
    job.rig.storage.fail_deletes = True

    with structlog.testing.capture_logs() as logs:
        assert await job.command.run_once() == 1

    assert job.only().status is ImportStatus.INVALID
    assert [log["event"] for log in logs] == ["catalog_import_objects_not_deleted"]


async def test_the_import_is_validating_while_the_file_is_read() -> None:
    seen: list[ImportStatus] = []
    store = InMemoryCatalogImports(_pending())

    class Reader:
        async def read(self, data: bytes, *, max_rows: int) -> Result[SheetRows, ImportIssue]:
            seen.extend(item.status for item in store.by_id.values())
            return Ok(sheet(row()))

    job = _Job(make_rig(), Reader(), store)

    await job.command.run_once()

    assert seen == [ImportStatus.VALIDATING]
    assert [item.started_at for item in store.by_id.values()] == [NOW]


async def test_reading_and_validating_run_outside_any_transaction() -> None:
    transactions = _Tracking()
    inside: dict[str, bool] = {}
    rig = make_rig(downloads={URL_1: b"png"})
    store = InMemoryCatalogImports(_pending())

    class Reader:
        async def read(self, data: bytes, *, max_rows: int) -> Result[SheetRows, ImportIssue]:
            inside["read"] = transactions.inside
            return Ok(sheet(row(Imagen_1=URL_1)))

    original = rig.downloader.fetch

    async def fetch(url: str) -> Result[bytes, Any]:
        inside["download"] = transactions.inside
        return await original(url)

    rig.downloader.fetch = fetch  # type: ignore[method-assign]
    job = _Job(rig, Reader(), store, transactions)

    await job.command.run_once()

    assert inside == {"read": False, "download": False}
    assert transactions.entered == 2  # the claim, and the one short transaction with the result


class _ExplodingValidator(ImportValidator):
    def __init__(self, rig: ValidatorRig) -> None:
        super().__init__(
            references=rig.references,
            downloader=rig.downloader,
            images=rig.images,
            storage=rig.storage,
        )

    async def validate(self, data: SheetRows) -> ValidationResult:
        raise RuntimeError("validator blew up")


async def test_an_unexpected_exception_fails_the_import_logs_it_and_never_raises() -> None:
    rig = make_rig()
    job = _job(sheet(row()), _pending(), rig=rig, validator=_ExplodingValidator(rig))

    with structlog.testing.capture_logs() as logs:
        processed = await job.command.run_once()

    assert processed == 1
    item = job.only()
    assert item.status is ImportStatus.FAILED
    assert [(e.row, e.column, e.code) for e in item.errors] == [
        (None, None, "CATALOG_IMPORT_FAILED")
    ]
    assert item.error_count == 1
    assert item.file is None
    [entry] = [log for log in logs if log["event"] == "catalog_import_failed"]
    assert entry["log_level"] == "error"
    assert entry["import_id"] == str(item.id)
    assert entry["exc_info"] is True


async def test_an_exception_of_the_reader_also_fails_the_import() -> None:
    store = InMemoryCatalogImports(_pending())

    class Reader:
        async def read(self, data: bytes, *, max_rows: int) -> Result[SheetRows, ImportIssue]:
            raise OSError("disk went away")

    job = _Job(make_rig(), Reader(), store)

    assert await job.command.run_once() == 1

    assert job.only().status is ImportStatus.FAILED


async def test_a_failure_after_staging_deletes_the_uploaded_objects() -> None:
    rig = make_rig(downloads={URL_1: b"png"})
    job = _job(sheet(row(Imagen_1=URL_1)), _pending(), rig=rig)

    async def save_explodes(catalog_import: CatalogImport) -> None:
        if catalog_import.status is ImportStatus.READY:
            raise RuntimeError("database went away")
        job.store.by_id[catalog_import.id] = catalog_import

    original_save = job.store.save
    job.store.save = save_explodes  # type: ignore[method-assign]

    assert await job.command.run_once() == 1

    job.store.save = original_save  # type: ignore[method-assign]
    assert rig.storage.objects == {}
    assert len(rig.storage.deleted) == 2


async def test_when_marking_it_failed_also_fails_the_job_still_does_not_raise() -> None:
    rig = make_rig()
    job = _job(sheet(row()), _pending(), rig=rig, validator=_ExplodingValidator(rig))

    async def gone(import_id: object) -> None:
        return None

    job.store.get_for_update = gone  # type: ignore[method-assign]

    with structlog.testing.capture_logs() as logs:
        assert await job.command.run_once() == 1

    events = [log["event"] for log in logs]
    assert events == ["catalog_import_failed", "catalog_import_not_marked_failed"]


async def test_an_import_the_expiry_job_already_failed_is_left_as_interrupted() -> None:
    """The validation outlived `stale_after`: the other job failed it; saving a result over it
    must not raise `ValueError` nor overwrite the interruption."""
    store = InMemoryCatalogImports(_pending())
    rig = make_rig(downloads={URL_1: b"png"})
    expire = ExpireCatalogImports(
        imports=store,
        storage=rig.storage,
        transactions=InMemoryTransactionRunner(),
        clock=FixedClock(NOW + timedelta(hours=1)),
        stale_after=STALE_AFTER,
    )

    class SlowValidator(ImportValidator):
        async def validate(self, data: SheetRows) -> ValidationResult:
            assert await expire.run_once() == 1  # the other job runs while this one validates
            return await super().validate(data)

    validator = SlowValidator(
        references=rig.references,
        downloader=rig.downloader,
        images=rig.images,
        storage=rig.storage,
    )
    job = _Job(rig, FakeSheetReader(sheet(row(Imagen_1=URL_1))), store, validator=validator)

    with structlog.testing.capture_logs():
        assert await job.command.run_once() == 1

    item = job.only()
    assert item.status is ImportStatus.FAILED
    assert item.errors[0].code == "CATALOG_IMPORT_INTERRUPTED"
    assert rig.storage.objects == {}  # what this validation staged is deleted


# --- ExpireCatalogImports ---------------------------------------------------------------------


class _CommitSnapshot(InMemoryTransactionRunner):
    """Records what the storage had deleted when the transaction finished (the commit point)."""

    def __init__(self, storage: InMemoryMediaStorage) -> None:
        self._storage = storage
        self.deleted_at_commit: list[str] | None = None

    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
        result = await work()
        self.deleted_at_commit = list(self._storage.deleted)
        return result


def _expirer(
    store: InMemoryCatalogImports,
    storage: InMemoryMediaStorage,
    transactions: InMemoryTransactionRunner | None = None,
) -> ExpireCatalogImports:
    return ExpireCatalogImports(
        imports=store,
        storage=storage,
        transactions=transactions or InMemoryTransactionRunner(),
        clock=_clock(),
        stale_after=STALE_AFTER,
    )


async def test_nothing_to_expire_changes_nothing_and_returns_zero() -> None:
    store = InMemoryCatalogImports(_pending(), _validating(NOW), _ready(NOW + timedelta(seconds=1)))
    storage = InMemoryMediaStorage()

    assert await _expirer(store, storage).run_once() == 0

    assert sorted(item.status for item in store.by_id.values()) == sorted(
        [ImportStatus.PENDING, ImportStatus.VALIDATING, ImportStatus.READY]
    )
    assert storage.deleted == []


async def test_a_validation_older_than_thirty_minutes_is_failed_as_interrupted() -> None:
    stale = _validating(NOW - timedelta(minutes=31))
    fresh = _validating(NOW - timedelta(minutes=29))
    store = InMemoryCatalogImports(stale, fresh)

    with structlog.testing.capture_logs() as logs:
        changed = await _expirer(store, InMemoryMediaStorage()).run_once()

    assert changed == 1
    assert store.by_id[fresh.id].status is ImportStatus.VALIDATING
    failed = store.by_id[stale.id]
    assert failed.status is ImportStatus.FAILED
    assert [(e.row, e.column, e.code) for e in failed.errors] == [
        (None, None, "CATALOG_IMPORT_INTERRUPTED")
    ]
    assert (failed.error_count, failed.finished_at, failed.file) == (1, NOW, None)
    [entry] = logs
    assert (entry["event"], entry["log_level"], entry["import_id"]) == (
        "catalog_import_interrupted",
        "warning",
        str(stale.id),
    )


async def test_a_ready_import_past_its_expiry_expires_and_its_objects_are_deleted() -> None:
    expired = _ready(NOW - timedelta(seconds=1))
    assert expired.plan is not None
    keys = expired.plan.staged_keys()
    store = InMemoryCatalogImports(expired)
    storage = InMemoryMediaStorage()
    storage.objects.update({key: b"x" for key in keys})

    assert await _expirer(store, storage).run_once() == 1

    item = store.by_id[expired.id]
    assert item.status is ImportStatus.EXPIRED
    assert item.plan is None
    assert storage.deleted == keys
    assert storage.objects == {}


async def test_the_expiry_instant_itself_is_expired_and_one_second_before_is_not() -> None:
    on_time = _ready(NOW)
    early = _ready(NOW + timedelta(seconds=1))
    store = InMemoryCatalogImports(on_time, early)

    assert await _expirer(store, InMemoryMediaStorage()).run_once() == 1

    assert store.by_id[on_time.id].status is ImportStatus.EXPIRED
    assert store.by_id[early.id].status is ImportStatus.READY


async def test_the_objects_are_deleted_after_the_commit_not_inside_the_transaction() -> None:
    expired = _ready(NOW - timedelta(minutes=1))
    store = InMemoryCatalogImports(expired)
    storage = InMemoryMediaStorage()
    transactions = _CommitSnapshot(storage)

    await _expirer(store, storage, transactions).run_once()

    assert transactions.deleted_at_commit == []
    assert len(storage.deleted) == 6


async def test_a_failure_deleting_the_objects_does_not_undo_the_expiry() -> None:
    expired = _ready(NOW - timedelta(minutes=1))
    store = InMemoryCatalogImports(expired)
    storage = InMemoryMediaStorage(fail_deletes=True)

    with structlog.testing.capture_logs() as logs:
        changed = await _expirer(store, storage).run_once()

    assert changed == 1
    assert store.by_id[expired.id].status is ImportStatus.EXPIRED
    assert [log["event"] for log in logs] == ["catalog_import_objects_not_deleted"]


async def test_stale_and_expired_imports_are_both_handled_and_counted_in_one_run() -> None:
    stale = _validating(NOW - timedelta(hours=1))
    expired = _ready(NOW - timedelta(minutes=1))
    store = InMemoryCatalogImports(stale, expired)

    with structlog.testing.capture_logs():
        assert await _expirer(store, InMemoryMediaStorage()).run_once() == 2

    assert store.by_id[stale.id].status is ImportStatus.FAILED
    assert store.by_id[expired.id].status is ImportStatus.EXPIRED


async def test_an_applied_import_is_never_touched() -> None:
    applied = _ready(NOW - timedelta(minutes=1))
    applied.status = ImportStatus.APPLIED
    store = InMemoryCatalogImports(applied)

    assert await _expirer(store, InMemoryMediaStorage()).run_once() == 0

    assert store.by_id[applied.id].status is ImportStatus.APPLIED


# --- queries ----------------------------------------------------------------------------------


async def test_get_returns_the_import_report_and_not_found_otherwise() -> None:
    ready = _ready(NOW + TTL)
    store = InMemoryCatalogImports(ready)
    query = GetCatalogImport(store)

    found = unwrap_ok(await query.execute(ready.id))
    missing = await query.execute(_pending().id)

    assert found.id == ready.id
    assert found.status == "ready"
    assert found.summary is not None
    assert (found.summary.perfumes_new, found.error_count, found.errors) == (1, 0, [])
    assert error_code(missing) == "CATALOG_IMPORT_NOT_FOUND"


class _RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], list[str]]] = []

    async def build(self, families: list[str], concentrations: list[str]) -> bytes:
        self.calls.append((families, concentrations))
        return b"workbook"


async def test_the_template_is_built_from_the_active_family_and_concentration_names() -> None:
    families = InMemoryOlfactoryFamilies(
        make_family("Floral"), make_family("Aromática"), make_family("Cítrica", active=False)
    )
    concentrations = InMemoryConcentrations(
        make_concentration("Parfum", "PAR"),
        make_concentration("Eau de Toilette", "EDT"),
        make_concentration("Colonia", "COL", active=False),
    )
    writer = _RecordingWriter()

    data = await DownloadCatalogTemplate(families, concentrations, writer).execute()

    assert data == b"workbook"
    assert writer.calls == [(["Aromática", "Floral"], ["Eau de Toilette", "Parfum"])]
