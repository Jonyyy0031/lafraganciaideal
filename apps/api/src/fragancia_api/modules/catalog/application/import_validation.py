"""Validation of an import sheet into a per-row report, or a typed plan for confirming it.

The algorithm, in order:

1. Headers: missing required columns or duplicated headers stop the validation at once.
2. References are loaded once. Brands and families match by slug; a concentration by its name
   slug, then by its abbreviation slug (decision 65).
3. Every row is checked and collects every issue, not only the first: required cells, cell
   parsing (`import_cells`), the domain value objects and unknown references.
4. Rows are grouped by identity (brand, name slug, concentration). Rows of one perfume must
   agree on the perfume columns (decision 61); an ml appears once per perfume; archived
   perfumes and presentations are errors (decision 68); references follow decision 23 (a new
   perfume needs active ones, an existing one keeps its archived brand and concentration and
   needs an active family only when it changes).
5. Groups without issues get their images: a URL equal to the stored `source_url` of that
   position is skipped (decision 58), a position with a photo is replaced, others append. Each
   URL is downloaded and processed once (at most 4 at a time), and its renditions are
   uploaded to their final keys. A download not started within the image budget, or one that
   raises, becomes `IMAGE_UNREACHABLE` on that cell instead of failing the import.
6. Any issue → the issues and the uploaded keys (the caller deletes them). Otherwise the
   summary, the plan and the keys.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal
from uuid import UUID

import structlog

from fragancia_api.modules.catalog.application.ports import (
    ExistingPerfume,
    ImageDownloader,
    ImageProcessor,
    ImportReferences,
    MediaStorage,
    PerfumeIdentity,
    ProcessedImage,
    ReferenceItem,
    SheetRows,
)
from fragancia_api.modules.catalog.domain import import_cells as cells
from fragancia_api.modules.catalog.domain.catalog_import import (
    ImportIssue,
    ImportPlan,
    ImportSummary,
    PlannedPerfume,
    PlannedPhoto,
    PlannedPresentation,
)
from fragancia_api.modules.catalog.domain.errors import (
    ImportBrandArchived,
    ImportBrandUnknown,
    ImportConcentrationArchived,
    ImportConcentrationUnknown,
    ImportDuplicateRow,
    ImportFamilyArchived,
    ImportFamilyUnknown,
    ImportHeaderDuplicated,
    ImportHeaderMissing,
    ImportImageUnreachable,
    ImportPerfumeArchived,
    ImportPerfumeMismatch,
    ImportPresentationArchived,
    ImportRequired,
    ImportValueInvalid,
)
from fragancia_api.modules.catalog.domain.naming import slugify
from fragancia_api.modules.catalog.domain.perfume import (
    Availability,
    Description,
    Gender,
    Ml,
    Notes,
    PerfumeName,
    Price,
    Sale,
    photo_keys,
)
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result, new_id

log = structlog.get_logger(__name__)

DOWNLOADS_MAX = 4

# The perfume columns every row of one perfume must agree on (decision 61).
_PERFUME_COLUMNS = (
    "Género",
    "Familia olfativa",
    "Descripción",
    "Notas de salida",
    "Notas de corazón",
    "Notas de fondo",
    "Publicado",
    *cells.IMAGE_COLUMNS,
)
_NOTE_COLUMNS = ("Notas de salida", "Notas de corazón", "Notas de fondo")
# Columns parsed from their raw cell; every other column is read as text first.
_RAW_COLUMNS = frozenset(
    (
        "ML",
        "Precio",
        "Precio oferta",
        "Oferta desde",
        "Oferta hasta",
        "Entrega mín. (días)",
        "Entrega máx. (días)",
    )
)

type ValidationResult = tuple[list[ImportIssue], ImportSummary | None, ImportPlan | None, list[str]]


@dataclass(slots=True)
class _Row:
    """One data row with what could be parsed from it (None where a cell failed)."""

    number: int
    issues: list[ImportIssue] = field(default_factory=list)
    brand: ReferenceItem | None = None
    concentration: ReferenceItem | None = None
    family: ReferenceItem | None = None
    name: PerfumeName | None = None
    gender: Gender | None = None
    description: Description | None = None
    notes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    ml: int | None = None
    price: Price | None = None
    sale: Sale | None = None
    availability: Availability | None = None
    publish: bool | None = None
    images: list[str | None] = field(default_factory=lambda: [None, None, None])
    comparable: dict[str, object] = field(default_factory=dict)

    def add(self, error: DomainError, column: str | None) -> None:
        self.issues.append(ImportIssue.of(error, row=self.number, column=column))


@dataclass(slots=True)
class _Group:
    """The rows of one perfume; `perfume_id` is the stored id, or a new one given up front."""

    rows: list[_Row]
    existing: ExistingPerfume | None
    perfume_id: UUID
    photos: list[PlannedPhoto] = field(default_factory=list)

    @property
    def first(self) -> _Row:
        return self.rows[0]

    def has_issues(self) -> bool:
        return any(row.issues for row in self.rows)


@dataclass(frozen=True, slots=True)
class _PhotoJob:
    group: _Group
    image_number: int  # N of "Imagen N"
    position: int
    url: str
    replaces_photo_id: UUID | None


@dataclass(frozen=True, slots=True)
class _References:
    brands: dict[str, ReferenceItem]
    families: dict[str, ReferenceItem]
    concentrations: dict[str, ReferenceItem]
    concentration_abbreviations: dict[str, ReferenceItem]

    def concentration(self, slug: str) -> ReferenceItem | None:
        return self.concentrations.get(slug) or self.concentration_abbreviations.get(slug)


def _is_empty(cell: cells.Cell) -> bool:
    return cell is None or (isinstance(cell, str) and cell.strip() == "")


def _column_order(column: str | None) -> int:
    if column is None:
        return -1
    if column in cells.COLUMNS:
        return cells.COLUMNS.index(column)
    return len(cells.COLUMNS)


def _sorted(issues: list[ImportIssue]) -> list[ImportIssue]:
    return sorted(issues, key=lambda issue: (issue.row or 0, _column_order(issue.column)))


class ImportValidator:
    def __init__(
        self,
        *,
        references: ImportReferences,
        downloader: ImageDownloader,
        images: ImageProcessor,
        storage: MediaStorage,
        image_budget: timedelta,
    ) -> None:
        self._references = references
        self._downloader = downloader
        self._images = images
        self._storage = storage
        # Downloads not started within this time become issues, so a file with slow image
        # hosts still gets a full report before the job's timeout.
        self._image_budget = image_budget

    async def validate(self, sheet: SheetRows) -> ValidationResult:
        header_issues = self._check_headers(sheet)
        if header_issues:
            return header_issues, None, None, []

        references = await self._load_references()
        rows = [self._parse_row(number, row_cells, references) for number, row_cells in sheet.rows]
        by_identity = self._group(rows)
        existing = (
            await self._references.existing_perfumes(list(by_identity)) if by_identity else {}
        )
        groups: list[_Group] = []
        for identity, group_rows in by_identity.items():
            stored = existing.get(identity)
            group = _Group(
                rows=group_rows,
                existing=stored,
                perfume_id=stored.id if stored is not None else new_id(),
            )
            self._check_group(group)
            groups.append(group)

        uploaded: list[str] = []
        await self._stage_photos([group for group in groups if not group.has_issues()], uploaded)

        issues = _sorted([issue for row in rows for issue in row.issues])
        if issues:
            return issues, None, None, uploaded
        summary, plan = self._plan(groups)
        return [], summary, plan, uploaded

    # 1. Headers

    def _check_headers(self, sheet: SheetRows) -> list[ImportIssue]:
        issues: list[ImportIssue] = []
        found = set(sheet.headers)
        missing = [c for c in cells.REQUIRED_COLUMNS if cells.header_key(c) not in found]
        if missing:
            issues.append(
                ImportIssue.of(
                    ImportHeaderMissing(details={"columns": missing}),
                    row=None,
                    column=", ".join(missing),
                )
            )
        if sheet.duplicated:
            duplicated = [
                next((c for c in cells.COLUMNS if cells.header_key(c) == key), key)
                for key in sheet.duplicated
            ]
            issues.append(
                ImportIssue.of(
                    ImportHeaderDuplicated(details={"columns": duplicated}),
                    row=None,
                    column=", ".join(duplicated),
                )
            )
        return issues

    # 2. References

    async def _load_references(self) -> _References:
        brands = await self._references.brands()
        families = await self._references.families()
        concentrations = await self._references.concentrations()
        return _References(
            brands={item.slug: item for item in brands},
            families={item.slug: item for item in families},
            concentrations={item.slug: item for item in concentrations},
            concentration_abbreviations={
                item.alt_slug: item for item in concentrations if item.alt_slug is not None
            },
        )

    # 3. Rows

    def _parse_row(
        self, number: int, row_cells: dict[str, cells.Cell], references: _References
    ) -> _Row:
        row = _Row(number=number)
        raw: dict[str, cells.Cell] = {
            column: row_cells.get(cells.header_key(column)) for column in cells.COLUMNS
        }
        texts: dict[str, str] = {}
        for column in cells.COLUMNS:
            if column in cells.REQUIRED_COLUMNS and _is_empty(raw[column]):
                row.add(ImportRequired(), column)
                continue
            if column in _RAW_COLUMNS:
                continue
            match cells.text(raw[column]):
                case Err(invalid):
                    row.add(invalid, column)
                case Ok(value):
                    texts[column] = value

        self._parse_perfume(row, texts, references)
        self._parse_presentation(row, raw, texts)
        row.comparable = {
            column: self._comparable(column, texts.get(column)) for column in _PERFUME_COLUMNS
        }
        return row

    def _parse_perfume(self, row: _Row, texts: dict[str, str], references: _References) -> None:
        if (brand := texts.get("Marca")) is not None:
            row.brand = references.brands.get(slugify(brand))
            if row.brand is None:
                row.add(ImportBrandUnknown(), "Marca")
        if (name := texts.get("Nombre")) is not None:
            row.name = _ok_or_issue(PerfumeName.create(name), row, "Nombre")
        if (concentration := texts.get("Concentración")) is not None:
            row.concentration = references.concentration(slugify(concentration))
            if row.concentration is None:
                row.add(ImportConcentrationUnknown(), "Concentración")
        if (gender := texts.get("Género")) is not None:
            parsed_gender = _ok_or_issue(cells.gender(gender), row, "Género")
            row.gender = Gender(parsed_gender) if parsed_gender is not None else None
        if (family := texts.get("Familia olfativa")) is not None:
            row.family = references.families.get(slugify(family))
            if row.family is None:
                row.add(ImportFamilyUnknown(), "Familia olfativa")
        if (description := texts.get("Descripción")) is not None:
            row.description = _ok_or_issue(Description.create(description), row, "Descripción")
        for column in _NOTE_COLUMNS:
            if (value := texts.get(column)) is not None:
                parsed = _ok_or_issue(Notes.create(cells.notes(value), [], []), row, column)
                if parsed is not None:
                    row.notes[column] = parsed.top
        if (publish := texts.get("Publicado")) is not None:
            match cells.published(publish):
                case Err(invalid):
                    row.add(invalid, "Publicado")
                case Ok(flag):
                    row.publish = flag
        for index, column in enumerate(cells.IMAGE_COLUMNS):
            if (url := texts.get(column)) is not None:
                match cells.image_url(url):
                    case Err(invalid_url):
                        row.add(invalid_url, column)
                    case Ok(parsed_url):
                        row.images[index] = parsed_url

    def _parse_presentation(
        self, row: _Row, raw: dict[str, cells.Cell], texts: dict[str, str]
    ) -> None:
        if not _is_empty(raw["ML"]):
            ml = _ok_or_issue(cells.whole_number(raw["ML"]), row, "ML")
            if ml is not None and _ok_or_issue(Ml.create(ml), row, "ML") is not None:
                row.ml = ml
        if not _is_empty(raw["Precio"]):
            cents = _ok_or_issue(cells.pesos_to_cents(raw["Precio"]), row, "Precio")
            if cents is not None:
                row.price = _ok_or_issue(Price.create(cents), row, "Precio")

        sale_cents = _optional(raw["Precio oferta"], cells.pesos_to_cents, row, "Precio oferta")
        starts = _optional(raw["Oferta desde"], cells.sale_day, row, "Oferta desde")
        ends = _optional(raw["Oferta hasta"], cells.sale_day, row, "Oferta hasta")
        if (
            row.price is not None
            and sale_cents is not None
            and starts is not None
            and ends is not None
        ):
            starts_at, ends_at = cells.sale_window(starts.value, ends.value)
            match Sale.create(sale_cents.value, starts_at, ends_at, regular=row.price):
                case Err(invalid_sale):
                    row.add(invalid_sale, "Precio oferta")
                case Ok(sale):
                    row.sale = sale

        min_days = _optional(
            raw["Entrega mín. (días)"], cells.whole_number, row, "Entrega mín. (días)"
        )
        max_days = _optional(
            raw["Entrega máx. (días)"], cells.whole_number, row, "Entrega máx. (días)"
        )
        kind: Literal["in_stock", "made_to_order"] | None = None
        if (availability := texts.get("Disponibilidad")) is not None:
            match cells.availability(availability):
                case Err(invalid):
                    row.add(invalid, "Disponibilidad")
                case Ok("made_to_order"):
                    kind = "made_to_order"
                case Ok(_):
                    kind = "in_stock"
        if kind is not None and min_days is not None and max_days is not None:
            row.availability = _ok_or_issue(
                Availability.create(kind, min_days.value, max_days.value), row, "Disponibilidad"
            )

    @staticmethod
    def _comparable(column: str, value: str | None) -> object:
        if value is None:
            return None
        if column in _NOTE_COLUMNS:
            return tuple(cells.notes(value))
        if column in ("Género", "Familia olfativa", "Publicado"):
            return slugify(value)
        return value

    # 4. Groups

    @staticmethod
    def _group(rows: list[_Row]) -> dict[PerfumeIdentity, list[_Row]]:
        """The rows with a resolved identity, by identity, in file order."""
        groups: dict[PerfumeIdentity, list[_Row]] = {}
        for row in rows:
            if row.brand is None or row.name is None or row.concentration is None:
                continue
            identity = (row.brand.id, row.name.slug, row.concentration.id)
            groups.setdefault(identity, []).append(row)
        return groups

    def _check_group(self, group: _Group) -> None:
        first = group.first
        existing = group.existing
        seen_ml: set[int] = set()
        for row in group.rows:
            if row is not first:
                for column in _PERFUME_COLUMNS:
                    if row.comparable[column] != first.comparable[column]:
                        row.add(ImportPerfumeMismatch(), column)
            if row.ml is not None:
                if row.ml in seen_ml:
                    row.add(ImportDuplicateRow(), "ML")
                seen_ml.add(row.ml)
            self._check_archived(row, existing)

    @staticmethod
    def _check_archived(row: _Row, existing: ExistingPerfume | None) -> None:
        if existing is None:
            # A new perfume needs an active brand, family and concentration (decision 23).
            if row.brand is not None and not row.brand.is_active:
                row.add(ImportBrandArchived(), "Marca")
            if row.concentration is not None and not row.concentration.is_active:
                row.add(ImportConcentrationArchived(), "Concentración")
            if row.family is not None and not row.family.is_active:
                row.add(ImportFamilyArchived(), "Familia olfativa")
            return
        if existing.is_archived:
            row.add(ImportPerfumeArchived(), "Nombre")
        # An existing perfume keeps its brand and concentration; a changed family must be active.
        if (
            row.family is not None
            and not row.family.is_active
            and row.family.id != existing.family_id
        ):
            row.add(ImportFamilyArchived(), "Familia olfativa")
        if row.ml is not None and row.ml in existing.presentations:
            _, is_active = existing.presentations[row.ml]
            if not is_active:
                row.add(ImportPresentationArchived(), "ML")

    # 5. Images

    @staticmethod
    def _photo_jobs(group: _Group) -> list[_PhotoJob]:
        existing_photos = group.existing.photos if group.existing is not None else {}
        next_position = len(existing_photos) + 1
        jobs: list[_PhotoJob] = []
        for number, url in enumerate(group.first.images, start=1):
            if url is None:
                continue
            current = existing_photos.get(number)
            if current is not None:
                photo_id, source_url = current
                if source_url == url:
                    continue  # the same URL as the stored photo (decision 58)
                jobs.append(_PhotoJob(group, number, number, url, photo_id))
            else:
                jobs.append(_PhotoJob(group, number, next_position, url, None))
                next_position += 1
        return jobs

    async def _stage_photos(self, groups: list[_Group], uploaded: list[str]) -> None:
        by_url: dict[str, list[_PhotoJob]] = {}
        for group in groups:
            for job in self._photo_jobs(group):
                by_url.setdefault(job.url, []).append(job)
        if not by_url:
            return
        semaphore = asyncio.Semaphore(DOWNLOADS_MAX)
        deadline = time.monotonic() + self._image_budget.total_seconds()

        async def stage(url: str, jobs: list[_PhotoJob]) -> None:
            async with semaphore:
                if time.monotonic() >= deadline:  # out of time: no download, a full report
                    result: Result[ProcessedImage, DomainError] = Err(ImportImageUnreachable())
                else:
                    result = await self._fetch_and_process(url)
                match result:
                    case Err(error):
                        for job in jobs:
                            job.group.first.add(error, f"Imagen {job.image_number}")
                    case Ok(image):
                        for job in jobs:
                            await self._upload(job, image, uploaded)

        try:
            async with asyncio.TaskGroup() as tasks:
                for url, jobs in by_url.items():
                    tasks.create_task(stage(url, jobs))
        except BaseException:
            # The caller never sees these keys when this raises: clean up here.
            await _delete_quietly(self._storage, uploaded)
            raise
        for group in groups:
            group.photos.sort(key=lambda photo: photo.position)

    async def _fetch_and_process(self, url: str) -> Result[ProcessedImage, DomainError]:
        """Downloads and processes one URL. An unexpected exception becomes an issue on that
        cell, never a failed import."""
        try:
            match await self._downloader.fetch(url):
                case Err(unreachable):
                    return Err(unreachable)
                case Ok(data):
                    pass
            match await self._images.process(data):
                case Err(invalid):
                    return Err(invalid)
                case Ok(image):
                    return Ok(image)
        except Exception:
            log.warning("catalog_import_image_error", exc_info=True)
            return Err(ImportImageUnreachable())

    async def _upload(self, job: _PhotoJob, image: ProcessedImage, uploaded: list[str]) -> None:
        photo_id = new_id()
        card_key, detail_key = photo_keys(job.group.perfume_id, photo_id)
        uploaded.extend([card_key, detail_key])  # before the puts: a partial upload is deleted
        await self._storage.put(card_key, image.card, "image/webp")
        await self._storage.put(detail_key, image.detail, "image/webp")
        job.group.photos.append(
            PlannedPhoto(
                position=job.position,
                photo_id=photo_id,
                source_url=job.url,
                card_key=card_key,
                detail_key=detail_key,
                width=image.width,
                height=image.height,
                replaces_photo_id=job.replaces_photo_id,
            )
        )

    # 6. Result

    @staticmethod
    def _plan(groups: list[_Group]) -> tuple[ImportSummary, ImportPlan]:
        perfumes: list[PlannedPerfume] = []
        presentations_new = presentations_updated = photos_new = 0
        for group in groups:
            first = group.first
            existing = group.existing
            presentations: list[PlannedPresentation] = []
            for row in group.rows:
                # A row without issues has every required value: `_required` never raises here.
                ml = _required(row.ml)
                availability = _required(row.availability)
                stored = existing.presentations.get(ml) if existing is not None else None
                if stored is None:
                    presentations_new += 1
                else:
                    presentations_updated += 1
                sale = row.sale
                presentations.append(
                    PlannedPresentation(
                        presentation_id=stored[0] if stored is not None else None,
                        ml=ml,
                        price_cents=_required(row.price).amount.cents,
                        sale_price_cents=sale.price.cents if sale is not None else None,
                        sale_starts_at=sale.starts_at if sale is not None else None,
                        sale_ends_at=sale.ends_at if sale is not None else None,
                        availability=availability.kind,
                        lead_time_min_days=availability.min_days,
                        lead_time_max_days=availability.max_days,
                        row=row.number,
                    )
                )
            photos_new += len(group.photos)
            perfumes.append(
                PlannedPerfume(
                    perfume_id=group.perfume_id,
                    is_new=existing is None,
                    brand_id=_required(first.brand).id,
                    concentration_id=_required(first.concentration).id,
                    family_id=_required(first.family).id,
                    name=_required(first.name).value,
                    gender=_required(first.gender),
                    description=_required(first.description).value,
                    top_notes=first.notes.get("Notas de salida", ()),
                    heart_notes=first.notes.get("Notas de corazón", ()),
                    base_notes=first.notes.get("Notas de fondo", ()),
                    publish=first.publish,
                    first_row=first.number,
                    presentations=tuple(presentations),
                    photos=tuple(group.photos),
                )
            )
        summary = ImportSummary(
            perfumes_new=sum(1 for group in groups if group.existing is None),
            perfumes_updated=sum(1 for group in groups if group.existing is not None),
            presentations_new=presentations_new,
            presentations_updated=presentations_updated,
            photos_new=photos_new,
        )
        return summary, ImportPlan(perfumes=tuple(perfumes))


def _ok_or_issue[T](result: Result[T, DomainError], row: _Row, column: str) -> T | None:
    match result:
        case Err(error):
            row.add(error, column)
            return None
        case Ok(value):
            return value


def _optional[T](
    cell: cells.Cell,
    parse: Callable[[cells.Cell], Result[T, ImportValueInvalid]],
    row: _Row,
    column: str,
) -> Ok[T | None] | None:
    """`Ok(None)` for an empty cell, `Ok(value)` when it parses, None after an issue."""
    if _is_empty(cell):
        return Ok(None)
    match parse(cell):
        case Err(error):
            row.add(error, column)
            return None
        case Ok(value):
            return Ok(value)


def _required[T](value: T | None) -> T:
    if value is None:
        raise RuntimeError("A row without issues lacks a required value")
    return value


async def _delete_quietly(storage: MediaStorage, keys: list[str]) -> None:
    """Delete staged objects, logging (never raising) a failure: a leftover is an orphan."""
    if not keys:
        return
    try:
        await storage.delete(keys)
    except Exception:
        log.warning("catalog_import_objects_not_deleted", keys=list(keys), exc_info=True)
