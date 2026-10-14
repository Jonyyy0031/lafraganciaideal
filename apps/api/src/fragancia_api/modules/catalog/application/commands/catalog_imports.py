"""Commands of the catalog import: upload, validate in the worker, expire, confirm and discard.

The import row is its own job (a database-backed queue): the upload stores the file as a
pending import, a worker cron claims the oldest pending one with `FOR UPDATE SKIP LOCKED`,
validates it outside any transaction and writes the result back in one short transaction.
Object storage is not transactional: staged objects of an invalid or failed validation are
deleted at once, those of an expired or discarded import after the commit, and so are the
objects of photos a confirm replaced.

Confirming applies the plan all or nothing in one transaction: every planned perfume goes
through its aggregate, every refusal is collected as an issue, and with any issue the whole
transaction rolls back and the confirm answers `ImportStale` with the issues.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
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
    PlannedPerfume,
    PlannedPresentation,
)
from fragancia_api.modules.catalog.domain.errors import (
    ImportFailed,
    ImportFormatInvalid,
    ImportInterrupted,
    ImportNotFound,
    ImportNotReady,
    ImportPresentationArchived,
    ImportStale,
    ImportStockPermissionRequired,
    PerfumeBrandUnavailable,
    PerfumeConcentrationUnavailable,
    PerfumeFamilyUnavailable,
    PerfumeNotFound,
    PresentationNotFound,
)
from fragancia_api.modules.catalog.domain.naming import slugify
from fragancia_api.modules.catalog.domain.perfume import (
    Availability,
    AvailabilityKind,
    Description,
    Ml,
    Notes,
    Perfume,
    PerfumeName,
    Price,
    Sale,
    perfume_slug,
)
from fragancia_api.modules.catalog.domain.repositories import (
    BrandRepository,
    ConcentrationRepository,
    OlfactoryFamilyRepository,
    PerfumeRepository,
)
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.events import EventPublisher
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


@dataclass(frozen=True, slots=True)
class _PerfumeValues:
    name: PerfumeName
    description: Description
    notes: Notes


@dataclass(frozen=True, slots=True)
class _PresentationValues:
    ml: Ml
    price: Price
    sale: Sale | None
    availability: Availability


def _perfume_values(planned: PlannedPerfume) -> Result[_PerfumeValues, DomainError]:
    """The value objects of a planned perfume (validated by the import already; an Err here
    still becomes an issue rather than an exception)."""
    match PerfumeName.create(planned.name):
        case Err(invalid_name):
            return Err(invalid_name)
        case Ok(name):
            pass
    match Description.create(planned.description):
        case Err(invalid_description):
            return Err(invalid_description)
        case Ok(description):
            pass
    match Notes.create(
        list(planned.top_notes), list(planned.heart_notes), list(planned.base_notes)
    ):
        case Err(invalid_notes):
            return Err(invalid_notes)
        case Ok(notes):
            pass
    return Ok(_PerfumeValues(name, description, notes))


def _presentation_values(
    planned: PlannedPresentation,
) -> Result[_PresentationValues, DomainError]:
    match Ml.create(planned.ml):
        case Err(invalid_ml):
            return Err(invalid_ml)
        case Ok(ml):
            pass
    match Price.create(planned.price_cents):
        case Err(invalid_price):
            return Err(invalid_price)
        case Ok(price):
            pass
    match Sale.create(
        planned.sale_price_cents, planned.sale_starts_at, planned.sale_ends_at, regular=price
    ):
        case Err(invalid_sale):
            return Err(invalid_sale)
        case Ok(sale):
            pass
    # The plan stores the kind the validation produced from an `Availability`.
    kind = cast(AvailabilityKind, planned.availability)
    match Availability.create(kind, planned.lead_time_min_days, planned.lead_time_max_days):
        case Err(invalid_availability):
            return Err(invalid_availability)
        case Ok(availability):
            pass
    return Ok(_PresentationValues(ml, price, sale, availability))


def _issue(error: DomainError, row: int) -> ImportIssue:
    return ImportIssue.of(error, row=row, column=None)


def _identity_order(planned: PlannedPerfume) -> tuple[str, str, str]:
    """The perfume's identity as a sort key; the same perfume has it in every import."""
    return (str(planned.brand_id), slugify(planned.name), str(planned.concentration_id))


def _issues_details(issues: list[ImportIssue]) -> dict[str, object]:
    return {
        "issues": [
            {"row": issue.row, "column": issue.column, "code": issue.code, "message": issue.message}
            for issue in issues
        ]
    }


type _Counts = list[tuple[UUID, int]]
"""(presentation id, physical count) of the planned presentations that carry a count."""

type _Applied = tuple[list[str], _Counts]
"""What applying one perfume leaves: the keys of the replaced photos and its counts."""


class ConfirmCatalogImport:
    """Command: apply a ready import, all or nothing, in one transaction.

    The plan is a snapshot up to 60 minutes old, so each planned perfume is re-checked against
    the live catalog through its aggregate, with the rules of the panel commands (decision 23
    for the references, the slug recompute, the presentation and photo methods). Every
    refusal is collected as an issue (one per perfume, at its row) and the loop goes on, so
    the admin sees every stale row; with any issue the transaction rolls back and the result
    is `ImportStale`. The objects of replaced photos are deleted after the commit.

    Confirming publishes `catalog.import.applied` through the outbox, in the same transaction;
    inventory turns its stock counts into count movements.
    """

    def __init__(
        self,
        *,
        imports: CatalogImportRepository,
        perfumes: PerfumeRepository,
        brands: BrandRepository,
        families: OlfactoryFamilyRepository,
        concentrations: ConcentrationRepository,
        storage: MediaStorage,
        transactions: TransactionRunner,
        clock: Clock,
        events: EventPublisher,
    ) -> None:
        self._imports = imports
        self._perfumes = perfumes
        self._brands = brands
        self._families = families
        self._concentrations = concentrations
        self._storage = storage
        self._transactions = transactions
        self._clock = clock
        self._events = events

    async def execute(
        self, import_id: UUID, actor_id: str, *, may_record_stock: bool
    ) -> Result[None, DomainError]:
        async def work() -> Result[list[str], DomainError]:
            now = self._clock.now()
            catalog_import = await self._imports.get_for_update(import_id)
            if catalog_import is None:
                return Err(ImportNotFound())
            plan = catalog_import.plan
            if not catalog_import.is_confirmable(now) or plan is None:
                return Err(ImportNotReady())
            # Decision 21, checked under the import lock: the plan cannot change after this.
            if not may_record_stock and any(
                presentation.stock_count is not None
                for perfume in plan.perfumes
                for presentation in perfume.presentations
            ):
                return Err(ImportStockPermissionRequired())
            issues: list[ImportIssue] = []
            old_keys: list[str] = []
            counts: dict[UUID, int] = {}
            # One global lock order, so two confirms cannot deadlock: by identity (brand, name
            # slug, concentration), which two imports share even for a perfume both create
            # (each pre-assigns it a different id).
            for planned in sorted(plan.perfumes, key=_identity_order):
                match await self._apply(planned, now):
                    case Err(issue):
                        issues.append(issue)
                    case Ok((keys, applied_counts)):
                        old_keys.extend(keys)
                        counts.update(applied_counts)
            if issues:  # the runner rolls back every write of the loop
                issues.sort(key=lambda i: (i.row if i.row is not None else 0, i.column or ""))
                return Err(ImportStale(details=_issues_details(issues)))
            catalog_import.apply(now, actor_id, counts)
            await self._imports.save(catalog_import)
            await self._events.publish(catalog_import.pull_events())
            return Ok(old_keys)

        match await self._transactions.run(work):
            case Err(error):
                return Err(error)
            case Ok(keys):
                await _delete_quietly(self._storage, keys)
                return Ok(None)

    async def _apply(self, planned: PlannedPerfume, now: datetime) -> Result[_Applied, ImportIssue]:
        """Write one planned perfume; returns the object keys of the photos it replaced and
        the counts of its presentations."""
        match _perfume_values(planned):
            case Err(invalid):
                return Err(_issue(invalid, planned.first_row))
            case Ok(values):
                pass
        if planned.is_new:
            return await self._create(planned, values, now)
        return await self._update(planned, values, now)

    async def _create(
        self, planned: PlannedPerfume, values: _PerfumeValues, now: datetime
    ) -> Result[_Applied, ImportIssue]:
        row = planned.first_row
        brand = await self._brands.get(planned.brand_id)
        if brand is None or not brand.is_active:
            return Err(_issue(PerfumeBrandUnavailable(), row))
        family = await self._families.get(planned.family_id)
        if family is None or not family.is_active:
            return Err(_issue(PerfumeFamilyUnavailable(), row))
        concentration = await self._concentrations.get(planned.concentration_id)
        if concentration is None or not concentration.is_active:
            return Err(_issue(PerfumeConcentrationUnavailable(), row))
        perfume = Perfume.create(
            id=planned.perfume_id,
            brand_id=planned.brand_id,
            concentration_id=planned.concentration_id,
            family_id=planned.family_id,
            name=values.name,
            slug=perfume_slug(brand.name.value, values.name, concentration.abbreviation.value),
            gender=planned.gender,
            description=values.description,
            notes=values.notes,
            now=now,
        )
        counts: _Counts = []
        for planned_presentation in planned.presentations:
            match _presentation_values(planned_presentation):
                case Err(invalid):
                    return Err(_issue(invalid, planned_presentation.row))
                case Ok(presentation):
                    pass
            match perfume.add_presentation(
                presentation.ml,
                presentation.price,
                presentation.sale,
                presentation.availability,
                created_at=now,
            ):
                case Err(refused):
                    return Err(_issue(refused, planned_presentation.row))
                case Ok(presentation_id):
                    if planned_presentation.stock_count is not None:
                        counts.append((presentation_id, planned_presentation.stock_count))
        for photo in planned.photos:
            match perfume.add_photo(
                photo.photo_id,
                photo.card_key,
                photo.detail_key,
                photo.width,
                photo.height,
                source_url=photo.source_url,
                created_at=now,
            ):
                case Err(refused_photo):
                    return Err(_issue(refused_photo, row))
        if planned.publish is True:
            match perfume.publish(now):
                case Err(refused_publish):
                    return Err(_issue(refused_publish, row))
        match await self._perfumes.add(perfume):  # identity or slug taken meanwhile
            case Err(conflict):
                return Err(_issue(conflict, row))
        return Ok(([], counts))

    async def _update(
        self, planned: PlannedPerfume, values: _PerfumeValues, now: datetime
    ) -> Result[_Applied, ImportIssue]:
        row = planned.first_row
        perfume = await self._perfumes.get_for_update(planned.perfume_id)
        if perfume is None:
            return Err(_issue(PerfumeNotFound(), row))
        # Decision 23, as in UpdatePerfume: only a changed reference must be active; an
        # unchanged one (part of the identity) is kept even when archived.
        brand = await self._brands.get(planned.brand_id)
        if brand is None or (not brand.is_active and planned.brand_id != perfume.brand_id):
            return Err(_issue(PerfumeBrandUnavailable(), row))
        if planned.family_id != perfume.family_id:
            family = await self._families.get(planned.family_id)
            if family is None or not family.is_active:
                return Err(_issue(PerfumeFamilyUnavailable(), row))
        concentration = await self._concentrations.get(planned.concentration_id)
        if concentration is None or (
            not concentration.is_active and planned.concentration_id != perfume.concentration_id
        ):
            return Err(_issue(PerfumeConcentrationUnavailable(), row))
        match perfume.update(
            brand_id=planned.brand_id,
            concentration_id=planned.concentration_id,
            family_id=planned.family_id,
            name=values.name,
            slug=perfume_slug(brand.name.value, values.name, concentration.abbreviation.value),
            gender=planned.gender,
            description=values.description,
            notes=values.notes,
            now=now,
        ):
            case Err(archived):
                return Err(_issue(archived, row))
        counts: _Counts = []
        for planned_presentation in planned.presentations:
            match await self._upsert_presentation(perfume, planned_presentation, now):
                case Err(issue):
                    return Err(issue)
                case Ok(presentation_id):
                    if planned_presentation.stock_count is not None:
                        counts.append((presentation_id, planned_presentation.stock_count))
        old_keys: list[str] = []
        for photo in planned.photos:
            if photo.replaces_photo_id is not None:
                match perfume.replace_photo(
                    photo.replaces_photo_id,
                    photo.photo_id,
                    photo.card_key,
                    photo.detail_key,
                    photo.width,
                    photo.height,
                    source_url=photo.source_url,
                    created_at=now,
                ):
                    case Err(refused_replace):
                        return Err(_issue(refused_replace, row))
                    case Ok(old):
                        old_keys.extend((old.card_key, old.detail_key))
            else:
                match perfume.add_photo(
                    photo.photo_id,
                    photo.card_key,
                    photo.detail_key,
                    photo.width,
                    photo.height,
                    source_url=photo.source_url,
                    created_at=now,
                ):
                    case Err(refused_photo):
                        return Err(_issue(refused_photo, row))
        if planned.publish is True:
            match perfume.publish(now):
                case Err(refused_publish):
                    return Err(_issue(refused_publish, row))
        elif planned.publish is False:
            perfume.hide()
        match await self._perfumes.save(perfume):  # identity, slug or ml taken meanwhile
            case Err(conflict):
                return Err(_issue(conflict, row))
        return Ok((old_keys, counts))

    @staticmethod
    async def _upsert_presentation(
        perfume: Perfume, planned: PlannedPresentation, now: datetime
    ) -> Result[UUID, ImportIssue]:
        """Add or update one presentation; returns its id."""
        match _presentation_values(planned):
            case Err(invalid):
                return Err(_issue(invalid, planned.row))
            case Ok(values):
                pass
        if planned.presentation_id is None:
            match perfume.add_presentation(
                values.ml, values.price, values.sale, values.availability, created_at=now
            ):
                case Err(refused):
                    return Err(_issue(refused, planned.row))
                case Ok(new_presentation_id):
                    return Ok(new_presentation_id)
        stored = next((p for p in perfume.presentations if p.id == planned.presentation_id), None)
        if stored is None:
            return Err(_issue(PresentationNotFound(), planned.row))
        if not stored.is_active:
            return Err(_issue(ImportPresentationArchived(), planned.row))
        match perfume.update_presentation(
            planned.presentation_id,
            values.ml,
            values.price,
            values.sale,
            values.availability,
            now=now,
        ):
            case Err(refused_update):
                return Err(_issue(refused_update, planned.row))
        return Ok(planned.presentation_id)


class DiscardCatalogImport:
    """Command: drop a ready import before it expires (even one already past `expires_at`
    that the cron has not expired yet), deleting its staged objects after the commit."""

    def __init__(
        self,
        *,
        imports: CatalogImportRepository,
        storage: MediaStorage,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._imports = imports
        self._storage = storage
        self._transactions = transactions
        self._clock = clock

    async def execute(self, import_id: UUID) -> Result[None, DomainError]:
        async def work() -> Result[list[str], DomainError]:
            catalog_import = await self._imports.get_for_update(import_id)
            if catalog_import is None:
                return Err(ImportNotFound())
            if catalog_import.status != ImportStatus.READY:
                return Err(ImportNotReady())
            keys = catalog_import.discard(self._clock.now())
            await self._imports.save(catalog_import)
            return Ok(keys)

        match await self._transactions.run(work):
            case Err(error):
                return Err(error)
            case Ok(keys):
                await _delete_quietly(self._storage, keys)
                return Ok(None)
