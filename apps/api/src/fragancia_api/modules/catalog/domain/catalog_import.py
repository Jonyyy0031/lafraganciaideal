"""The CatalogImport aggregate: an uploaded .xlsx file, its validation report and its plan.

Lifecycle (any other transition is a programming error and raises `ValueError`):

- `create` → `pending` (the file is stored with the import, which is its own job).
- `start` (pending) → `validating`.
- `finish_ready` (validating) → `ready`, with a summary, the typed plan that confirming applies
  and `expires_at = now + ttl`.
- `finish_invalid` (validating) → `invalid`, with the issues (at most 1,000 kept;
  `error_count` is the full count).
- `fail` (pending or validating) → `failed`, with one file-level issue.
- `expire` (ready) → `expired`; returns the staged object keys and drops the plan.
- `apply` (ready) → `applied`, with `applied_at` and `applied_by`; drops the plan, whose staged
  objects are now live photos, and records `CatalogImportApplied` (the stock counts).
- `discard` (ready) → `discarded`, with `discarded_at`; returns the staged object keys and drops
  the plan.

Every `finish_*` and `fail` sets `finished_at` and drops the file. An import can be confirmed
(`is_confirmable`) only while it is ready and `now < expires_at`; a ready import past
`expires_at` may still be discarded or expired.

Validation stages the photos at their final keys, so the plan carries the final ids of new
perfumes and photos, and `staged_keys` lists every object an unconfirmed import leaves behind.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar, Self
from uuid import UUID

from fragancia_api.modules.catalog.domain.perfume import Gender
from fragancia_api.shared.kernel import AggregateRoot, DomainError, DomainEvent, new_id

ISSUES_MAX = 1000


@dataclass(frozen=True, kw_only=True)
class CatalogImportApplied(DomainEvent):
    """An import was confirmed. `stock_counts` maps presentation id → physical count."""

    name: ClassVar[str] = "catalog.import.applied"
    import_id: UUID
    actor_id: str
    stock_counts: dict[UUID, int]


class ImportStatus(StrEnum):
    PENDING = "pending"
    VALIDATING = "validating"
    READY = "ready"
    INVALID = "invalid"
    FAILED = "failed"
    EXPIRED = "expired"
    APPLIED = "applied"
    DISCARDED = "discarded"


@dataclass(frozen=True, slots=True)
class ImportIssue:
    """One problem of the file. `row` is the Excel row number (the header is row 1); a
    file-level issue has no row."""

    row: int | None
    column: str | None
    code: str
    message: str

    @classmethod
    def of(cls, error: DomainError, *, row: int | None, column: str | None) -> Self:
        return cls(row=row, column=column, code=error.code, message=error.message)


@dataclass(frozen=True, slots=True)
class ImportSummary:
    perfumes_new: int
    perfumes_updated: int
    presentations_new: int
    presentations_updated: int
    photos_new: int
    stock_counts: int = 0


@dataclass(frozen=True, slots=True)
class PlannedPresentation:
    """A presentation to create (`presentation_id` None) or update."""

    presentation_id: UUID | None
    ml: int
    price_cents: int
    sale_price_cents: int | None
    sale_starts_at: datetime | None
    sale_ends_at: datetime | None
    availability: str
    lead_time_min_days: int | None
    lead_time_max_days: int | None
    row: int
    stock_count: int | None = None


@dataclass(frozen=True, slots=True)
class PlannedPhoto:
    """A staged photo at its final keys; it replaces the photo `replaces_photo_id` when set."""

    position: int
    photo_id: UUID
    source_url: str
    card_key: str
    detail_key: str
    width: int
    height: int
    replaces_photo_id: UUID | None


@dataclass(frozen=True, slots=True)
class PlannedPerfume:
    """A perfume to create (`is_new`, with its final id) or update. `publish` None leaves the
    published flag as it is."""

    perfume_id: UUID
    is_new: bool
    brand_id: UUID
    concentration_id: UUID
    family_id: UUID
    name: str
    gender: Gender
    description: str
    top_notes: tuple[str, ...]
    heart_notes: tuple[str, ...]
    base_notes: tuple[str, ...]
    publish: bool | None
    first_row: int
    presentations: tuple[PlannedPresentation, ...]
    photos: tuple[PlannedPhoto, ...]


@dataclass(frozen=True, slots=True)
class ImportPlan:
    perfumes: tuple[PlannedPerfume, ...]

    def staged_keys(self) -> list[str]:
        """Every object key validation uploaded for this plan."""
        return [
            key
            for perfume in self.perfumes
            for photo in perfume.photos
            for key in (photo.card_key, photo.detail_key)
        ]


class CatalogImport(AggregateRoot):
    def __init__(
        self,
        *,
        id: UUID,
        status: ImportStatus,
        file: bytes | None,
        created_by: str,
        created_at: datetime,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        expires_at: datetime | None = None,
        row_count: int | None = None,
        summary: ImportSummary | None = None,
        error_count: int | None = None,
        errors: tuple[ImportIssue, ...] = (),
        plan: ImportPlan | None = None,
        applied_at: datetime | None = None,
        applied_by: str | None = None,
        discarded_at: datetime | None = None,
    ) -> None:
        super().__init__()
        self.id = id
        self.status = status
        self.file = file
        self.created_by = created_by
        self.created_at = created_at
        self.started_at = started_at
        self.finished_at = finished_at
        self.expires_at = expires_at
        self.row_count = row_count
        self.summary = summary
        self.error_count = error_count
        self.errors = errors
        self.plan = plan
        self.applied_at = applied_at
        self.applied_by = applied_by
        self.discarded_at = discarded_at

    @classmethod
    def create(cls, file: bytes, created_by: str, now: datetime) -> CatalogImport:
        """A new pending import holding the uploaded file."""
        return cls(
            id=new_id(),
            status=ImportStatus.PENDING,
            file=file,
            created_by=created_by,
            created_at=now,
        )

    def start(self, now: datetime) -> None:
        self._require(ImportStatus.PENDING)
        self.status = ImportStatus.VALIDATING
        self.started_at = now

    def finish_ready(
        self,
        now: datetime,
        ttl: timedelta,
        row_count: int,
        summary: ImportSummary,
        plan: ImportPlan,
    ) -> None:
        self._require(ImportStatus.VALIDATING)
        self.status = ImportStatus.READY
        self.expires_at = now + ttl
        self.row_count = row_count
        self.summary = summary
        self.plan = plan
        self.error_count = 0
        self.errors = ()
        self._finish(now)

    def finish_invalid(
        self, now: datetime, row_count: int | None, errors: list[ImportIssue]
    ) -> None:
        self._require(ImportStatus.VALIDATING)
        self.status = ImportStatus.INVALID
        self.row_count = row_count
        self.error_count = len(errors)
        self.errors = tuple(errors[:ISSUES_MAX])
        self._finish(now)

    def fail(self, now: datetime, issue: ImportIssue) -> None:
        self._require(ImportStatus.PENDING, ImportStatus.VALIDATING)
        self.status = ImportStatus.FAILED
        self.error_count = 1
        self.errors = (issue,)
        self._finish(now)

    def expire(self) -> list[str]:
        """Expire a ready import; returns the keys of its staged objects to delete."""
        self._require(ImportStatus.READY)
        keys = self.plan.staged_keys() if self.plan is not None else []
        self.status = ImportStatus.EXPIRED
        self.plan = None
        return keys

    def is_confirmable(self, now: datetime) -> bool:
        """Ready and not yet past `expires_at` (the expiry cron may not have run yet)."""
        return (
            self.status == ImportStatus.READY
            and self.expires_at is not None
            and now < self.expires_at
        )

    def apply(self, now: datetime, actor_id: str, stock_counts: Mapping[UUID, int]) -> None:
        """Mark a ready import applied; its staged objects are now live photos.

        Records `CatalogImportApplied` with the physical counts (presentation id → count) that
        inventory turns into count movements."""
        self._require(ImportStatus.READY)
        self.status = ImportStatus.APPLIED
        self.applied_at = now
        self.applied_by = actor_id
        self.plan = None
        self.record(
            CatalogImportApplied(
                import_id=self.id, actor_id=actor_id, stock_counts=dict(stock_counts)
            )
        )

    def discard(self, now: datetime) -> list[str]:
        """Discard a ready import; returns the keys of its staged objects to delete."""
        self._require(ImportStatus.READY)
        keys = self.plan.staged_keys() if self.plan is not None else []
        self.status = ImportStatus.DISCARDED
        self.discarded_at = now
        self.plan = None
        return keys

    def _finish(self, now: datetime) -> None:
        self.finished_at = now
        self.file = None

    def _require(self, *allowed: ImportStatus) -> None:
        if self.status not in allowed:
            raise ValueError(f"Import {self.id} is {self.status}, expected one of {allowed}")
