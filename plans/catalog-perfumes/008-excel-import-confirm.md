---
status: approved
module: catalog
min_implementer: high
depends_on: ["007"]
---

# 008 — Excel import: confirm and discard

## Context

Plan 007 (done) leaves a validated import in status `ready`.

- **The import.** It holds a typed `ImportPlan`
  (`apps/api/src/fragancia_api/modules/catalog/domain/catalog_import.py:66-129`) and expires
  after 60 minutes.
- **Staged photos.** They already sit at their final keys in S3.
- **Ids.** The plan carries the final ids of new perfumes and photos (`PlannedPerfume`,
  `catalog_import.py:96-115`), and the existing presentation ids (`PlannedPresentation`,
  `catalog_import.py:66-79`).
- **The missing step.** Nothing applies the plan yet. `ImportStatus.APPLIED` exists, but no
  transition sets it (`catalog_import.py:32-39`, `14`).

This plan adds two routes:
- **`POST /admin/catalog-imports/{id}/confirm`** applies a ready import all or nothing (decision
  14) in the request, in a single transaction (decision 73).
- **`POST /admin/catalog-imports/{id}/discard`** drops it early (decision 74).

**A stale confirm applies nothing** (decision 72). The plan is a snapshot taken up to 60
minutes earlier, and confirming re-checks it against the live catalog. Each perfume goes
through its aggregate, and any refusal is collected as an issue:
- an archived perfume;
- an identity taken by someone else;
- a presentation that disappeared or was archived;
- a 4th photo;
- a reference that is no longer usable.

With at least one issue, the transaction is rolled back and the route answers 409
`CATALOG_IMPORT_STALE` with the issues. The import stays `ready`, so the admin can see why and
upload again.

**Approaches compared.**
1. **Reuse the panel commands per row** (`CreatePerfume`, `UpdatePerfume`, `AddPresentation`
   and so on, `application/commands/perfumes.py`, `presentations.py`). Rejected: each runs its
   own `TransactionRunner.run`. Nested calls join the outer transaction
   (`shared/infrastructure/database.py:64-66`), but each returns its first `Err`. They also
   cannot create a perfume with a pre-assigned id, or a photo whose objects already exist.
2. **Chosen: one `ConfirmCatalogImport` command.** It drives the aggregates directly inside one
   transaction, with the same rules the panel commands enforce:
   - the reference checks of decision 23 (`commands/perfumes.py:92-100`, `155-168`);
   - the slug recompute (`domain/perfume.py:263-265`);
   - the aggregate methods for presentations and photos.

   It collects every issue before deciding.

**Domain additions.**
- `Perfume.create` takes an optional `id`, because the plan pre-assigned it.
- `Photo` gains `source_url` (the column exists since 007, `infrastructure/tables.py`).
- `Perfume.add_photo` takes `source_url`.
- A new `Perfume.replace_photo` keeps the position of the photo it replaces.

**Repository changes.**
- `SqlPerfumeRepository.add` inserts photos too. Today it inserts only the perfume and its
  presentations (`infrastructure/sql_perfume_repository.py:191-205`).
- Photo rows carry `source_url` (`_photo_row`/`_to_photo`, `sql_perfume_repository.py:114-136`).

**Objects.** A replaced photo's objects are deleted after the commit, exactly like
`RemovePhoto` (`application/commands/photos.py:464-499`). A discard deletes the staged keys
after the commit, like `ExpireCatalogImports` (`commands/catalog_imports.py:188-230`).

**Imitated files.**
- The command shape follows `ExpireCatalogImports`: state changes in the transaction, object
  deletes after the commit, and `_delete_quietly` (`commands/catalog_imports.py:51-58`).
- The routes follow `get_catalog_import` (`http/import_router.py:99-107`).

## Out of scope

- **Re-validating automatically** on a stale confirm (decision 72): the admin uploads again.
- **Partial application.** Never: all or nothing (decision 14).
- **Archiving or restoring** anything through the file (decisions 62, 68).
- **Undoing an applied import.**
- **Changing the import's report** of a stale confirm. The issues travel in the 409 body
  only, and the stored validation report stays as it was.
- **Any change to validation** (007), the template, or the panel photo routes. Panel uploads
  keep `source_url` NULL.
- **A web panel** (phase 3).
- **The open finding `platform-unbounded-request-body`.**

## Dependencies

- **007 (done):**
  - `CatalogImport`, `ImportPlan`, `PlannedPerfume`, `PlannedPresentation`, `PlannedPhoto`
    and `ImportIssue` (`domain/catalog_import.py`);
  - `CatalogImportRepository` with `get_for_update` and `save` (`application/ports.py:217-238`);
  - `SqlCatalogImportRepository` and its JSON mappings
    (`infrastructure/sql_catalog_import_repository.py:179-216`);
  - the `catalog.imports` table (`infrastructure/tables.py:148-166`) and
    `perfume_photos.source_url`;
  - `GetCatalogImport` and the import router (`http/import_router.py`);
  - `_delete_quietly` (`commands/catalog_imports.py:51-58`).
- **003 and 006 (done):**
  - the `Perfume` aggregate (`domain/perfume.py:326-587`), `perfume_slug`
    (`domain/perfume.py:263-265`) and `PerfumeRepository` (`domain/repositories.py:105-134`);
  - the brand and concentration repositories' `get` (`domain/repositories.py:30`, `89`);
  - `MediaStorage.delete` (`application/ports.py`).

## Steps

1. **Domain: perfume and photo**
   - Files: `apps/api/src/fragancia_api/modules/catalog/domain/perfume.py` (modify)
   - Do:
     - **`Photo`** (`perfume.py:303-323`) takes `source_url: str | None = None` and stores it.
     - **`Perfume.create`** (`perfume.py:371-403`) takes `id: UUID | None = None` and uses
       `id or new_id()`.
     - **`Perfume.add_photo`** (`perfume.py:534-559`) takes `source_url: str | None = None` and
       passes it to `Photo`. The panel callers are unchanged.
     - **New `Perfume.replace_photo(old_photo_id, photo_id, card_key, detail_key, width,
       height, *, source_url, created_at) -> Result[Photo, PerfumeArchived | PhotoNotFound]`:**
       - It refuses on an archived perfume.
       - An unknown `old_photo_id` → `PhotoNotFound`.
       - Otherwise it puts a new `Photo` at the old photo's position, in the same list slot,
         and appends the old id to `removed_photo_ids`.
       - It returns the old `Photo`, so the caller can delete its objects.
     - Update the module docstring's photo section accordingly.
   - Observable result: unit tests can replace photo 2 of 3 and keep positions 1..3 without
     gaps.

2. **Domain: import confirm and discard; errors**
   - Files: `apps/api/src/fragancia_api/modules/catalog/domain/catalog_import.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/domain/errors.py` (modify)
   - Do:
     - **`ImportStatus`** gains `DISCARDED = "discarded"`.
     - **`CatalogImport`** gains `applied_at: datetime | None = None`,
       `applied_by: str | None = None` and `discarded_at: datetime | None = None`.
     - **`apply(now, actor_id)`** (`ready` → `applied`): sets `applied_at` and `applied_by`,
       and sets `plan` to None. The staged objects are now live.
     - **`discard(now)`** (`ready` → `discarded`): sets `discarded_at`, returns
       `plan.staged_keys()` and sets `plan` to None.
     - **`is_confirmable(now)`**: `status == READY` and `now < expires_at`.
     - Update the lifecycle docstring.
     - **`errors.py`** gains two errors:
       - `ImportNotReady(ConflictError)`, code `CATALOG_IMPORT_NOT_READY`, message "Only a
         ready import that has not expired can be confirmed or discarded";
       - `ImportStale(ConflictError)`, code `CATALOG_IMPORT_STALE`, message "The catalog
         changed since this import was validated; upload the file again". Its details are
         `{"issues": [{"row", "column", "code", "message"}, ...]}`.
   - Observable result: unit tests drive `apply` and `discard` from `ready`, and both raise
     `ValueError` from any other status.

3. **Application: the confirm and discard commands**
   - Files: `apps/api/src/fragancia_api/modules/catalog/application/commands/catalog_imports.py`
     (modify)
   - Do:

     **`ConfirmCatalogImport(imports, perfumes: PerfumeRepository, brands: BrandRepository,
     families: OlfactoryFamilyRepository, concentrations: ConcentrationRepository, storage,
     transactions, clock).execute(import_id, actor_id) -> Result[None, DomainError]`.** In one
     `transactions.run`:

     1. **Load and lock the import** with `imports.get_for_update`:
        - missing → `ImportNotFound`;
        - `not is_confirmable(now)` → `ImportNotReady`.
     2. **For each `PlannedPerfume`, in plan order**, collect issues into a list. Each issue is
        an `ImportIssue` whose `row` is the planned perfume's `first_row`, or the presentation's
        `row` for a presentation issue. Its `column` is `None`, and its code is the domain
        error's code. **Continue to the next perfume after an issue**, so that every stale row
        is reported.
        - **Values.** Build `PerfumeName`, `Description`, `Notes`, and `Ml`, `Price`, `Sale` and
          `Availability` per presentation, with the `create` methods
          (`domain/perfume.py:114-260`). Plan 007 validated them, but an `Err` still becomes an
          issue.
        - **References** (decision 23):
          - A **new** perfume needs `brands.get`, `families.get` and `concentrations.get`, each
            existing and active.
          - An **existing** perfume keeps its brand and concentration (part of its identity) even
            when archived. Its family must be active only when it differs from the stored one.
          - Breaking a rule → the same errors `CreatePerfume` and `UpdatePerfume` return:
            `PerfumeBrandUnavailable`, `PerfumeFamilyUnavailable` and
            `PerfumeConcentrationUnavailable`.
        - **The slug** is `perfume_slug(brand.name.value, name,
          concentration.abbreviation.value)`.
        - **A new perfume** (`is_new`):
          - `Perfume.create(id=planned.perfume_id, ...)`;
          - `add_presentation` for each planned presentation;
          - `add_photo(photo_id, card_key, detail_key, width, height, source_url=...,
            created_at=now)` for each planned photo, in plan order;
          - `publish(now)` when `publish is True`;
          - then `perfumes.add(perfume)`. An `Err(PerfumeAlreadyExists)` (identity or slug taken
            meanwhile) → issue.
        - **An existing perfume:**
          - `perfumes.get_for_update(perfume_id)`; missing → issue `CATALOG_PERFUME_NOT_FOUND`.
          - `perfume.update(...)`. Its `PerfumeArchived` → issue.
          - **Planned presentations with an id:** the presentation must still exist and be
            active. Otherwise → `CATALOG_PRESENTATION_NOT_FOUND`, or
            `CATALOG_IMPORT_PRESENTATION_ARCHIVED` for an archived one. Then
            `update_presentation(...)`.
          - **Planned presentations without an id:** `add_presentation(...)`. A
            `PresentationAlreadyExists` → issue.
          - **Planned photos with `replaces_photo_id`:** `replace_photo(...)`. Its
            `PhotoNotFound` → issue. Remember the returned old photo's two keys.
          - **Other planned photos:** `add_photo(...)`. Its `PhotoLimitReached` → issue.
          - **Publishing:** `publish is True` → `publish(now)`, whose refusal is an issue.
            `publish is False` → `hide()`. `None` leaves it as it is.
          - Then `perfumes.save(perfume)`. An `Err` → issue.
     3. **Decide.**
        - **With any issue:** return `Err(ImportStale(details={"issues": [...]}))`. The
          `TransactionRunner` rolls everything back (`shared/application/transactions.py`).
        - **Otherwise:** `catalog_import.apply(now, actor_id)`, `imports.save`, and return
          `Ok(old_keys)`.

     After the commit, delete the old keys of the replaced photos quietly. Return `Ok(None)`.

     **The stale path is a rollback, not an exception.** Every repository write in the loop
     must happen inside the one transaction and use savepoints as today
     (`sql_perfume_repository.py:195`, `248`). A failed `add` or `save` therefore does not
     abort the remaining checks.

     **`DiscardCatalogImport(imports, storage, transactions, clock).execute(import_id) ->
     Result[None, DomainError]`:**
     - In a transaction: `get_for_update`. Missing → `ImportNotFound`; status not `READY` →
       `ImportNotReady`. An import that is ready but already past `expires_at` may still be
       discarded.
     - `keys = discard(now)`, then save.
     - After the commit, delete the keys quietly.
   - Observable result: with in-memory fakes, the confirm of a valid plan writes everything.
     A plan with one stale perfume writes nothing and returns every issue.

4. **Persistence: perfume photos and import columns**
   - Files: `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_perfume_repository.py`
     (modify),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_catalog_import_repository.py`
     (modify),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_catalog_import_queries.py`
     (modify),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/in_memory_perfumes.py`
     (modify),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/in_memory_imports.py`
     (modify),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/tables.py` (modify),
     `apps/api/migrations/versions/0011_catalog_import_applied.py` (create)
   - Do:
     - **`sql_perfume_repository.py`:**
       - `_photo_row` writes `source_url` and `_to_photo` reads it (`:114-136`).
       - `add` (`:191-205`) also inserts every photo, inside the same savepoint.
       - The upsert in `save` keeps setting only `position` on conflict, since `source_url` of
         an existing photo never changes.
     - **`tables.py`:** `catalog.imports` gains `applied_at` and `discarded_at` (both
       DateTime(tz), nullable) and `applied_by` (String(64), nullable).
     - **Migration:** `uv run just db-revision "catalog import applied"`, renamed `0011` like
       `0010`. Its `downgrade` drops the three columns.
     - **`sql_catalog_import_repository.py`:** `_to_row` and `_to_import` map the three
       columns (`:179-216`).
     - **`sql_catalog_import_queries.py`:** fills the new response fields.
     - **The in-memory fakes:** keep `source_url` and support photos on `add`, in line with
       the SQL repository.
   - Observable result: on the test DB, `alembic -x test=true check` is clean, and a
     downgrade/upgrade round trip works.

5. **Contracts and routes**
   - Files: `apps/api/src/fragancia_api/modules/catalog/contracts.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/http/import_router.py` (modify),
     `apps/api/openapi.json` (modify)
   - Do:
     - **`CatalogImportResponse`:**
       - `status` gains `"discarded"`;
       - it gains `applied_at: datetime | None` and `discarded_at: datetime | None`.
     - **Routes:**

       | Route | Answer | Errors |
       | --- | --- | --- |
       | `POST /{import_id}/confirm` | 200 `CatalogImportResponse` (status `applied`), read back through `GetCatalogImport` after the command | 404 `CATALOG_IMPORT_NOT_FOUND`; 409 `CATALOG_IMPORT_NOT_READY` or `CATALOG_IMPORT_STALE` (with `details.issues`) |
       | `POST /{import_id}/discard` | 204 | 404, 409 `CATALOG_IMPORT_NOT_READY` |

       `actor_id` comes from `request.state.actor.id`, as in `start_catalog_import`
       (`http/import_router.py:95`).
     - Run `uv run just openapi`.
   - Observable result: both routes show in `/api/v1/docs`, and anonymous calls get 401.

6. **Wiring and docs**
   - Files: `apps/api/src/fragancia_api/modules/catalog/module.py` (modify),
     `docs/architecture.md` (modify)
   - Do:
     - **`module.py`:** register `ConfirmCatalogImport`, built with `imports`, `perfumes`,
       `brands`, `families`, `concentrations`, `media`, `platform.transactions` and
       `platform.clock`, all already in `register` (`module.py:96-260`). Register
       `DiscardCatalogImport` too.
     - **`docs/architecture.md`:** extend the import sentence in "Events, outbox and jobs" with
       the confirm step: one transaction, all or nothing, and stale means 409.
   - Observable result: `uv run just arch` is green, and the container builds.

7. **Tests**
   - Files: `apps/api/tests/unit/catalog/` (modify), `apps/api/tests/integration/catalog/`
     (modify)
   - Do: the tester writes the layered tests; see "Test layers required".
   - Observable result: `plans-scope` is clean.

## Acceptance criteria

- [ ] **Confirm a valid import.** Confirming a `ready` import with new and existing perfumes
  returns 200 with `status: "applied"` and `applied_at` set.
  - The new perfumes exist with the pre-assigned ids, their presentations and their photos.
    The photos use the staged keys, which return 200 publicly, and carry `source_url`.
  - The existing perfume has the new price, and its existing presentation keeps its id.
  - "Publicado = Sí" perfumes are published and show in `GET /api/v1/perfumes`.
  - The sale window appears in the admin detail as validated: Cancún days.
- [ ] **Replace a photo.** Re-importing with a changed `Imagen 1` URL replaces photo 1 at the
  same position. The old objects return 404 after the confirm.
- [ ] **Same URL.** Re-importing the same URLs plans no photo, and confirming leaves the
  photos untouched.
- [ ] **Stale confirm.** Archiving a planned existing perfume between validating and
  confirming makes the confirm return 409 `CATALOG_IMPORT_STALE`, with an issue for its row.
  - Nothing from the file was written: the other perfumes and prices are unchanged.
  - The import stays `ready`.
- [ ] **Stale photo limit.** Adding a 3rd photo from the panel meanwhile, on a perfume that
  the import appends a photo to, also gives 409 `CATALOG_IMPORT_STALE`
  (`CATALOG_PHOTO_LIMIT_REACHED` in the issues).
- [ ] **Twice or not ready.** Confirming twice gives 200, then 409 `CATALOG_IMPORT_NOT_READY`.
  Confirming an `invalid`, `expired` or `discarded` import gives 409 `NOT_READY`. A `ready`
  import past `expires_at` (before the cron ran) also gives `NOT_READY`.
- [ ] **Discard.** Discarding a ready import gives 204, its status becomes `discarded` with
  `discarded_at` set, and its staged objects return 404. Discarding again gives 409.
- [ ] **Errors.** An unknown id returns 404 on both routes. Without a session, both return
  401.
- [ ] **Panel unchanged.** A photo uploaded from the panel still works, and its `source_url`
  is NULL.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `replace_photo` (position kept, removed id recorded, archived and unknown refused). `Perfume.create` with an id. `add_photo` with `source_url`. `CatalogImport.apply`, `discard` and `is_confirmable` transitions, including at `expires_at` exactly |
| application | yes     | `ConfirmCatalogImport` with fakes: the happy path for new and existing perfumes; every stale cause (archived perfume, identity taken, presentation gone or archived, ml taken, photo limit, replaced photo gone, reference archived for new and for changed family, publish refused) collected together; nothing saved on stale; the import is applied and the old keys deleted only on success. `DiscardCatalogImport` paths |
| http        | yes     | Both routes: 200 and 204, 404, 409 codes with `details.issues`, 401 and 403, the response fields |
| integration | yes     | Migration round trip. `add` with photos and `source_url` round trip. A full confirm against PostgreSQL that proves the rollback: a stale plan leaves every table unchanged. Concurrent confirm and expire, or confirm and confirm, with one winner. Replace with objects in RustFS |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

## Test coverage

## Review findings

## Verification
