---
status: done
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

Implementation finished 2026-10-12. Every step was applied as written. Four small, cosmetic
deviations, each fixed forward:

1. **`in_memory_perfumes.py` is unchanged.** Its `add` already stores a deep copy of the whole
   aggregate, so the photos and their `source_url` are kept. `save` also stores the whole
   aggregate, so it already supports `replace_photo`. Nothing needed changing. `plans-scope`
   lists the file as declared but unchanged.
2. **Three existing tests asserted the old contract and were updated** (step 7 paths):
   - `tests/unit/catalog/test_catalog_import_domain.py`: the list of status wire values gains
     `discarded`, and the test is renamed to `..._eight_statuses_...`.
   - `tests/unit/catalog/test_catalog_import_http.py`: the two exact response shapes gain
     `applied_at` and `discarded_at`.
   - `tests/integration/catalog/test_sql_catalog_imports.py`: the reflected `catalog.imports`
     columns gain the three new columns.

   No new tests were written. The layered tests are the tester's job.
3. **The comment in `errors.py`** above the import errors now says "the first five are
   HTTP-level", because `ImportNotReady` and `ImportStale` sit next to `ImportNotFound`.
   `CATALOG_IMPORT_PRESENTATION_ARCHIVED`, which was only a report code until now, can also
   appear in the 409 `details.issues`.
4. **Choices the plan left open:**
   - **One issue per perfume.** Each perfume stops at its first issue, then the loop moves to
     the next perfume.
   - **Issue rows.** A photo or publish issue uses the perfume's `first_row`, because
     `PlannedPhoto` carries no row.
   - **No early identity check.** `ConfirmCatalogImport` does not call
     `exists_with_identity` up front. A taken identity or slug surfaces through the `Err` of
     `perfumes.add` / `perfumes.save`, as the plan states.
   - **The availability kind.** The plan stores it as a `str`, and the command passes it to
     `Availability.create` with `cast(AvailabilityKind, ...)`.

**Not verified here (for the tester and verifier):**
- No command was run end to end against PostgreSQL, so the rollback after a savepoint failure
  in the middle of the loop is untested.
- The unit fakes do not roll back: `InMemoryTransactionRunner` keeps the writes. Tests of
  "nothing saved on stale" need a snapshot runner, like the existing `_CommitSnapshot`.

Commands run: `uv run just check` green, and `uv run just test-integration` green (463
passed, 4 skipped). On the test DB, `alembic -x test=true check` is clean, and a
downgrade 0010 / upgrade head round trip works. `uv run just openapi` regenerated the schema.
Against `uv run just api`, an anonymous `POST …/confirm` and `POST …/discard` both answered
401, and the API was stopped afterwards.

### Repair round 1 (2026-10-12, main session: verify → implementing)

**Why.** The review left one advisory Low finding, now confirmed. `ConfirmCatalogImport`
locks existing perfumes in file row order (`commands/catalog_imports.py:249`, `:346`). Two
confirms of different imports that touch the same perfumes in opposite order can deadlock;
PostgreSQL aborts one of them, and that caller gets a 500. The user chose to fix it before
`done` (README decision 75). The verification of 2026-10-12 stays valid for everything else;
only the confirm path is re-verified.

**Repair spec.** Product code is `application/commands/catalog_imports.py` only.
1. In `ConfirmCatalogImport`, iterate the planned perfumes sorted by `perfume_id`, ascending
   UUID order. That covers new and existing perfumes alike, so every confirm takes row locks
   and unique-index entries in one global order.
2. Report the collected issues sorted by `(row, column or "")`, so the 409 body stays in file
   order whatever the processing order.
3. Add a one-line comment saying why the order matters.

**Done (implementer, 2026-10-12).** In `ConfirmCatalogImport.execute`
(`application/commands/catalog_imports.py`): the loop now iterates `sorted(plan.perfumes,
key=lambda p: p.perfume_id)` with a comment on why; before returning `ImportStale` the issues
are sorted by `(row or 0, column or "")`. No existing test pinned the old order, so none was
changed. `uv run just check` green (1824 unit passed, 3 skipped; 684 harness) and
`uv run just test-integration` green (475 passed, 5 skipped). No deviations from the spec.

Then set the status to `testing`. The tester adds these tests:
- unit: the processing order is ascending `perfume_id` even when the plan lists perfumes in
  another order, and the issues come out in row order;
- integration: two concurrent confirms of different ready imports that touch the same two
  existing perfumes in opposite file order both finish without a deadlock. One applies; the
  other applies or answers 409 `STALE`/`NOT_READY`, never a 500.

## Test coverage

Tester run, 2026-10-12. Baseline (before any test): `uv run just check` green (1709 unit
passed, 3 skipped; 684 harness) and `uv run just test-integration` green (463 passed, 4
skipped). No product code was touched.

**New files** (all under `apps/api/tests/`):

| Layer | File | Tests |
| --- | --- | --- |
| domain | `unit/catalog/test_catalog_import_confirm_domain.py` | 37 |
| application | `unit/catalog/test_confirm_catalog_import.py` | 58 |
| http | `unit/catalog/test_catalog_import_confirm_http.py` | 20 |
| integration | `integration/catalog/test_sql_catalog_import_confirm.py` | 12 pass + 1 skip |

**The two flagged gaps, closed.**
1. **Real rollback against PostgreSQL.**
   `test_a_stale_confirm_leaves_every_catalog_table_and_the_import_unchanged` runs a five-perfume
   plan on `fragancia_test`: a valid new perfume, a valid update, an archived perfume in the
   middle, a new perfume whose identity someone else holds (a unique violation inside the
   repository savepoint) and a valid new perfume last. A probing runner reads the same session
   right after the work and proves the valid rows (2 perfumes, 2 photos, the new price) really
   were written inside the transaction. Afterwards a full `SELECT *` dump of `perfumes`,
   `presentations`, `perfume_photos`, `perfume_slug_history` and `imports` is identical to the
   one taken before, and the import is still `ready` with its plan. A second test confirms the
   same import after the catalog is fixed, and a third proves an exception mid-loop rolls back
   too.
2. **Rollback-aware unit runner.** `_RollbackRunner` in `test_confirm_catalog_import.py`
   snapshots the perfume store, the slug history and the import store, restores them on `Err`
   or exception, and records the state it found right before undoing it (so the "nothing saved"
   assertions are proven non-vacuous: the valid perfumes were written first) and what storage
   had deleted when the transaction ended.

| Behavior (from plan / code) | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| `Perfume.create` keeps a given id, else generates one | `perfume.py:377-404` | domain | `test_catalog_import_confirm_domain.py::test_create_keeps_the_id_it_is_given`, `::test_create_without_an_id_generates_a_distinct_one` | CONFIRMED |
| `add_photo` stores `source_url` (None for panel callers) | `perfume.py:541-568` | domain | `::test_add_photo_stores_the_source_url`, `::test_add_photo_without_a_source_url_leaves_it_none_as_the_panel_does` | CONFIRMED |
| `replace_photo`: slot and positions 1..3 kept, old photo returned, only the old id recorded as removed, works on a full perfume | `perfume.py:570-601` | domain | `::test_replace_photo_2_of_3_keeps_positions_1_to_3_without_gaps_and_the_slot`, `::test_replace_photo_stores_the_new_photo_data_and_returns_the_old_photo`, `::test_replace_photo_records_only_the_old_id_as_removed`, `::test_replace_photo_works_on_a_full_perfume_without_hitting_the_limit` | CONFIRMED |
| `replace_photo` refuses an unknown old photo and an archived perfume, changing nothing | `perfume.py:584-588` | domain | `::test_replace_photo_refuses_an_unknown_old_photo_and_changes_nothing`, `::test_replace_photo_refuses_an_archived_perfume_and_changes_nothing` | CONFIRMED |
| `apply`: ready to applied, sets `applied_at`/`applied_by`, drops the plan; `ValueError` from the 7 other statuses | `catalog_import.py:245-251` | domain | `::test_apply_marks_a_ready_import_applied_with_who_and_when_and_drops_the_plan`, `::test_apply_raises_from_any_status_but_ready` | CONFIRMED |
| `discard`: ready to discarded, returns the 6 staged keys, drops the plan; `ValueError` from the 7 other statuses; allowed past `expires_at` | `catalog_import.py:253-260` | domain | `::test_discard_marks_a_ready_import_discarded_returns_the_staged_keys_and_drops_the_plan`, `::test_discard_raises_from_any_status_but_ready`, `::test_a_ready_import_past_its_expiry_can_still_be_discarded` | CONFIRMED |
| `is_confirmable`: true until the instant before `expires_at`, false at `expires_at` exactly and after, false for any other status | `catalog_import.py:237-243` | domain | `::test_a_ready_import_is_confirmable_until_the_instant_before_expires_at`, `::test_a_ready_import_is_not_confirmable_at_expires_at_exactly_nor_after`, `::test_an_import_that_is_not_ready_is_never_confirmable` | CONFIRMED |
| `ImportNotReady` / `ImportStale`: conflicts with their codes, stale carries details | `errors.py:246-256` | domain | `::test_not_ready_and_stale_are_conflicts_with_their_codes` | CONFIRMED |
| Confirm of a valid plan creates new perfumes with the planned ids, slug, presentations and photos (staged keys, `source_url`, order) | `catalog_imports.py:424-483` | application | `test_confirm_catalog_import.py::test_confirming_a_valid_plan_creates_new_perfumes_with_the_planned_ids_and_photos` | CONFIRMED |
| Import applied with actor and clock, plan dropped, transaction committed | `catalog_imports.py:400-402` | application | `::test_confirming_marks_the_import_applied_with_the_actor_and_the_clock` | CONFIRMED |
| Existing perfume updated, presentation keeps its id; a new presentation is added | `catalog_imports.py:485-594` | application | `::test_confirming_updates_an_existing_perfume_and_keeps_its_presentation_id`, `::test_confirming_adds_a_new_presentation_to_an_existing_perfume` | CONFIRMED |
| `publish` True publishes (sets `first_published_at`), False hides, None leaves the flag, for new and existing | `catalog_imports.py:476-479`, `552-557` | application | `::test_the_publish_column_publishes_hides_or_leaves_the_flag` | CONFIRMED |
| Replaced photo keeps its position; its objects are deleted after the commit, never the staged ones; no replacement deletes nothing | `catalog_imports.py:524-539`, `404-409` | application | `::test_a_replaced_photo_keeps_its_position_and_its_objects_go_after_the_commit`, `::test_a_confirm_without_replaced_photos_deletes_no_objects` | CONFIRMED |
| A failure deleting old objects is logged and does not undo the confirm | `catalog_imports.py:86-93` | application | `::test_a_failure_deleting_replaced_objects_does_not_undo_the_confirm` | CONFIRMED |
| Decision 23: an unchanged archived brand, family or concentration does not block an update | `catalog_imports.py:494-505` | application | `::test_an_unchanged_archived_family_brand_and_concentration_do_not_block_an_update` | CONFIRMED |
| Confirming twice: second is NOT_READY and changes nothing | `catalog_imports.py:388-389` | application | `::test_confirming_twice_applies_once_and_the_second_is_not_ready` | CONFIRMED |
| Not found; NOT_READY for the 7 other statuses; ready past or at `expires_at` is NOT_READY; one second before is confirmed; nothing written | `catalog_imports.py:385-389` | application | `::test_confirming_an_unknown_import_is_not_found`, `::test_confirming_an_import_that_is_not_ready_is_refused_and_writes_nothing` (7), `::test_a_ready_import_past_expires_at_is_not_ready_even_if_the_cron_did_not_run`, `::test_a_ready_import_at_expires_at_exactly_is_not_ready`, `::test_a_ready_import_one_second_before_expires_at_is_confirmed` | CONFIRMED |
| Every stale cause is an issue at the right row: archived perfume, perfume gone, identity taken, rename collision, presentation gone, presentation archived, ml taken, 4th photo, replaced photo gone, reference archived or missing (new), changed family/brand/concentration archived (existing), publish refused, invalid planned value, invalid presentation at its own row | `catalog_imports.py:415-594` | application | the 16 `test_stale_*` tests in `test_confirm_catalog_import.py` (3 parametrized) | CONFIRMED |
| All issues collected together in plan order, one per perfume; an issue carries row, column, code, message | `catalog_imports.py:390-399`, `336-346` | application | `::test_every_stale_row_is_reported_together_in_plan_order_one_issue_per_perfume`, `::test_an_issue_carries_row_column_code_and_message` | CONFIRMED |
| Nothing saved on stale (rollback-aware runner), import stays ready with its plan, no objects deleted; an exception rolls back too | `catalog_imports.py:398-399` | application | `::test_a_stale_confirm_saves_nothing_even_though_valid_perfumes_were_written_first`, `::test_a_stale_confirm_leaves_the_import_ready_with_its_plan_and_deletes_no_objects`, `::test_an_unexpected_exception_rolls_back_too_and_deletes_no_objects` | CONFIRMED |
| Discard: deletes staged keys after the commit, marks discarded, never touches the catalog; not found; not ready for 7 statuses; ready past `expires_at` still discardable; twice; delete failure ignored; applied cannot be discarded | `catalog_imports.py:597-630` | application | the 8 discard tests in `test_confirm_catalog_import.py` | CONFIRMED |
| Both routes: 401 without a session, 403 without `catalog:manage`, malformed id 422 | `import_router.py:116`, `137` | http | `test_catalog_import_confirm_http.py::test_confirm_and_discard_return_401_without_a_session`, `::..._403_...`, `::test_a_malformed_import_id_is_422_validation_error` | CONFIRMED |
| Confirm 200 with the full response (`applied`, `applied_at`, `discarded_at` null), actor from the session; 404; twice 200 then 409 NOT_READY; other statuses and past-expiry 409 | `import_router.py:116-134` | http | `::test_confirm_answers_200_with_the_applied_import_and_applies_the_plan`, `::test_confirm_of_an_unknown_import_is_404`, `::test_confirming_twice_is_200_then_409_not_ready`, `::test_confirming_an_import_that_is_not_ready_is_409_not_ready` (4), `::test_confirming_a_ready_import_past_expires_at_is_409_not_ready` | CONFIRMED |
| Stale confirm is 409 `CATALOG_IMPORT_STALE` with `details.issues` (row, column, code, message); import stays ready | `import_router.py:116-134`, `errors.py:252` | http | `::test_a_stale_confirm_is_409_with_the_issues_of_each_affected_row` | CONFIRMED |
| Discard 204 with no body, reads back as discarded with `discarded_at`; 404; twice 204 then 409; applied 409 | `import_router.py:137-147` | http | `::test_discard_answers_204_with_no_body_and_the_import_becomes_discarded`, `::test_the_discarded_import_reads_back_as_discarded_with_its_timestamp`, `::test_discard_of_an_unknown_import_is_404`, `::test_discarding_twice_is_204_then_409_not_ready`, `::test_discarding_an_applied_import_is_409_not_ready` | CONFIRMED |
| **Stale plan leaves every catalog table and the import unchanged (PostgreSQL)** incl. a savepoint failure mid-loop | `catalog_imports.py:381-409`, `sql_perfume_repository.py:193-216` | integration | `test_sql_catalog_import_confirm.py::test_a_stale_confirm_leaves_every_catalog_table_and_the_import_unchanged` | CONFIRMED |
| Same import confirms once the catalog is fixed; an exception mid-loop rolls back | same | integration | `::test_after_a_stale_confirm_the_same_import_confirms_once_the_catalog_is_fixed`, `::test_an_exception_in_the_middle_of_the_confirm_rolls_everything_back_too` | CONFIRMED |
| Full confirm writes new and existing perfumes, photos with `source_url`, keeps the presentation id, applies the import (`applied_by`, `applied_at`, plan NULL) | `catalog_imports.py:381-409` | integration | `::test_a_valid_confirm_writes_new_and_existing_perfumes_and_applies_the_import` | CONFIRMED |
| `add` inserts photos with `source_url` (panel photo stays NULL); `replace_photo` saved in the same position, old row gone | `sql_perfume_repository.py:193-216`, `243-300` | integration | `::test_add_stores_the_photos_with_their_source_url_and_a_panel_photo_stays_null`, `::test_replace_photo_is_saved_in_the_same_position_and_the_old_row_is_gone` | CONFIRMED |
| Replace with real objects in RustFS: old keys 404, staged keys 200; stale confirm deletes no object; discard deletes the staged objects | `catalog_imports.py:404-409` | integration | `::test_replacing_a_photo_deletes_the_old_objects_and_keeps_the_staged_ones_live`, `::test_a_stale_confirm_deletes_no_object_neither_old_nor_staged`, `::test_discard_deletes_the_staged_objects_and_marks_the_import_discarded` | CONFIRMED |
| Concurrent confirms: one winner, the rest NOT_READY (never STALE); confirm waits for an expiry holding the row, then is refused; the expiry job skips an import a confirm holds (SKIP LOCKED) | `sql_catalog_import_repository.py:225-235`, `255-262` | integration | `::test_concurrent_confirms_of_the_same_import_have_exactly_one_winner`, `::test_a_confirm_waits_for_the_expiry_that_holds_the_import_and_is_then_refused`, `::test_the_expiry_job_skips_an_import_a_confirm_is_holding` | CONFIRMED |
| Migration 0011: upgraded columns exist | `0011_catalog_import_applied.py` | integration | `test_sql_catalog_imports.py::test_the_migration_created_the_imports_table_index_and_source_url_column` (updated by the implementer) | CONFIRMED |
| Migration 0011 downgrade then upgrade round trip | `0011_catalog_import_applied.py` | integration | `test_sql_catalog_import_confirm.py::test_the_0011_migration_downgrades_and_upgrades_cleanly` (skipped) | NOT CONFIRMED (not automated: it would rewrite the shared test schema mid-suite; the implementer ran it by hand) |
| Acceptance criteria that need the running API with a real Excel file and image downloads end to end (replace via re-import, same URL plans no photo) | `import_validation.py` | e2e | none | NOT CONFIRMED here (verifier: phase 5). The same-URL rule belongs to the 007 validator; this plan's confirm only applies the plan it is given |

No GAP found: nothing the plan promises is missing in the code, and no assertion was weakened.
Observations that are not gaps: each perfume reports at most its first issue (a recorded
deviation), and a photo or publish issue carries the perfume's `first_row`, not the photo's.

### Repair round 1

Tester run, 2026-10-12, after the lock-order repair (commit 99065d3). Product code untouched.
Closing run: `uv run just check` green (1826 unit passed, 3 skipped; 684 harness) and
`uv run just test-integration` green (476 passed, 5 skipped). The implementer's run was 1824
unit and 475 integration, so the difference is exactly the three new tests.

| Behavior | Source | Layer | Test | Status |
| --- | --- | --- | --- | --- |
| Perfumes are processed in ascending `perfume_id` whatever the plan order (plan 9, 3, 5; the in-memory repository inserts in call order, so the stored order is 3, 5, 9) | `catalog_imports.py:393` | application | `test_confirm_catalog_import.py::test_perfumes_are_processed_in_ascending_perfume_id_whatever_the_plan_order` | CONFIRMED |
| Issues come out sorted by row although plan and processing order differ (plan rows 6, 4, 2; reported 2, 4, 6) | `catalog_imports.py:400` | application | `::test_issues_come_out_sorted_by_row_even_when_processing_order_differs` | CONFIRMED |
| Two concurrent confirms of different ready imports touching the same two existing perfumes in opposite file order finish without a deadlock; one applies, the other applies or answers STALE/NOT_READY, nothing raises (PostgreSQL) | `catalog_imports.py:393` | integration | `test_sql_catalog_import_confirm.py::test_confirms_touching_the_same_perfumes_in_opposite_order_do_not_deadlock` | CONFIRMED |

The integration test puts a gate on `get_for_update`: after its first perfume lock each
confirm waits up to 0.5 s for the other to hold its own first lock, which forces the
file-order interleaving that deadlocks. With the global order the second confirm is blocked on
the first lock, the gate times out and both finish. NOT CONFIRMED: that this test fails
against the pre-repair code was not run (the tester does not touch product code); it is argued
from the gate, not demonstrated.

## Review findings

Reviewer run, 2026-10-12, on `feat/catalog-import-confirm` (`git diff main...HEAD`, clean
worktree). The diff was read against the plan.

**Checklist: 14/14 applicable items passed** (the PR body item does not apply yet).
- [x] `uv run just plans-scope`: "Every change is inside the plan". `in_memory_perfumes.py` is
  declared but unchanged, as Deviation 1 explains. No hot files were touched: `module.py` only
  appends two `services.add`, and `container.py`, `docs/modules.json` and `.importlinter` are
  untouched.
- [x] `uv run just check` is green: 1824 unit passed, 3 skipped; 684 harness tests passed.
  The committed OpenAPI drift test (`tests/unit/test_openapi.py`) is part of it.
- [x] `uv run just test-integration` is green: 475 passed, 5 skipped.
- [x] Business rules live in the domain: `replace_photo`, `apply`, `discard` and
  `is_confirmable` are in the aggregates. The reference checks of decision 23 sit in the
  application layer, exactly as in `CreatePerfume` and `UpdatePerfume`. The router and mappers
  hold no rules.
- [x] CQRS-lite: `ConfirmCatalogImport` and `DiscardCatalogImport` go through the aggregate and
  repository inside one `transactions.run` and return a `Result`. The confirm route reads back
  through `GetCatalogImport`. No repository gained a screen-specific method.
- [x] Contracts: `CatalogImportResponse` in `contracts.py`. `openapi.json` was regenerated,
  and the drift test passes.
- [x] Errors: `ImportNotReady` is `CATALOG_IMPORT_NOT_READY` and `ImportStale` is
  `CATALOG_IMPORT_STALE`, both `ConflictError`, and the issues go in `details`. Unexpected
  `IntegrityError`s still raise (`_conflict`, `add`).
- [x] Money: `Price.create(price_cents)` and `Sale.create(sale_price_cents, starts, ends,
  regular=price)` take the planned integers and the tz-aware datetimes as they are. The plan
  JSON round-trips them with `isoformat`/`fromisoformat`, so the offset is kept. There is no
  float and no recomputation. `now` comes from `Clock`, and the planned ids are reused.
- [x] Migration `0011`: it only adds three nullable columns on `catalog.imports`, and its
  downgrade drops exactly those three. It has no cross-schema foreign key. The implementer ran
  the round trip by hand (see Deviations), and it is not automated.
- [x] Routes are on the existing `admin_imports` admin router, and the HTTP tests cover 401
  and 403.
- [x] Wiring: both commands are registered once, and `test_container` is green.
- [x] No secrets or personal data: the fixtures use `img.test` URLs and synthetic names.
- [x] Deviations are honest. Spot-checked: "one issue per perfume" matches `_create`/`_update`,
  which return on the first `Err`. The issue row of a photo or publish problem is `first_row`.
  `in_memory_perfumes.py` is indeed unchanged.
- [x] Docs: `docs/architecture.md` describes the confirm step, and decisions 72 to 74 are in
  the initiative README. `apps/api/README.md` holds no stale statement.
- [ ] PR body: not applicable yet, since no PR exists for this branch. The main session must
  write the six sections when it opens one.

**Bug hunt.** These points were traced end to end and found correct:
- **All or nothing.** Each `perfumes.add`/`save` runs in its own `begin_nested()` savepoint.
  A unique violation rolls back only that savepoint and becomes an issue, and the loop goes
  on. With any issue, `work` returns `Err`, and `SqlTransactionRunner.run` rolls back the
  outer transaction. That undoes every earlier successful savepoint and leaves the import
  `ready` with its plan. An exception rolls back too (`except BaseException`). The old keys
  are returned only from the `Ok` branch, so a stale or raising confirm deletes no object.
  The integration test proves this on PostgreSQL with a unique violation in the middle of the
  loop, followed by a valid perfume.
- **The deferred position constraint** (`uq_perfume_photos_position`, `INITIALLY DEFERRED`)
  is checked at COMMIT, outside any savepoint. The confirm cannot violate it:
  - `replace_photo` keeps the old position, and `save` deletes `removed_photo_ids` before the
    upsert;
  - `add_photo` appends at `len + 1` on a perfume locked `FOR UPDATE`.
- **Concurrency with expiry.** The confirm locks the import `FOR UPDATE` and re-checks
  `status == READY and now < expires_at`. The expiry job uses `SKIP LOCKED`, so it skips an
  import a confirm holds, and a confirm that waits behind an expiry sees `expired` and
  answers NOT_READY. Discard against confirm or expiry is serialized the same way, so staged
  objects are deleted at most once and never after they went live.
- **Concurrency with panel edits.** Panel photo and presentation commands take
  `get_for_update` on the perfume, as the confirm does, so they serialize:
  - a panel remove of a photo the import replaces → `PhotoNotFound` issue;
  - a panel 3rd photo → `PhotoLimitReached` issue;
  - a concurrent create of the same identity blocks on the unique index, then fails in the
    savepoint and becomes an issue.
- **Photo replace.** The new photo takes the old slot and position, and the old id is
  recorded for deletion. The old keys (`perfumes/{perfume}/{old photo}-…`) never equal the
  staged keys, because the photo ids differ. They are deleted quietly after the commit only.
- **Panel photo behaviour is unchanged.**
  - `Photo` and `add_photo` gained `source_url` as a trailing defaulted parameter.
  - `_photo_row` writes NULL for panel photos.
  - The `save` upsert still sets only `position` on conflict.
  - `add` inserts photos only when the aggregate has some, and panel `CreatePerfume` never
    does.

Findings by severity:

- **Blocker / High / Medium:** none.
- **Low (advisory, no change required for this plan; uncertain how likely it is):**
  `application/commands/catalog_imports.py:249` and `:346`. The confirm locks perfumes
  (`get_for_update`) in file order, so two concurrent confirms of *different* ready imports
  that update the same existing perfumes in opposite row order can deadlock.
  - Failure scenario: admin 1 confirms import X (rows: P1, P2) while admin 2 confirms import
    Y (rows: P2, P1). PostgreSQL aborts one transaction with a deadlock error. That error is
    not an `IntegrityError`, so it raises, and the loser gets a 500 instead of a 409.
  - Data stays consistent: the loser's transaction rolls back, its import stays `ready`, and
    a retry succeeds.
  - It needs two admins confirming overlapping files at the same moment. If it ever matters,
    applying the planned perfumes in `perfume_id` order would remove it (a design choice for
    the user, not a fix made here).
- **Observations (by design, not findings):**
  - A panel edit to fields, price or sale of a planned perfume between validating and
    confirming is overwritten by the file. Decision 72 lists only refusals as stale.
  - If the panel reorders photos meanwhile, the replacement takes the replaced photo's
    *current* position, as step 1 specifies.
  - `Perfume.create` uses `id or new_id()`. A `UUID` is always truthy, so this is correct.

**Result: all checks passed; no finding needs a code change.** Status → `verify`.
*(Superseded for the confirm path by the re-review below.)*

### Re-review after repair round 1

Reviewer run, 2026-10-12, scope `git diff b860461..HEAD` (commits 99065d3 and 2e97a6f). The
worktree was clean.

**Checklist: all applicable items passed.**
- `plans-scope` is clean.
- `uv run just check` is green: 1826 unit passed, 3 skipped; 684 harness tests passed.
- `uv run just test-integration` is green: 476 passed, 5 skipped.
- The product change matches the repair spec:
  - it touches only `application/commands/catalog_imports.py`;
  - it iterates `sorted(plan.perfumes, key=perfume_id)`, with a one-line comment;
  - it sorts the issues by `(row or 0, column or "")` before `ImportStale`.
- Contracts, migrations and wiring are unchanged. The implementer's "Done" note matches the
  code.
- The PR body is still not applicable, because no PR exists yet.

**Does the new integration test fail against the pre-repair order? Yes, demonstrated.**
- **Method.** A pytest plugin in the session scratchpad (`oldorder_plugin.py`, loaded with
  `-o pythonpath=<scratchpad> -p oldorder_plugin`) shadows `sorted` in the `catalog_imports`
  module only. For a list of `PlannedPerfume` it returns the file order unchanged, and every
  other call goes to the builtin. The worktree was not edited.
- **Result.** The plugin was applied (it logged both confirms).
  `test_confirms_touching_the_same_perfumes_in_opposite_order_do_not_deadlock` **failed in
  1.45 s** with `psycopg.errors.DeadlockDetected` on `SELECT … FROM catalog.perfumes … FOR
  UPDATE`: each process waited for the other's transaction. Without the plugin, the same test
  **passes** in 0.69 s.
- **Conclusion.** The gate forces the interleaving deterministically, and the test is a real
  regression test for this repair. The tester's "NOT CONFIRMED" for this point is now
  confirmed by the reviewer.

**Bug hunt on the repair.**
- **Issue order.** It is still one issue per perfume, and `first_row`s are distinct, so the
  sort gives file order. `row or 0` is only a guard, since every confirm issue carries a row.
  The 409 body is unchanged except for its order, which is now always file order.
- **All or nothing is untouched.** The loop body, the savepoints, the rollback and the
  old-key handling are unchanged.
- **Lock ordering for existing perfumes** is now global, which removes the reported deadlock.

**Findings.**
- **Blocker / High / Medium:** none.
- **Low (advisory, uncertain how likely; not demonstrated):**
  `application/commands/catalog_imports.py:393`. The repair spec says the order also covers
  "unique-index entries". It does not fully, because two imports that create the *same* new
  identity give it different pre-assigned `perfume_id`s, so their inserts sort differently.
  - Failure scenario: import X creates new perfume N_x (id `a`) with identity I and updates
    existing P (id `b`, `a < b`). Import Y creates N_y (id `c`, `c > b`) with the same
    identity I and updates P.
    - X inserts N_x, then waits for P's lock.
    - Y holds P, then its insert of N_y waits on X's uncommitted index entry.
    - PostgreSQL detects a deadlock, and the loser gets a 500 instead of a 409 `STALE`.
  - Impact: the loser would have been stale anyway, the data stays consistent through the
    rollback, and a retry answers 409. It needs two concurrent confirms that create the same
    new perfume *and* share an existing one.
  - No change is required for this plan. Either record it or accept it.

**Result: the repair is correct and its tests are real; no finding needs a code change.**
Status → `verify`, so the verifier re-checks the confirm path.

## Verification

**2026-10-12, verifier (main session, inline).**

**Setup.** Branch `feat/catalog-import-confirm` at `f1eaed8`. The API and worker ran through
the scratchpad launcher with `CATALOG_IMPORT_IMAGE_ALLOW_PRIVATE_HOSTS=true` and the default
60-minute TTL. Images were served by `python -m http.server 8765`. The owner was
`verifier007@example.test`, and the plan 007 QA data was reused ("QA Versace", "QA
Existente").

**A leftover from implementation.** A `uvicorn --reload` API started at 11:31 by the
implementer subagent still held port 8100, although its report said it was stopped. The
first QA upload went through it; that upload was then rejected for a mistake in the QA file
(see A below). I stopped it (`kill 1135317`) and restarted the API with known settings.

**Suites.**
- `uv run just check`: "All checks passed!", "no issues found in 252 source files", "1824
  passed, 3 skipped", "684 passed", "plans OK", "24 adapters up to date".
- `uv run just test-integration`: "475 passed, 5 skipped".
- The dev DB is at `0011`.
- On the test DB, `alembic -x test=true downgrade -1` gives "Running downgrade 0011 -> 0010",
  `upgrade head` gives "Running upgrade 0010 -> 0011", and `check` gives "No new upgrade
  operations detected."

**Acceptance criteria.**
- [x] **Confirm a valid import.** `c_valid` (a new "QA Confirma" EDP with 50 and 100 ml and
  images a/b, plus "QA Existente" 100 ml at $2,100 with images a and c) went `ready`, then
  `POST …/confirm` returned 200 `applied` with `applied_at` set.
  - The new perfume id equals the plan's `perfume_id`, and the perfume is published.
  - 50 ml: `price=189950`, `sale=150000`, `2026-11-01T05:00:00Z -> 2026-12-01T05:00:00Z`, which
    is 00:00 Cancún to the end of 30/11. 100 ml: `249900`, no sale.
  - Photos 1 and 2 have `src` a.jpg and b.png, and their cards return 200.
  - The existing 100 ml is `price=210000`, with the same id as planned.
  - The existing perfume gained photo 2 (`src` c.jpg, card 200); photo 1 is kept.
  - `GET /api/v1/perfumes?q=QA Confirma` lists it.
  - Note: my first QA file left the images off the 100 ml row. Validation correctly answered
    `PERFUME_MISMATCH` on Imagen 1 and 2 (decision 61); the file was fixed and re-run.
- [x] **Replace a photo.** `c_replace` with Imagen 1 = c.jpg planned `pos=1 replaces=<old
  id>`. The confirm returned 200. After it, the old photo 1 card returns 404, the new photo 1
  (`src` c.jpg) returns 200, and photo 2 keeps its id ("photo 2 untouched: True").
- [x] **Same URL.** `c_same` with the same URLs has `photos_new=0`. The confirm returned 200
  and the photo ids are unchanged.
- [x] **Stale confirm.** `c_stale` held "QA Stale Nuevo" (new) and "QA Existente" at $77.77.
  "QA Existente" was archived after validation.
  - The confirm returned 409 `CATALOG_IMPORT_STALE` with the issue `row 3 |
    CATALOG_PERFUME_ARCHIVED`.
  - "QA Stale Nuevo" has 0 rows, the existing 100 ml price is still 210000, and the import is
    still `ready`.
  - The perfume was restored afterwards (204).
- [x] **Stale photo limit.** "QA Existente" had 2 photos, and `c_limit` appended Imagen 3.
  After validation, a panel upload added a 3rd photo (201). The confirm returned 409 `STALE`
  with the issue `row 2 | CATALOG_PHOTO_LIMIT_REACHED`.
- [x] **Twice or not ready.**
  - Confirming `c_valid` again gives 409 `CATALOG_IMPORT_NOT_READY`.
  - Confirming the invalid import from plan 007's QA gives 409 `NOT_READY`, and so does
    confirming the expired one.
  - The case of a ready import past `expires_at` before the cron runs was not driven live. It
    is covered by the unit and integration tests in the matrix.
- [x] **Discard.** `c_discard` staged objects returned [200, 200]. `POST …/discard` gave
  204, then `status=discarded` with `discarded_at` set, and the objects return [404, 404].
  Discarding again gives 409 `NOT_READY`, and confirming the discarded import gives 409
  `NOT_READY`.
- [x] **Errors.** An unknown id gives 404 `CATALOG_IMPORT_NOT_FOUND` on both routes, and an
  anonymous call gives 401 on both.
- [x] **Panel unchanged.** The panel upload in the photo-limit check returned 201 and stored
  `source_url` NULL, next to the imported photos' URLs.

**Reviewer Low: deadlock between two confirms.** Two confirms of *different* imports that
lock the same perfumes in opposite order could deadlock, and the loser would get a 500. Not
reproduced live. It is left to the user to decide.

**NOT VERIFIED:**
- 403 for an admin without `catalog:manage` (no such role exists yet; covered by the HTTP
  tests);
- the deadlock above;
- the expiry-boundary case live.

**Cleanup.**
- The two imports still ready from the stale checks were discarded through the API (204, 204),
  so their staged objects are gone.
- The API, worker and image server are stopped, and ports 8100 and 8765 are free.
- The QA perfumes stay in the dev DB, since the catalog never deletes.

**2026-10-12, re-verification after repair round 1 (main session, inline).**

Branch at `37ef82e`. The reviewer reports `uv run just check` (1826 passed, 3 skipped; 684
harness) and `uv run just test-integration` (476 passed, 5 skipped). The reviewer also showed
that the new concurrency test deadlocks with the old file order (`DeadlockDetected`) and passes
on HEAD. The API and worker ran with private hosts allowed.
- Confirming the same URLs (`c_same`): 200 `applied`, and the photo ids are unchanged.
- Stale confirm (`c_stale`, "QA Existente" archived after validation): 409
  `CATALOG_IMPORT_STALE` with `row 3 | CATALOG_PERFUME_ARCHIVED`. "QA Stale Nuevo" has 0
  rows, the existing price is still 210000, and the import is still `ready`. The perfume was
  restored afterwards (204). The leftover ready import was discarded (204).
- Every earlier criterion stays valid. The API, worker and image server are stopped, and ports
  8100 and 8765 are free.

**New advisory Low from the re-review** (not reproduced): two imports that both create the
*same* new perfume, and both update the same existing perfume, can still deadlock. The new
perfume gets a different pre-assigned id in each import. The loser gets a 500 instead of the
409 it would get anyway, and the data stays consistent. Recorded as finding
`catalog-import-confirm-same-new-perfume-deadlock` for the user to triage.

**Result:** every acceptance criterion that can be exercised locally passes. Ready for the
user to mark `done`.
