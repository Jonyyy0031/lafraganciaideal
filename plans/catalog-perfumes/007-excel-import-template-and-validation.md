---
status: testing
module: catalog
min_implementer: high
depends_on: ["003", "006"]
---

# 007 — Excel import: template, upload and background validation

## Context

The catalog can already be maintained one perfume at a time:

- **Perfumes:** the `Perfume` aggregate with its presentations and photos
  (`apps/api/src/fragancia_api/modules/catalog/domain/perfume.py:326-587`).
- **Commands:** create and update (`application/commands/perfumes.py:64-191`), presentations
  (`application/commands/presentations.py:332-407`) and photos
  (`application/commands/photos.py:396-461`).
- **Photo pipeline:** `ImageProcessor` and `MediaStorage` (`application/ports.py:249-274`),
  implemented by `PillowImageProcessor` and `S3MediaStorage`
  (`infrastructure/s3_media_storage.py:11-30`). Object keys come from `photo_keys`
  (`domain/perfume.py:297-300`).
- **Worker:** it runs only the outbox relay cron (`main/worker.py:42-56`). No module can
  schedule its own job yet: `AppModule` has only `name`, `register` and `routers`
  (`shared/module.py:33-38`).

This plan delivers the first half of the Excel import (decisions 13–16 and 57–69 of the
initiative README):

- the CI integration job gains Mailpit (decision 57);
- a downloadable `.xlsx` template;
- a raw-body upload that stores the file as a **pending import**;
- a worker job that validates the whole file into a per-row report, downloads and processes
  the image URLs that changed, and stages their renditions in S3;
- a status route the panel polls;
- a job that expires unconfirmed imports and deletes their staged objects.

Confirming, which applies the import, is plan 008. This plan stores everything 008 needs as a
typed **import plan**.

**Approaches compared.**

1. **Process in the HTTP request.** Rejected by decision 63: 2,000 rows with up to three
   image downloads per perfume would take minutes.
2. **arq enqueue from the API.** It needs an arq pool in the container and a Valkey payload
   for a 5 MB file.
3. **Chosen: a database-backed queue.** The import row *is* the job:
   - The upload inserts a `pending` row holding the file (`bytea`).
   - A worker cron claims the oldest pending row with `FOR UPDATE SKIP LOCKED`, the same way
     the outbox relay claims rows (`shared/infrastructure/outbox.py:103-130`).
   - The validation runs outside any transaction.
   - The result is written back in one short transaction.

   This needs no queue client in the API. The file is stored in the same database, and
   restarts are safe.

**Staging photos.** To spare 008 any object copies, validation gives every new perfume and
every new photo its final id up front. It uploads the renditions straight to their final
keys, `photo_keys(perfume_id, photo_id)`, so 008 only writes rows. The cost: an import that
never gets confirmed leaves public objects under unguessable UUID keys until the expiry job
deletes them.

**Image downloads are SSRF-hardened.** The URLs come from a spreadsheet, so:

- Every resolved address must be public (`ipaddress.ip_address(...).is_global`).
- The request connects to the checked IP itself, so the address cannot change between the
  check and the connection (DNS rebinding). It sends the original `Host` header and the TLS
  SNI and verifies the certificate against the hostname:
  - httpcore takes the server hostname from the `sni_hostname` request extension
    (`.venv/lib/python3.14/site-packages/httpcore/_async/connection.py:107,151`).
- Redirects are followed by hand, at most 3, and each hop is checked again.

**Imitated files.**

- The upload route copies the streaming size check of `add_photo`
  (`http/perfume_router.py:259-275`), behind the same `catalog:manage` router dependency
  (`http/perfume_router.py:58-62`).
- Claiming copies `OutboxRelay._relay_one` (`shared/infrastructure/outbox.py:103-130`).
- The migration shape follows `migrations/versions/0009_catalog_perfume_photos.py`.
- The commands follow `application/commands/photos.py`: storage work happens outside the
  transaction, and a failure deletes the objects with `_delete_quietly`
  (`application/commands/photos.py:388-393`).

## Out of scope

- **Confirming and applying an import** (plan 008):
  - writing perfumes, presentations and photos;
  - recording `source_url` on photos the import creates;
  - the `applied` status transition.
- **A web panel** (phase 3). The API returns codes; the panel translates them.
- **The data itself.** No CSV, no other sheet formats, no creating brands, families or
  concentrations, and the file never archives or restores anything (decisions 13, 15, 62, 68).
- **Re-running a failed or interrupted validation.** The admin uploads the file again.
- **Orphan objects** left by a worker that crashes mid-validation. They are logged, never
  swept.
- **The open finding `platform-unbounded-request-body`** (the JSON routes).
- **Renaming the CI job "API integration (PostgreSQL + Valkey)".**
- **Any change to the photo routes of plan 006.**

## Dependencies

- **003 (done):**
  - `Perfume`, `Presentation` and the value objects `PerfumeName`, `Description`, `Notes`,
    `Ml`, `Price`, `Sale`, `Availability` and `Gender` (`domain/perfume.py:114-260`);
  - identity by brand + name slug + concentration (`infrastructure/tables.py:216`);
  - the reference rules of decision 23 (`application/commands/perfumes.py:155-168`).
- **006 (done):**
  - `ImageProcessor`, `ProcessedImage` and `MediaStorage` (`application/ports.py:249-274`);
  - `photo_keys` (`domain/perfume.py:297-300`);
  - `catalog.perfume_photos` (`infrastructure/tables.py:245-264`);
  - the photo error codes (`domain/errors.py:188-219`);
  - `photo_max_bytes` (`config.py:79`).

## Steps

1. **Mailpit in the CI integration job (decision 57)**
   - Files: `.github/workflows/ci.yml` (modify)
   - Do:
     - Add a `mailpit` service to `api-integration`, next to `storage`:
       - the image `axllent/mailpit@sha256:ed9b00c609e77e99c79b93f1178255ebc271868920f2c69a8d166bd5634ed10d`,
         the same digest as `infra/docker/compose.yaml:76`;
       - ports `1026:1025` and `8026:8025`;
       - health options `--health-cmd "/mailpit readyz"` with the same interval, timeout and
         retries as `valkey`.
     - Add the job env vars `SMTP_HOST: 127.0.0.1` and `SMTP_PORT: "1026"`.
     - The identity tests read the UI port from `MAILPIT_UI_PORT`, which defaults to `8026`
       (`apps/api/tests/integration/identity/test_sql_account_links.py:87`), so no further env
       var is needed.
   - Observable result: the integration job no longer skips the four Mailpit tests (the local
     run reports 2 skipped; CI used to report 6).

2. **Runtime dependencies**
   - Files: `apps/api/pyproject.toml` (modify), `uv.lock` (modify)
   - Do: from `apps/api`, run `uv add openpyxl httpx tzdata`. Never edit `uv.lock` by hand.
     - `openpyxl` reads and writes `.xlsx`.
     - `httpx` downloads the images. Today it is present only through the dev tools.
     - `tzdata` provides the `America/Cancun` zone in the slim image.
   - Observable result: `uv run python -c "import openpyxl, httpx, zoneinfo;
     zoneinfo.ZoneInfo('America/Cancun')"` works in `apps/api`.

3. **Settings**
   - Files: `apps/api/src/fragancia_api/config.py` (modify), `apps/api/.env.example` (modify)
   - Do: add these to `Settings`, after `photo_max_bytes` (`config.py:79`):
     - `catalog_import_max_bytes: int = Field(default=5_242_880, ge=1)`
     - `catalog_import_max_rows: int = Field(default=2000, ge=1)`
     - `catalog_import_ttl_minutes: int = Field(default=60, ge=1)`
     - `catalog_import_image_allow_private_hosts: bool = False`. Only tests and local QA set
       it.

     In `.env.example`, add a commented block that documents the four variables with these
     defaults.
   - Observable result: `Settings()` exposes the four fields with these defaults.

4. **Domain: the import model and the cell rules**
   - Files: `apps/api/src/fragancia_api/modules/catalog/domain/catalog_import.py` (create),
     `apps/api/src/fragancia_api/modules/catalog/domain/import_cells.py` (create),
     `apps/api/src/fragancia_api/modules/catalog/domain/errors.py` (modify)
   - Do:

     **`errors.py`** gains these codes, each a dataclass with a `code` and an English
     `message`, in the style of `errors.py:11-26`:

     | Group | Category | Codes |
     | --- | --- | --- |
     | HTTP-level | `InvalidValueError` | `CATALOG_IMPORT_FORMAT_INVALID`, `CATALOG_IMPORT_TOO_LARGE` (details `max_bytes`) |
     | HTTP-level | `NotFoundError` | `CATALOG_IMPORT_NOT_FOUND` |
     | Report-only | `InvalidValueError` | `CATALOG_IMPORT_FILE_UNREADABLE`, `CATALOG_IMPORT_HEADER_MISSING` (details `columns`), `CATALOG_IMPORT_HEADER_DUPLICATED` (details `columns`), `CATALOG_IMPORT_EMPTY`, `CATALOG_IMPORT_TOO_MANY_ROWS` (details `max_rows`), `CATALOG_IMPORT_REQUIRED`, `CATALOG_IMPORT_VALUE_INVALID`, `CATALOG_IMPORT_BRAND_UNKNOWN`, `CATALOG_IMPORT_FAMILY_UNKNOWN`, `CATALOG_IMPORT_CONCENTRATION_UNKNOWN`, `CATALOG_IMPORT_PERFUME_MISMATCH`, `CATALOG_IMPORT_IMAGE_URL_INVALID`, `CATALOG_IMPORT_IMAGE_HOST_BLOCKED` |
     | Report-only | `ConflictError` | `CATALOG_IMPORT_BRAND_ARCHIVED`, `CATALOG_IMPORT_FAMILY_ARCHIVED`, `CATALOG_IMPORT_CONCENTRATION_ARCHIVED`, `CATALOG_IMPORT_PERFUME_ARCHIVED`, `CATALOG_IMPORT_PRESENTATION_ARCHIVED`, `CATALOG_IMPORT_DUPLICATE_ROW` |
     | Report-only | `DomainError` | `CATALOG_IMPORT_IMAGE_UNREACHABLE`, `CATALOG_IMPORT_INTERRUPTED`, `CATALOG_IMPORT_FAILED` |

     **`import_cells.py`** is pure: the stdlib plus `naming.slugify`.
     - `COLUMNS`: an ordered tuple of the 21 exact Spanish headers, in this order: `Marca`,
       `Nombre`, `Concentración`, `Género`, `Familia olfativa`, `ML`, `Precio`,
       `Precio oferta`, `Oferta desde`, `Oferta hasta`, `Disponibilidad`,
       `Entrega mín. (días)`, `Entrega máx. (días)`, `Publicado`, `Notas de salida`,
       `Notas de corazón`, `Notas de fondo`, `Descripción`, `Imagen 1`, `Imagen 2`,
       `Imagen 3`.
     - `REQUIRED_COLUMNS`: `Marca`, `Nombre`, `Concentración`, `Género`, `Familia olfativa`,
       `ML`, `Precio`, `Disponibilidad`.
     - `header_key(text)` is `slugify(text)`. Headers are matched by key, so case, accents and
       punctuation do not matter. Unknown headers are ignored.
     - `type Cell = str | int | float | bool | date | datetime | None`.
     - The parsing functions below each return `Result[T, ImportValueInvalid]`. None of them
       raises.
       - `text(cell)`:
         - `None` → `""`;
         - `str` → stripped;
         - `int`, or an integral `float` → its digits;
         - anything else → error.
         - More than 4,000 characters is an error.
       - `whole_number(cell)`: an `int`, an integral `float`, or a digit-only string.
       - `pesos_to_cents(cell)` (decision 67):
         - `int`, `float` (through `Decimal(str(value))`) or a string matching
           `^\d+(\.\d{1,2})?$`;
         - more than 2 decimals, a negative or a non-finite value is an error;
         - returns `int(amount * 100)`.
       - `sale_day(cell)` (decision 66):
         - a `date`, a `datetime` at exactly 00:00:00, or a string `DD/MM/AAAA` or
           `AAAA-MM-DD`;
         - anything else is an error;
         - returns a `date`.
       - `sale_window(starts, ends)` returns `(starts_at, ends_at)`, both aware datetimes in
         `ZoneInfo("America/Cancun")`:
         - `starts_at` is 00:00 of the "desde" day;
         - `ends_at` is 00:00 of the day *after* the "hasta" day;
         - either may be `None`.
       - `gender(text)`: `{"mujer": "women", "hombre": "men", "unisex": "unisex"}`, by slug.
       - `availability(text)`: `{"en-existencia": "in_stock", "sobre-pedido":
         "made_to_order"}`.
       - `published(text)`: `{"si": True, "no": False}`; empty → `None`.
       - `notes(text)`: split on `,`, strip each item, drop empty items.
       - `image_url(text)`: empty → `None`. Otherwise it must start with `http://` or
         `https://` and have at most 2,000 characters, or it is `CATALOG_IMPORT_IMAGE_URL_INVALID`.

     **`catalog_import.py`** is pure:
     - `ImportStatus(StrEnum)`: `pending`, `validating`, `ready`, `invalid`, `failed`,
       `expired`, `applied`. Plan 008 sets `applied`; it is declared now so the contract does
       not change.
     - `ImportIssue(row: int | None, column: str | None, code: str, message: str)`. A frozen
       dataclass; `row` is the Excel row number, and the header is row 1.
     - `ImportSummary(perfumes_new, perfumes_updated, presentations_new,
       presentations_updated, photos_new)`, all ints.
     - The typed plan that 008 consumes, all frozen dataclasses:
       - `PlannedPresentation(presentation_id: UUID | None, ml, price_cents,
         sale_price_cents: int | None, sale_starts_at: datetime | None,
         sale_ends_at: datetime | None, availability, lead_time_min_days: int | None,
         lead_time_max_days: int | None, row: int)`;
       - `PlannedPhoto(position, photo_id, source_url, card_key, detail_key, width, height,
         replaces_photo_id: UUID | None)`;
       - `PlannedPerfume(perfume_id, is_new, brand_id, concentration_id, family_id, name,
         gender, description, top_notes: tuple[str, ...], heart_notes, base_notes,
         publish: bool | None, first_row: int, presentations: tuple[PlannedPresentation, ...],
         photos: tuple[PlannedPhoto, ...])`;
       - `ImportPlan(perfumes: tuple[PlannedPerfume, ...])`, with `staged_keys()` returning
         every `card_key` and `detail_key`.
     - `CatalogImport(AggregateRoot)`. Its fields:
       - `id`, `status`, `file: bytes | None`, `created_by: str`, `created_at`;
       - `started_at`, `finished_at`, `expires_at`, `row_count`, `summary`, `error_count`,
         all optional;
       - `errors: tuple[ImportIssue, ...]`, `plan: ImportPlan | None`.

       Its transitions (any other is a programming error and raises `ValueError`):

       | Method | From | Effect |
       | --- | --- | --- |
       | `create(file, created_by, now)` | — | `pending` |
       | `start(now)` | `pending` | `validating`; sets `started_at` |
       | `finish_ready(now, ttl, row_count, summary, plan)` | `validating` | `ready`; sets `expires_at = now + ttl` |
       | `finish_invalid(now, row_count, errors)` | `validating` | `invalid`; `error_count = len(errors)`; keeps only the first 1,000 issues |
       | `fail(now, issue)` | `pending` or `validating` | `failed` |
       | `expire()` | `ready` | `expired`; returns the staged keys; sets `plan` to None |

       Every `finish_*` and `fail` sets `finished_at` and drops `file` (sets it to `None`).
   - Observable result: unit tests can drive every cell rule and transition without IO.

5. **Application: ports, validation and commands**
   - Files: `apps/api/src/fragancia_api/modules/catalog/application/ports.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/application/import_validation.py` (create),
     `apps/api/src/fragancia_api/modules/catalog/application/commands/catalog_imports.py`
     (create),
     `apps/api/src/fragancia_api/modules/catalog/application/queries/catalog_imports.py`
     (create)
   - Do:

     **`ports.py`** gains these ports:
     - **`SheetRows`:** `rows: list[tuple[int, dict[str, Cell]]]` (Excel row number → cells
       by `header_key`), `headers: list[str]` (the keys found) and `duplicated: list[str]`.
     - **`SpreadsheetReader`:** `async read(data, *, max_rows) -> Result[SheetRows,
       ImportIssue]`, where the issue is file level: unreadable, empty or too many rows.
     - **`TemplateWriter`:** `async build(families: list[str], concentrations: list[str]) ->
       bytes`.
     - **`ImageDownloader`:** `async fetch(url) -> Result[bytes, DomainError]`, where the
       error is `IMAGE_URL_INVALID`, `IMAGE_HOST_BLOCKED`, `IMAGE_UNREACHABLE` or
       `CATALOG_PHOTO_TOO_LARGE`.
     - **`ImportReferences`** (lookups, read only):
       - `brands()`, `families()` and `concentrations()` return lists of
         `ReferenceItem(id, slug, alt_slug: str | None, name, is_active)`. `alt_slug` is the
         abbreviation slug of a concentration.
       - `existing_perfumes(identities)` returns `ExistingPerfume(id, is_archived, family_id,
         presentations: dict[int, tuple[UUID, bool]], photos: dict[int, tuple[UUID,
         str | None]])`, keyed by `(brand_id, name_slug, concentration_id)`. `presentations`
         maps ml → (id, is_active); `photos` maps position → (id, source_url).
     - **`CatalogImportRepository`:**
       - `add`;
       - `get_for_update(id)`;
       - `claim_next_pending()`: the oldest pending import by `created_at`, locked with
         `FOR UPDATE SKIP LOCKED`, or `None`;
       - `list_stale_validating(before)` and `list_expired_ready(now)`, both locked with
         `SKIP LOCKED`;
       - `save`.
     - **`CatalogImportQueries`:** `get(id) -> CatalogImportResponse | None`.

     **`import_validation.py`**: `ImportValidator`, built with `references`, `downloader`,
     `images` (`ImageProcessor`) and `storage` (`MediaStorage`). Its method is
     `async validate(sheet: SheetRows) -> tuple[list[ImportIssue], ImportSummary | None,
     ImportPlan | None, list[str]]`; the last item is the keys it uploaded. The algorithm,
     in order:

     1. **Headers.** Every missing `REQUIRED_COLUMNS` key → one file-level
        `HEADER_MISSING` issue (row None) listing them. Duplicated headers →
        `HEADER_DUPLICATED`. With either, return at once.
     2. **Load the references once.** Match brands and families by
        `slugify(cell) == slug`. Match a concentration by name slug first, then by
        abbreviation slug (decision 65).
     3. **Per row**, collect *every* issue (not only the first), with
        `column` = the header text:
        - An empty required cell → `REQUIRED`.
        - Parse errors from `import_cells` → `VALUE_INVALID` or `IMAGE_URL_INVALID`.
        - The domain value objects give their own codes:
          - `PerfumeName.create`, `Description.create`, and `Notes.create` (once per level);
          - `Ml.create`, `Price.create`;
          - `Sale.create(..., regular=price)`, with the window from `sale_window`;
          - `Availability.create`. "Sobre pedido" needs both lead-time cells; "En existencia"
            needs both empty.
        - Unknown brand, family or concentration → `*_UNKNOWN`. Archived ones follow
          decision 23, as in `commands/perfumes.py:92-100` and `155-168`:
          - **A new perfume** needs an active brand, family and concentration.
          - **An existing perfume** keeps an archived brand or concentration, which are part
            of its identity. Its family must be active only when it differs from the stored
            one.
          - Breaking these gives `*_ARCHIVED`.
     4. **Group the rows** with a resolved identity by `(brand_id, name.slug,
        concentration_id)`, then call `existing_perfumes` once for all groups.
        - Within a group, every later row is compared with the first one on: Género, Familia
          olfativa, Descripción, the three notes columns (as parsed lists), Publicado and
          Imagen 1–3. Each differing column gives a `PERFUME_MISMATCH` (decision 61).
        - A repeated ml in a group → `DUPLICATE_ROW` on the later row.
        - An existing archived perfume → `PERFUME_ARCHIVED` on every row of the group.
        - An existing presentation at that ml that is archived → `PRESENTATION_ARCHIVED`
          (decision 68).
     5. **Images, only for groups without issues.** Let `k` be the number of existing photos.
        For each `Imagen N` (N = 1..3) with a URL:
        - The existing photo at position N has the same `source_url` → skip it (decision 58).
        - N ≤ k → replace: the new photo keeps position N, and `replaces_photo_id` is the old
          id.
        - Otherwise → append at the next free position, in ascending N.

        Then, for each URL to fetch:
        - Download it through `downloader.fetch`, at most **4 concurrent** downloads
          (`asyncio.Semaphore`). A URL repeated within the file is downloaded and processed
          once (cache by URL).
        - Process it with `images.process`.
        - Upload card and detail to `photo_keys(perfume_id, photo_id)` with a new `photo_id`.
          A new perfume gets its `perfume_id` from `new_id()` up front.

        A fetch or process error becomes an issue on the group's first row, with
        `column = "Imagen N"` and the error's code.
     6. **Result.**
        - With **any** issue, return the issues, `None`, `None` and the uploaded keys. The
          caller deletes them.
        - Otherwise, return no issues, the `ImportSummary`, the `ImportPlan` and the keys.
          - "updated" counts existing perfumes and presentations that appear in the file,
            whether or not a value changed.
          - Presentations with an existing ml carry their `presentation_id`.

     Issues are sorted by `(row or 0, column order in COLUMNS)`.

     **`commands/catalog_imports.py`:**
     - **`ImportUploadLimit(max_bytes)`:** the same idea as `PhotoUploadLimit`
       (`commands/photos.py:381-385`).
     - **`StartCatalogImport(imports, transactions, clock).execute(data, created_by) ->
       Result[UUID, DomainError]`:** data that does not start with `b"PK\x03\x04"` →
       `CATALOG_IMPORT_FORMAT_INVALID`. Otherwise it adds a `pending` import.
     - **`ValidateNextCatalogImport(imports, reader, validator, storage, transactions, clock,
       max_rows, ttl).run_once() -> int`:**
       1. In a transaction, claim the next pending import, call `start(now)` and save. With
          nothing pending, return 0.
       2. Outside any transaction, `reader.read(file, max_rows=...)`:
          - a file-level issue → `finish_invalid` with that issue;
          - otherwise `validator.validate(...)`.
       3. Save in a new transaction, after a `get_for_update`:
          - with issues → `finish_invalid`, then delete the uploaded keys quietly;
          - otherwise → `finish_ready`.
       4. Any unexpected exception → log `catalog_import_failed` with `exc_info`, delete the
          uploaded keys quietly, and save `fail(now, CATALOG_IMPORT_FAILED)`. It never
          re-raises, so the cron keeps going.
       5. Return 1.
     - **`ExpireCatalogImports(imports, storage, transactions, clock, stale_after).run_once()
       -> int`:**
       - Imports `validating` with `started_at` before `now - stale_after` (30 minutes) →
         `fail(now, CATALOG_IMPORT_INTERRUPTED)`, with a warning log
         `catalog_import_interrupted`. Their objects may be orphans; that is the documented
         risk.
       - `ready` imports with `expires_at <= now` → `expire()`, then delete the returned keys
         quietly *after* the commit.
       - It returns how many imports changed.

       Copy `_delete_quietly` (`commands/photos.py:388-393`) into a shared private helper of
       this file. Do not import the private one.

     **`queries/catalog_imports.py`:**
     - `GetCatalogImport(queries)`, whose `execute(id)` returns `Result` with `NOT_FOUND`;
     - `DownloadCatalogTemplate(families: OlfactoryFamilyQueries, concentrations:
       ConcentrationQueries, writer)`. It builds the template from the active names
       (`ports.py:310` and `322`).
   - Observable result: with in-memory fakes, a sheet goes through `validate` to the expected
     issues, summary and plan.

6. **Persistence: table, column and migration**
   - Files: `apps/api/src/fragancia_api/modules/catalog/infrastructure/tables.py` (modify),
     `apps/api/migrations/versions/0010_catalog_imports.py` (create)
   - Do:
     - **`catalog.imports`:**
       - `id` Uuid PK, `status` String(12) not null, `file` LargeBinary null,
         `created_by` String(64) not null;
       - `created_at` not null; `started_at`, `finished_at` and `expires_at` DateTime(tz)
         null;
       - `row_count` Integer null, `error_count` Integer null;
       - `summary`, `errors` and `plan` JSONB null;
       - `Index("ix_catalog_imports_status_created_at", "status", "created_at")`.
     - **`catalog.perfume_photos`** gains `source_url` String(2000), nullable. Photos
       uploaded from the panel keep `NULL`.
     - **Migration:** `uv run just db-revision "catalog imports"`, renamed and numbered
       `0010` like `0009`. Check that `downgrade` drops the column and the table.
   - Observable result: on the test DB, `uv run just db-migrate --test` then
     `alembic -x test=true check` is clean, and so is a downgrade/upgrade round trip.

7. **Infrastructure adapters**
   - Files:
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/xlsx_sheets.py` (create),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/http_image_downloader.py`
     (create),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_catalog_import_repository.py`
     (create),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_import_references.py`
     (create),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_catalog_import_queries.py`
     (create),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/in_memory_imports.py`
     (create)
   - Do:

     **`xlsx_sheets.py`.** openpyxl is blocking, so both adapters run in
     `asyncio.to_thread`.
     - **`OpenpyxlSheetReader`:**
       - opens `load_workbook(BytesIO(data), read_only=True, data_only=True)` and reads the
         first worksheet;
       - takes the header from row 1 with `max_col=30`;
       - iterates data rows with `min_row=2, max_col=30` and skips fully empty rows;
       - **stops** as soon as it has seen `max_rows + 1` non-empty rows →
         `TOO_MANY_ROWS`;
       - with no data rows, returns `EMPTY`;
       - any exception from openpyxl or zipfile → `FILE_UNREADABLE`, logged at info level
         with the exception class only;
       - closes the workbook in `finally`.
     - **`OpenpyxlTemplateWriter`:**
       - a sheet `Catálogo` with the `COLUMNS` headers in bold, frozen at row 2, with column
         widths;
       - a hidden sheet `Listas` with the families, the concentration names, `Mujer`,
         `Hombre`, `Unisex`, `En existencia`, `Sobre pedido`, `Sí` and `No`;
       - list `DataValidation` on rows 2–2001 of Familia olfativa, Concentración, Género,
         Disponibilidad and Publicado, each pointing at its range in `Listas`.

     **`http_image_downloader.py`: `HttpImageDownloader(max_bytes, allow_private_hosts)`.**
     For each URL:
     - `urlsplit`. The scheme must be http or https, the host must be present and there must
       be no userinfo; otherwise `IMAGE_URL_INVALID`.
     - Resolve with `loop.getaddrinfo(host, port, type=SOCK_STREAM)`. If any address is not
       `is_global`, or is multicast, return `IMAGE_HOST_BLOCKED`, unless
       `allow_private_hosts`. A resolution failure is `IMAGE_UNREACHABLE`.
     - Request the URL with the checked IP as host (IPv6 in brackets) through
       `httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(10.0, read=20.0))`:
       - set the header `Host` to the original host, plus `:port` when it is not the
         default;
       - for https, pass `extensions={"sni_hostname": host}`;
       - stream the body.
     - **Redirects:** a 301, 302, 303, 307 or 308 with `Location` is resolved with `urljoin`
       and re-checked from the top, at most 3 hops; more → `IMAGE_UNREACHABLE`.
     - **Responses:**
       - any other non-200 status → `IMAGE_UNREACHABLE` (details `status`);
       - a declared `Content-Length` over `max_bytes`, or a streamed total over it →
         `CATALOG_PHOTO_TOO_LARGE`;
       - httpx transport errors and timeouts → `IMAGE_UNREACHABLE`.
     - The whole fetch runs under `asyncio.timeout(30)`.

     **`sql_catalog_import_repository.py`:**
     - Copy the claiming of `outbox.py:105-113`.
     - Map `ImportPlan`, `ImportSummary` and the issues to and from JSON explicitly: UUIDs
       and ISO-8601 datetimes as strings. No pickling.
     - Every method uses `self._database.session`.

     **`sql_import_references.py`:**
     - Three plain selects for brands, families and concentrations.
     - One select for perfumes whose `(brand_id, name_slug, concentration_id)` is in the given
       set (a `tuple_(...).in_(...)`), plus one select each for their presentations and
       photos. It uses `Database.reader()` (`database.py:46-50`).

     **`sql_catalog_import_queries.py`:** maps the row to `CatalogImportResponse`.

     **`in_memory_imports.py`:** fakes for the unit tests, in the style of
     `infrastructure/in_memory_media.py:14-58`:
     - a reader returning given `SheetRows`;
     - a downloader returning bytes or errors per URL;
     - references with given lists;
     - a repository and queries.
   - Observable result: integration tests can round-trip a template through the reader, and
     the downloader refuses `http://127.0.0.1/...` with `allow_private_hosts=False`.

8. **Contracts and HTTP routes**
   - Files: `apps/api/src/fragancia_api/modules/catalog/contracts.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/http/import_router.py` (create),
     `apps/api/openapi.json` (modify)
   - Do:

     **`contracts.py`:**
     - `CatalogImportIssue(row: int | None, column: str | None, code: str, message: str)`;
     - `CatalogImportSummary` with the five counts;
     - `CatalogImportResponse(id, status: Literal[...the seven statuses...], created_at,
       finished_at: datetime | None, expires_at: datetime | None, row_count: int | None,
       summary: CatalogImportSummary | None, error_count: int, errors:
       list[CatalogImportIssue])`. `error_count` is 0 while there are none, and can exceed
       `len(errors)` when the list is capped.

     **`import_router.py`:**
     ```python
     admin_imports = admin_router(
         prefix="/catalog-imports",
         tags=["catalog · import"],
         dependencies=[Depends(require_permission(CATALOG_MANAGE))],
     )
     ```

     | Route | Behaviour |
     | --- | --- |
     | `GET /template` | 200, `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, `Content-Disposition: attachment; filename="plantilla-catalogo.xlsx"` |
     | `POST ""` | Raw body with that same media type, else `CATALOG_IMPORT_FORMAT_INVALID`. Copy the size check of `perfume_router.py:262-274` with `ImportUploadLimit` → `CATALOG_IMPORT_TOO_LARGE`. An empty body → `FORMAT_INVALID`. **202** `CreatedResponse`. `created_by` is `request.state.actor.id` (set by `require_admin`, `shared/http/access.py`) |
     | `GET /{import_id}` | 200 `CatalogImportResponse`, or 404 `CATALOG_IMPORT_NOT_FOUND` |

     - Declare the raw request body in OpenAPI the way `_PHOTO_BODY_OPENAPI` does
       (`perfume_router.py:231-238`).
     - Export `import_routers = (admin_imports,)`.
     - Run `uv run just openapi`.
   - Observable result: the three routes appear in `/api/v1/docs` under "catalog · import",
     and an anonymous call gets 401.

9. **Module jobs in the worker**
   - Files: `apps/api/src/fragancia_api/shared/module.py` (modify),
     `apps/api/src/fragancia_api/main/worker.py` (modify)
   - Do:
     - **`shared/module.py`:** add a frozen dataclass and an `AppModule` field.
       ```python
       ScheduledJob(name: str, run: Callable[[ServiceRegistry], Awaitable[int]],
                    every_seconds: int, timeout_seconds: int)
       ```
       - `every_seconds` divides 60, or is 60.
       - `AppModule` gains `jobs: Sequence[ScheduledJob] = field(default_factory=tuple)`.
     - **`main/worker.py`:**
       - Import `MODULES` from `fragancia_api.container`.
       - For each job, add one `cron(...)` with:
         - an `async def run(ctx)` closure that awaits
           `job.run(ctx["container"].services)` and logs `job.processed` (with `job` and
           `processed`) when the count is above 0;
         - `name=f"cron:{job.name}"`;
         - `second=set(range(0, 60, job.every_seconds))`, or `{0}` for 60;
         - `timeout=job.timeout_seconds` and `run_at_startup=False`.
       - Make `_hide_outbox_cron_runs` (`worker.py:32-34`) hide the INFO run lines of every
         `cron:` job, not only the outbox one, and rename it `_hide_cron_runs`.
   - Observable result: `uv run just worker` starts, lists the outbox cron and the two
     catalog crons, and stays quiet while idle.

10. **Wiring**
    - Files: `apps/api/src/fragancia_api/modules/catalog/module.py` (modify)
    - Do: in `register` (`module.py:95-219`), build these:
      - the import repository, references and queries;
      - `OpenpyxlSheetReader`, `OpenpyxlTemplateWriter`;
      - `HttpImageDownloader(max_bytes=settings.photo_max_bytes,
        allow_private_hosts=settings.catalog_import_image_allow_private_hosts)`;
      - an `ImportValidator` that reuses the existing `images` and `media`
        (`module.py:164-171`).

      Register:
      - `StartCatalogImport`, `ImportUploadLimit(settings.catalog_import_max_bytes)`;
      - `ValidateNextCatalogImport` (with `max_rows` and a `ttl` of
        `timedelta(minutes=settings.catalog_import_ttl_minutes)`);
      - `ExpireCatalogImports` (`stale_after=timedelta(minutes=30)`);
      - `GetCatalogImport`, `DownloadCatalogTemplate`.

      The module becomes:
      ```python
      AppModule(
          name="catalog",
          register=register,
          routers=(*routers, *perfume_routers, *import_routers),
          jobs=(
              ScheduledJob(
                  "catalog_validate_imports",
                  lambda s: s.get(ValidateNextCatalogImport).run_once(),
                  5,
                  1800,
              ),
              ScheduledJob(
                  "catalog_expire_imports",
                  lambda s: s.get(ExpireCatalogImports).run_once(),
                  60,
                  120,
              ),
          ),
      )
      ```
    - Observable result: `uv run just arch` is green, and the container builds in
      `tests/unit/test_container.py`.

11. **Docs**
    - Files: `docs/architecture.md` (modify), `apps/api/README.md` (modify)
    - Do:
      - **`docs/architecture.md`**, under "Events, outbox and jobs" (`docs/architecture.md:139`):
        modules declare `ScheduledJob`s that the worker runs as arq crons. The catalog import
        is a database-backed job claimed with `SKIP LOCKED`.
      - **`apps/api/README.md`:** update the worker lines (`apps/api/README.md:11` and `29`)
        to mention the catalog import jobs.
    - Observable result: docs describe the new jobs; `uv run just check` stays green.

## Acceptance criteria

- [ ] **Template:** `GET /api/v1/admin/catalog-imports/template`, with an admin session that
  has `catalog:manage`, returns 200 and an `.xlsx` that opens with:
  - the 21 headers in the `COLUMNS` order;
  - dropdowns on Familia olfativa (the active families), Concentración (the active names),
    Género, Disponibilidad and Publicado.
- [ ] **Valid upload:** posting a filled template returns **202** `{id}`. Polling
  `GET /api/v1/admin/catalog-imports/{id}` shows `pending` or `validating`, and then `ready`:
  - `summary` has the expected counts;
  - `error_count` is 0;
  - `expires_at` is about 60 minutes after `finished_at`.
- [ ] **Staged photos:** for a ready import with image URLs, the bucket holds the card and
  detail WebP for each staged photo under `perfumes/{perfume_id}/`.
- [ ] **Row errors:** a file with each of these gets `invalid`, with one issue per problem
  carrying the Excel row number, the header text and its code:
  - an unknown brand;
  - an archived family on a new perfume;
  - `ML` = `abc`;
  - `Precio` = `10.555`;
  - a sale price above the price;
  - "Sobre pedido" without days;
  - two rows of one perfume with different descriptions;
  - the same perfume + ml twice;
  - an archived perfume.
- [ ] **No leftovers:** after an `invalid` result, the bucket holds no objects staged by that
  import.
- [ ] **Image checks:** an image URL that returns 404, one that is not an image, and one on
  `127.0.0.1` (with the default settings) give `IMAGE_UNREACHABLE`,
  `CATALOG_PHOTO_FORMAT_INVALID` and `IMAGE_HOST_BLOCKED` on the perfume's first row, column
  `Imagen N`.
- [ ] **Sale dates:** "Oferta desde" 01/11/2026 and "Oferta hasta" 30/11/2026 produce a plan
  whose `sale_starts_at` is `2026-11-01T00:00:00-05:00` and whose `sale_ends_at` is
  `2026-12-01T00:00:00-05:00`.
- [ ] **Existing photos:** a perfume whose photo 1 has the same `source_url` as the
  `Imagen 1` cell downloads nothing for it, and `photos_new` does not count it. The
  `source_url` is set by SQL for QA, since only 008 writes it. A different URL plans a
  replacement with `replaces_photo_id`.
- [ ] **File-level rejects:**
  - A 6 MB body → 422 `CATALOG_IMPORT_TOO_LARGE`, both with `Content-Length` and chunked.
  - A body of the wrong media type, or not a ZIP → 422 `CATALOG_IMPORT_FORMAT_INVALID`.
  - A file with 2,001 data rows → `invalid` with `CATALOG_IMPORT_TOO_MANY_ROWS`.
  - A file without the `Precio` header → `CATALOG_IMPORT_HEADER_MISSING`.
- [ ] **Access:** without a session, all three routes return 401. An admin without
  `catalog:manage` gets 403.
- [ ] **Expiry:** with `CATALOG_IMPORT_TTL_MINUTES=1`, a ready import becomes `expired` after
  the expiry cron runs, and its staged objects are gone.
- [ ] **CI:** the integration job runs the four Mailpit tests instead of skipping them.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | Every `import_cells` rule, including the boundaries: 2 vs 3 decimals, 00:00 vs a time, the Cancún window and the day after "hasta", accents and case in the words, and notes splitting. `CatalogImport` transitions and the 1,000-issue cap. `ImportPlan.staged_keys` |
| application | yes     | `ImportValidator` with fakes: each issue code, collecting several issues per row, grouping and mismatch, duplicate ml, the archived rules of decision 23 for new and existing perfumes, photo placement (same URL skips, replace, append, URL cache), and no downloads for groups with issues. Every path through `ValidateNextCatalogImport`: nothing pending, file-level issue, invalid (keys deleted), ready, and an unexpected exception (failed, keys deleted, no raise). `ExpireCatalogImports`: stale validating and expired ready |
| http        | yes     | The three routes: 202 and 200 shapes, media type and size checks (declared and chunked), 404, 401 and 403, the template headers |
| integration | yes     | Migration round trip. The SQL repository: claiming with `SKIP LOCKED` (two concurrent claims get different imports or `None`), JSON round trip of plan and issues. SQL references, including `source_url`. openpyxl: a template round trip, a non-xlsx file, row limit, formulas read as values. The downloader against a local HTTP server: 200, 404, a redirect to a private host blocked, too large, timeout. The full job against real PostgreSQL and RustFS |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

All cosmetic or implementation detail; none changes scope or design.

1. **Stale `file:line` citations.** Several line numbers do not match the files. The cited
   symbols exist and were used: `ImageProcessor`/`MediaStorage` are at `application/ports.py:37-52`
   (not 249-274); `list_active` at `ports.py:88` and `100` (not 310/322); `PhotoUploadLimit` and
   `_delete_quietly` at `commands/photos.py:25-37` (not 381-393); `AppModule` at
   `shared/module.py:31-38`.
2. **Header issues carry their columns in `column`.** `ImportIssue` (and the contract) has no
   `details`. A `HEADER_MISSING` / `HEADER_DUPLICATED` issue therefore lists the header texts
   in `column`, joined by `", "` (e.g. `"Precio, ML"`). The error dataclass still gets
   `details={"columns": [...]}`. `TOO_MANY_ROWS` puts `max_rows` in the error details only.
3. **`sale_window` and `notes` return plain values, not `Result`.** Neither can fail. They
   return `(starts_at, ends_at)` and `list[str]`.
4. **The validator cleans up its own uploads when staging raises.** If an exception escapes
   the image stage, the caller never receives the keys. So `ImportValidator` deletes them quietly
   and then re-raises. `ValidateNextCatalogImport` still deletes every key it received. The
   "delete quietly" helper therefore exists twice: the plan's copy in
   `commands/catalog_imports.py`, and a private one in `import_validation.py`, which cannot
   import the command module without a cycle.
5. **Defensive paths in `ValidateNextCatalogImport`.** If the import disappears between the
   claim and the save, the save raises and the import is treated as failed. `_fail` marks it
   `failed` only when it is still `pending` or `validating`. A stale validation that the expiry
   job already failed is left as it is, so `fail` never raises `ValueError`.
6. **Downloader tries every resolved address.** All resolved addresses are checked. The fetch
   then connects to each in resolver order until one accepts the connection
   (`httpx.ConnectError`). Before this change `localhost` resolved to `::1` first and was
   unreachable against a service bound to IPv4. The downloader also uses `trust_env=False`, so
   no proxy from the environment bypasses the IP check.
7. **Small additions.**
   - `ScheduledJob.__post_init__` rejects an `every_seconds` that does not divide 60.
   - `OpenpyxlSheetReader` calls `reset_dimensions()`, so a wrong size in the sheet does not
     hide rows.
   - The template skips the dropdown of an empty list (no active families or concentrations),
     since an empty range is not a valid formula.
   - openpyxl has no type stubs, so its imports carry `# type: ignore[import-untyped]`, like
     boto3.
8. **Report details the plan leaves open.**
   - Archived-reference checks run only on rows with a resolved identity.
   - `PERFUME_ARCHIVED` uses column `Nombre`.
   - An invalid sale uses `Precio oferta`, and an invalid availability or lead time uses
     `Disponibilidad`.
   - `photos_new` counts replacements as well as appends.
   - `fail()` stores its single issue with `error_count = 1`.
   - `finish_ready` sets `error_count = 0`.

Implementation evidence (2026-10-11):

- `uv run just check`: green.
- `uv run just test-integration`: 329 passed, 2 skipped.
- Migration 0010 applied with `db-migrate` and `db-migrate --test`. On the test DB,
  `alembic -x test=true check` is clean, and so is a `downgrade 0009` / `upgrade head` round
  trip.
- `uv run just openapi` regenerated the contract.
- `uv run just plans-scope` is clean.
- The running worker listed `cron:relay_outbox`, `cron:catalog_validate_imports` and
  `cron:catalog_expire_imports` and stayed quiet while idle.
- Without a session, the three routes returned 401.
- A scratch script (not committed) exercised these:
  - a template round trip through the reader;
  - row issues, grouping and mismatches;
  - a Cancún sale window of `2026-11-01T00:00-05:00` → `2026-12-01T00:00-05:00`;
  - a skipped photo with the same URL;
  - downloader refusals for loopback, link-local, userinfo and non-http URLs;
  - a 404 and a too-large body against local services.

## Test coverage

## Review findings

## Verification
