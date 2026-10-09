---
status: verify
module: catalog
min_implementer: mid
depends_on: ["001"]
---

# 002 — Editable concentrations

## Context

**What exists** (plan 001, `done`). Olfactory families are an editable list in the `catalog`
module. Their parts:

- **Domain:**
  - The `FamilyName` value and the `OlfactoryFamily` aggregate
    (`apps/api/src/fragancia_api/modules/catalog/domain/olfactory_family.py:25-66`).
  - Name rules shared through `clean_name`/`slugify`, with `SLUG_MAX_LENGTH = 100`
    (`apps/api/src/fragancia_api/modules/catalog/domain/naming.py`).
  - Errors (`domain/errors.py:24-39`) and `OlfactoryFamilyRepository`
    (`domain/repositories.py:31-49`).
- **Application:**
  - Commands `create_olfactory_family.py`, `rename_olfactory_family.py:10-34` and
    `olfactory_family_status.py`, in `application/commands/`.
  - The port `OlfactoryFamilyQueries` (`application/ports.py:23-32`) and the queries
    `application/queries/list_olfactory_families.py`.
- **Infrastructure:**
  - Table `catalog.olfactory_families` with `FAMILY_SLUG_UNIQUE`
    (`infrastructure/tables.py:20-32`).
  - `SqlOlfactoryFamilyRepository`, which maps the unique violation inside a savepoint on
    `add` and `save` (`infrastructure/sql_olfactory_family_repository.py:47-86`).
  - `sql_olfactory_family_queries.py` and `InMemoryOlfactoryFamilies`
    (`infrastructure/in_memory.py:69-116`).
- **HTTP:** public `GET /olfactory-families`; admin list, create, `PATCH`, archive and
  restore under `catalog:manage` (`http/router.py:52-65,136-212`).
- **Wiring:** `module.py:58-79`.
- **Migration with seed:** `apps/api/migrations/versions/0005_catalog_olfactory_families.py`.

All paths above are relative to `apps/api/src/fragancia_api/modules/catalog/`, except the
migration.

**What we need.** README decisions 21, 26 and 27. Concentrations become a third editable list.
Unlike a family, a concentration has two texts: a **name** that the storefront shows ("Eau de
Toilette") and an **abbreviation** that plan 003 puts in the perfume URL ("EDT" →
`/perfumes/versace-eros-edt`). Both must be unique.

**Approach.** `Concentration` is a copy of `OlfactoryFamily` by shape, plus an `Abbreviation`
value with its own slug and its own unique constraint. Instead of a rename, an admin
**updates** both texts in one `PATCH` (both required): the pair is what a person edits.

I considered reusing the families table with a `kind` column. I discarded it for the same
reason plan 001 discarded a generic lookup table: plan 003 references each list by id with
different rules, and a concentration has a field a family does not.

Concentrations publish no events (nothing subscribes, the same as families).

**Imitated files, by name:** every `*olfactory_family*` file listed above, the family routes
`http/router.py:136-212`, the family wiring `module.py:58-79`, migration 0005, and the tests
`apps/api/tests/unit/catalog/test_olfactory_family_domain.py`,
`test_catalog_maintenance_commands.py`, `test_catalog_maintenance_http.py` and
`apps/api/tests/integration/catalog/test_sql_olfactory_families.py`.

**Rules fixed here.** These are derived from decisions 26 and 27 and the family rules.

- **Name:** exactly the family/brand rules (`clean_name`: 2–80 characters after trimming and
  collapsing whitespace, at least one letter or digit, slug ≤ 100). Two names with the same
  slug are the same concentration.
- **Abbreviation:** trimmed with inner whitespace collapsed, 2–12 characters, at least one
  letter or digit, slug ≤ 20 characters. It is kept as typed ("EDT", "Extrait"). Two
  abbreviations with the same slug ("EDT" and "edt") clash.
- Both slugs stay unique across active and archived rows. Re-creating an archived
  concentration is a 409; the admin restores it instead.
- An update that keeps either slug unchanged succeeds. An update to another row's name slug
  is a 409 `CATALOG_CONCENTRATION_ALREADY_EXISTS`. An update to another row's abbreviation
  slug is a 409 `CATALOG_CONCENTRATION_ABBREVIATION_TAKEN`. The name is checked first.
- Archive and restore are idempotent (204).

## Out of scope

- Perfumes, and what an archived concentration means for them (open for plan 003, see the
  README).
- A partial `PATCH` (only one of the two texts), sorting options, descriptions.
- Events and subscribers; deleting concentrations (README decision 10).
- Refactoring families and concentrations into a shared generic list.
- The web app.

## Dependencies

- **001** (`done`) provides `clean_name`, `slugify`, `NAME_*`/`SLUG_MAX_LENGTH`
  (`domain/naming.py`), the `CATALOG_MANAGE` router dependency (`http/router.py:52`) and the
  `TestActorResolver` with `catalog:manage` (`apps/api/tests/support.py`).

## Steps

1. **Contracts**
   - Files: `apps/api/src/fragancia_api/modules/catalog/contracts.py` (modify)
   - Do: after the family models (`contracts.py:47-78`) add:
     - `CreateConcentrationRequest` and `UpdateConcentrationRequest`, both with `name: str =
       Field(max_length=200, examples=["Eau de Toilette"])` and `abbreviation: str =
       Field(max_length=50, examples=["EDT"])`. Copy the comment from
       `CreateBrandRequest` (the rules live in the domain).
     - `PublicConcentration` with `id`, `name`, `abbreviation` and `slug`, where `slug` is
       the name slug.
     - `AdminConcentration` with `id`, `name`, `abbreviation`, `slug`, `is_active` and
       `created_at`.
     - `AdminConcentrationPage(Page[AdminConcentration])`.

     Each model gets a one-line docstring.
   - Observable result: `uv run just typecheck` passes.

2. **Domain**
   - Files: `apps/api/src/fragancia_api/modules/catalog/domain/naming.py` (modify), `apps/api/src/fragancia_api/modules/catalog/domain/concentration.py` (create), `apps/api/src/fragancia_api/modules/catalog/domain/errors.py` (modify), `apps/api/src/fragancia_api/modules/catalog/domain/repositories.py` (modify)
   - Do:
     - **`naming.py`:** add `ABBREVIATION_MIN_LENGTH = 2`, `ABBREVIATION_MAX_LENGTH = 12`,
       `ABBREVIATION_SLUG_MAX_LENGTH = 20` and `clean_abbreviation(raw: str) -> str | None`.
       It mirrors `clean_name`: trim and collapse whitespace, then return `None` when the
       length is outside 2–12 or when the slug is empty or longer than 20.
     - **`concentration.py`:**
       - `ConcentrationName` is a copy of `FamilyName` (`olfactory_family.py:25-38`) that
         returns `ConcentrationNameInvalid`.
       - `Abbreviation` is the same shape through `clean_abbreviation`, returning
         `ConcentrationAbbreviationInvalid` with details `{"min": 2, "max": 12}`.
       - `Concentration(AggregateRoot)` has `id`, `name`, `abbreviation`, `is_active` and
         `created_at`; the properties `slug` (the name slug) and `abbreviation_slug`; and
         the methods `create(name, abbreviation, *, created_at)` (active, no event),
         `update(name, abbreviation)`, `archive()` and `restore()`.
       - The module docstring states the "Rules fixed here".
     - **`errors.py`:**

       | Error | Category | Code | Message |
       | ----- | -------- | ---- | ------- |
       | `ConcentrationNameInvalid` | `InvalidValueError` | `CATALOG_CONCENTRATION_NAME_INVALID` | "A concentration name needs 2 to 80 characters, including letters or digits" |
       | `ConcentrationAbbreviationInvalid` | `InvalidValueError` | `CATALOG_CONCENTRATION_ABBREVIATION_INVALID` | "A concentration abbreviation needs 2 to 12 characters, including letters or digits" |
       | `ConcentrationAlreadyExists` | `ConflictError` | `CATALOG_CONCENTRATION_ALREADY_EXISTS` | "A concentration with this name already exists" |
       | `ConcentrationAbbreviationTaken` | `ConflictError` | `CATALOG_CONCENTRATION_ABBREVIATION_TAKEN` | "Another concentration already uses this abbreviation" |
       | `ConcentrationNotFound` | `NotFoundError` | `CATALOG_CONCENTRATION_NOT_FOUND` | "Concentration not found" |

     - **`repositories.py`:** add `ConcentrationRepository`, a copy of
       `OlfactoryFamilyRepository` with:
       - `exists_with_slug(slug, *, except_id=None)` and
         `exists_with_abbreviation(abbreviation_slug, *, except_id=None)`.
       - `add` and `save`, both returning `Result[None, ConcentrationAlreadyExists |
         ConcentrationAbbreviationTaken]`. `save` persists `name`, `slug`, `abbreviation`,
         `abbreviation_slug` and `is_active`.
       - `get_for_update`.
   - Observable result: `uv run just arch` is green.

3. **Application**
   - Files: `apps/api/src/fragancia_api/modules/catalog/application/commands/create_concentration.py` (create), `apps/api/src/fragancia_api/modules/catalog/application/commands/update_concentration.py` (create), `apps/api/src/fragancia_api/modules/catalog/application/commands/concentration_status.py` (create), `apps/api/src/fragancia_api/modules/catalog/application/ports.py` (modify), `apps/api/src/fragancia_api/modules/catalog/application/queries/list_concentrations.py` (create)
   - Do:
     - **`CreateConcentration(concentrations, transactions, clock).execute(name,
       abbreviation) -> Result[UUID, DomainError]`**, inside `transactions.run`:
       1. Validate the name, then the abbreviation; return the first invalid one as Err.
       2. `exists_with_slug` → Err `ConcentrationAlreadyExists`.
       3. `exists_with_abbreviation` → Err `ConcentrationAbbreviationTaken`.
       4. `Concentration.create`, then `add`, passing on its Err.

       Same shape as `create_olfactory_family.py`.
     - **`UpdateConcentration(concentrations, transactions).execute(concentration_id, name,
       abbreviation)`**:
       1. Validate both, as in create.
       2. `get_for_update`; Err `ConcentrationNotFound` if there is no row.
       3. The two `exists_*` checks with `except_id=concentration_id`.
       4. `update`, then `save`.

       Same shape as `rename_olfactory_family.py:19-34`.
     - **`ArchiveConcentration` and `RestoreConcentration`**: copies of
       `olfactory_family_status.py`.
     - **`ports.py`:** add `ConcentrationQueries` with `list_active() ->
       list[PublicConcentration]` and `list_all(*, page, size) -> AdminConcentrationPage`,
       both ordered by name, case-insensitive.
     - **`list_concentrations.py`:** `ListPublicConcentrations` and
       `ListAdminConcentrations`, copies of `list_olfactory_families.py`.
   - Observable result: `uv run just typecheck` and `uv run just arch` pass.

4. **Infrastructure**
   - Files: `apps/api/src/fragancia_api/modules/catalog/infrastructure/tables.py` (modify), `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_concentration_repository.py` (create), `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_concentration_queries.py` (create), `apps/api/src/fragancia_api/modules/catalog/infrastructure/in_memory.py` (modify)
   - Do:
     - **`tables.py`:** add `concentrations` with these columns:
       - `id` `Uuid` (primary key).
       - `name` `String(80)` and `slug` `String(100)`, unique.
       - `abbreviation` `String(12)` and `abbreviation_slug` `String(20)`, unique.
       - `is_active` `Boolean` and `created_at` `DateTime(timezone=True)`.

       Every column is not null. Add `CONCENTRATION_SLUG_UNIQUE =
       "uq_concentrations_slug"` and `CONCENTRATION_ABBREVIATION_UNIQUE =
       "uq_concentrations_abbreviation_slug"`.
     - **`SqlConcentrationRepository`:** a copy of `sql_olfactory_family_repository.py`. In
       `add` and `save`, a violation of `CONCENTRATION_SLUG_UNIQUE` maps to
       `Err(ConcentrationAlreadyExists())` and one of `CONCENTRATION_ABBREVIATION_UNIQUE` to
       `Err(ConcentrationAbbreviationTaken())`. Anything else re-raises.
     - **`SqlConcentrationQueries`:** a copy of `sql_olfactory_family_queries.py`, with the
       extra column.
     - **`in_memory.py`:** `InMemoryConcentrations` mirrors `InMemoryOlfactoryFamilies`. Its
       `add` and `save` refuse a name slug, then an abbreviation slug, that another id
       holds.
   - Observable result: `uv run just typecheck` passes.

5. **Migration with seed**
   - Files: `apps/api/migrations/versions/` (create)
   - Do:
     1. Run `uv run just db-revision "catalog concentrations"` and review the file by hand
        against `0005_catalog_olfactory_families.py`. It must have revision `0006` and
        `down_revision` `0005`. It creates `catalog.concentrations` with `pk_concentrations`,
        `uq_concentrations_slug` and `uq_concentrations_abbreviation_slug`, and the
        downgrade drops only that table.
     2. Seed the rows in `upgrade` exactly as 0005 does: a lightweight `sa.table`, then
        `op.bulk_insert` with `uuid.uuid7()`, `is_active=True` and `datetime.now(UTC)`.
        Write the slugs literally:

        | name | slug | abbreviation | abbreviation_slug |
        | ---- | ---- | ------------ | ----------------- |
        | Eau de Cologne | `eau-de-cologne` | EDC | `edc` |
        | Eau de Toilette | `eau-de-toilette` | EDT | `edt` |
        | Eau de Parfum | `eau-de-parfum` | EDP | `edp` |
        | Parfum | `parfum` | Parfum | `parfum` |
        | Extrait de Parfum | `extrait-de-parfum` | Extrait | `extrait` |

     3. Apply it with `uv run just db-migrate` and `uv run just db-migrate --test`.
   - Observable result: `uv run just psql -c "select name, abbreviation from
     catalog.concentrations"` returns the five rows. Downgrading to 0005 and upgrading again
     works on the test database.

6. **HTTP and wiring**
   - Files: `apps/api/src/fragancia_api/modules/catalog/http/router.py` (modify), `apps/api/src/fragancia_api/modules/catalog/module.py` (modify), `apps/api/src/fragancia_api/modules/catalog/__init__.py` (modify), `apps/api/openapi.json` (modify)
   - Do:
     - **Public route**, `public_router(prefix="/concentrations", tags=["catalog"])`:
       - `GET ""` → `list[PublicConcentration]`, the active ones ordered by name. Operation
         id `list_concentrations`.
     - **Admin routes**, `admin_router(prefix="/concentrations", tags=["catalog · admin"],
       dependencies=[Depends(require_permission(CATALOG_MANAGE))])`:
       - `GET ""`, paged as the family list (`list_all_concentrations`).
       - `POST ""` → 201 `CreatedResponse`; 409 `CATALOG_CONCENTRATION_ALREADY_EXISTS` or
         `CATALOG_CONCENTRATION_ABBREVIATION_TAKEN`; 422
         `CATALOG_CONCENTRATION_NAME_INVALID` or
         `CATALOG_CONCENTRATION_ABBREVIATION_INVALID` (`create_concentration`).
       - `PATCH "/{concentration_id}"`, body `UpdateConcentrationRequest` → 204; 404, 409,
         422 (`update_concentration`).
       - `POST "/{concentration_id}/archive"` and `"/{concentration_id}/restore"` → 204;
         404 (`archive_concentration`, `restore_concentration`).

       Each docstring lists its error codes, as at `router.py:163-164`. Append both
       routers to `routers`.
     - **`module.py`:** register the six use cases with the SQL adapters, as at
       `module.py:58-79`.
     - **`__init__.py`:** the docstring mentions concentrations.
     - Run `uv run just openapi`.
   - Observable result: `uv run just check` is green and `openapi.json` has the 6 new
     operations.

7. **Docs**
   - Files: `docs/architecture.md` (modify)
   - Do: the `catalog` row (`docs/architecture.md:55`) becomes "✔ Brands (create, rename,
     archive), olfactory families, concentrations. Next: perfumes, sizes (ml), prices,
     photos, availability: in stock / made to order".
   - Observable result: the row matches what was built.

8. **Tests (tester phase)**
   - Files: `apps/api/tests/unit/catalog/` (create), `apps/api/tests/integration/catalog/` (create)
   - Do: the layers in "Test layers required". In integration, an empty-table fixture must
     put the seed rows back afterwards, as `test_sql_olfactory_families.py` does.
   - Observable result: `uv run just check` and `uv run just test-integration` are green.

## Acceptance criteria

- [ ] After migrating, `GET /api/v1/concentrations` returns the five seeded concentrations
      with `name`, `abbreviation` and `slug`, ordered by name.
- [ ] `POST /api/v1/admin/concentrations {"name":" Body  Mist ","abbreviation":"Mist"}` →
      201. The public list shows `{"name":"Body Mist","abbreviation":"Mist","slug":"body-mist"}`.
- [ ] Creating `{"name":"eau de toilette","abbreviation":"X1"}` → 409
      `CATALOG_CONCENTRATION_ALREADY_EXISTS`. Creating `{"name":"Toilette Fraîche",
      "abbreviation":"edt"}` → 409 `CATALOG_CONCENTRATION_ABBREVIATION_TAKEN`. An abbreviation
      of 1 or 13 characters → 422 `CATALOG_CONCENTRATION_ABBREVIATION_INVALID`, and a
      one-character name → 422 `CATALOG_CONCENTRATION_NAME_INVALID`.
- [ ] `PATCH /api/v1/admin/concentrations/{id}` for Body Mist with `{"name":"Body Mist",
      "abbreviation":"BM"}` → 204 (same name slug). Changing its abbreviation to `"EDP"` →
      409 `..._ABBREVIATION_TAKEN`. An unknown id → 404 `CATALOG_CONCENTRATION_NOT_FOUND`.
- [ ] `archive` → 204: the concentration leaves the public list and the admin list shows
      `is_active: false`. A second `archive` is 204. `restore` → 204 and it is public again.
- [ ] The new admin routes return 401 without a session.
- [ ] Two concurrent updates that give two concentrations the same abbreviation → one 204 and
      one 409, never a 500.
- [ ] Migration 0006 applies on the development database. `openapi.json` is regenerated and
      the drift check passes.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `clean_abbreviation` bounds (2–12, slug ≤ 20, needs a letter or digit); `ConcentrationName`/`Abbreviation` errors and details; `create`/`update`/`archive`/`restore` |
| application | yes     | Create and update: name checked before abbreviation, each conflict, `except_id` keeps own slugs, not found, `save` Err passed on; archive/restore idempotent and not found |
| http        | yes     | Every new route: codes and error `code`s, 401, 403 without `catalog:manage`, public list only active, payload `max_length` |
| integration | yes     | Seed rows; both unique constraints mapped on `add` and `save` (transaction still usable); concurrent updates to one abbreviation; queries order and pagination |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

None. Step 8 (tests) is left to the tester phase by instruction of the main session. The
downgrade/upgrade round trip on the test database was not exercised by the implementer (only
`db-migrate` and `db-migrate --test` upgrades).

## Test coverage

Files (all new): `apps/api/tests/unit/catalog/test_concentration_domain.py` (34 tests),
`test_concentration_commands.py` (25), `test_concentration_http.py` (38) and
`apps/api/tests/integration/catalog/test_sql_concentrations.py` (17). No GAP and no NOT CONFIRMED.

Runs. Baseline: `just check` green (706 passed, 3 skipped); `just test-integration` green
(107 passed, 2 skipped). Closing: `just check` green (803 passed, 3 skipped, +97 unit);
`just test-integration` green (124 passed, 2 skipped, +17). The first closing run had two test
errors of mine (a mypy `no-any-return` in the commands test and an over-specific concurrency
assertion, see the last row); I fixed both and re-ran, so this phase used three full runs
instead of two.

Migration round trip, test database only (`alembic -x test=true`): `downgrade 0005` ran
`0006 -> 0005` and `current` showed `0005`; `upgrade head` ran `0005 -> 0006` and `current`
showed `0006 (head)`. The seed rows were then verified by
`test_migration_seeds_the_five_starting_concentrations` (green in the closing integration run).
CONFIRMED.

| Behavior | Source | Layer | Test | State |
| -------- | ------ | ----- | ---- | ----- |
| `clean_abbreviation`: trims, collapses, keeps case; 2-12 bounds inclusive; needs a letter or digit | `domain/naming.py:clean_abbreviation` | domain | `test_concentration_domain.py::test_clean_abbreviation_*` | CONFIRMED |
| Abbreviation slug above 20 characters rejected (NFKD expansion), exactly 20 accepted | `naming.py` (`ABBREVIATION_SLUG_MAX_LENGTH`) | domain | `::test_clean_abbreviation_rejects_a_slug_longer_than_20_characters` | CONFIRMED |
| `ConcentrationName` / `Abbreviation`: normalized, slugged, error code and `details` `{min, max}` | `domain/concentration.py` | domain | `::test_invalid_concentration_names`, `::test_invalid_abbreviations`, `::test_abbreviations_are_kept_as_typed_and_slugged` | CONFIRMED |
| `create` active, no event; `update` changes both texts and slugs, keeps id and flag; archive/restore idempotent | `domain/concentration.py` | domain | `::test_new_concentrations_*`, `::test_updating_*`, `::test_concentration_archive_and_restore_are_idempotent` | CONFIRMED |
| Create: name conflict, abbreviation conflict, name reported first, archived rows still clash, invalid text first (name before abbreviation) | `create_concentration.py` | application | `test_concentration_commands.py::test_a_name_with_the_same_slug_*`, `::test_an_abbreviation_*`, `::test_a_clash_on_both_*`, `::test_re_adding_*`, `::test_an_invalid_text_creates_nothing` | CONFIRMED |
| Create passes on an `add` Err (race) | `create_concentration.py` | application | `::test_create_passes_on_a_conflict_found_when_adding` | CONFIRMED |
| Update: both texts and slugs follow; own slugs via `except_id`; name conflict, abbreviation conflict, name first, archived rows clash; invalid text; not found; invalid before not found | `update_concentration.py` | application | `::test_updates_both_texts_*`, `::test_updating_*`, `::test_an_update_clashing_on_both_*`, `::test_an_invalid_text_is_reported_before_an_unknown_id` | CONFIRMED |
| Update passes on a `save` Err (race) | `update_concentration.py` | application | `::test_update_passes_on_a_conflict_found_when_saving` | CONFIRMED |
| Archive/restore idempotent; not found; public list active only ordered by name; admin list paged with archived | `concentration_status.py`, `list_concentrations.py` | application | `::test_archives_and_restores_*`, `::test_archiving_or_restoring_an_unknown_*`, `::test_public_list_*`, `::test_admin_list_*` | CONFIRMED |
| All 5 admin routes (list, create, patch, archive, restore): 401 without session, 403 without `catalog:manage` | `http/router.py:84-90,254-321` | http | `test_concentration_http.py::test_admin_routes_return_401_*`, `::test_admin_routes_return_403_*` | CONFIRMED |
| Create 201 + public payload `{id,name,abbreviation,slug}`; 409 `..._ALREADY_EXISTS`; 409 `..._ABBREVIATION_TAKEN`; 422 name/abbreviation codes, message and details | `router.py` `create_concentration` | http | `::test_create_returns_201_*`, `::test_duplicate_*`, `::test_invalid_*` | CONFIRMED |
| Payload `max_length` 200/50 and both fields required (create and update); malformed id | `contracts.py` | http | `::test_create_payload_is_bounded_*`, `::test_update_payload_is_bounded_*`, `::test_update_rejects_a_malformed_id` | CONFIRMED |
| PATCH 204 and lists show new texts; same name slug 204; 404/409/409/422/422 | `router.py` `update_concentration` | http | `::test_update_returns_204_*`, `::test_update_keeping_the_name_slug_is_204`, `::test_update_error_cases` | CONFIRMED |
| Archive/restore 204 idempotent, visibility in both lists, 404 | `router.py` | http | `::test_archive_hides_it_publicly_*`, `::test_archiving_or_restoring_an_unknown_*` | CONFIRMED |
| Public list ordered, active only, no session; admin list paged and its bounds | `router.py` | http | `::test_public_list_*`, `::test_admin_list_*` | CONFIRMED |
| Migration seeds the five rows (names, abbreviations, both slugs), ordered by name | `0006_catalog_concentrations.py` | integration | `test_sql_concentrations.py::test_migration_seeds_the_five_starting_concentrations` | CONFIRMED |
| Both unique constraints exist in the table | `tables.py:47-48` | integration | `::test_both_slugs_are_unique_in_the_table` | CONFIRMED |
| Create persists trimmed texts and slugs; duplicates (incl. archived) are conflicts | `sql_concentration_repository.py` | integration | `::test_create_persists_*`, `::test_duplicates_are_conflicts_including_archived_ones` | CONFIRMED |
| `add` and `save` map each constraint to its Err and the transaction stays usable | `sql_concentration_repository.py` | integration | `::test_add_maps_each_unique_violation_*`, `::test_save_maps_each_unique_violation_*` | CONFIRMED |
| `exists_*` honor `except_id`; `get_for_update` maps the row back | `sql_concentration_repository.py` | integration | `::test_exists_checks_can_ignore_one_row`, `::test_get_for_update_maps_*` | CONFIRMED |
| Update persists both texts and slugs; error cases leave the row intact | `update_concentration.py` | integration | `::test_update_persists_*`, `::test_update_error_cases` | CONFIRMED |
| Two concurrent updates to one abbreviation: one success, one `..._ABBREVIATION_TAKEN` (acceptance criterion); same for one name | `sql_concentration_repository.py` | integration | `::test_concurrent_updates_to_the_same_abbreviation_*`, `::test_concurrent_updates_to_the_same_name_*` | CONFIRMED |
| Concurrent creates: one success, the rest 409 (same name with different abbreviations: all `..._ALREADY_EXISTS`) | `sql_concentration_repository.py` | integration | `::test_concurrent_creation_of_the_same_concentration_*` | CONFIRMED |
| Concurrent creates clashing on both texts: one success, rest are a 409 of either code | `tables.py`, repository | integration | `::test_concurrent_creation_clashing_on_both_texts_*` | CONFIRMED (observation below) |
| Queries order by name case-insensitive, active filter, pagination; archive/restore visibility | `sql_concentration_queries.py` | integration | `::test_queries_order_by_name_*`, `::test_archive_and_restore_change_visibility_*`, `::test_archive_and_restore_of_an_unknown_*` | CONFIRMED |

Observation (not a defect, no finding): when a concurrent insert clashes on both texts at once,
PostgreSQL reports the abbreviation index first (observed: `CATALOG_CONCENTRATION_ABBREVIATION_TAKEN`
on every run), while the sequential path reports the name first (rule "The name is checked
first"). Both are 409, never a 500. I asserted the set of the two codes for that case only.

## Review findings

Reviewed 2026-10-09 against `git diff main...HEAD` (worktree clean; commits bad8405, b520c72,
fe533b3).

**Checklist: 15/16 passed, 1 not applicable, 0 failed.**

- [x] `plans-scope`: "22 declared, 26 changed (base main) ✔ Every change is inside the plan".
      No hot file touched (`container.py`, `docs/modules.json`, `.importlinter` unchanged).
- [x] `uv run just check` green: ruff, format, pre-commit, mypy (187 files), import-linter
      7/7 kept, plans-lint, harness-check, 803 passed / 3 skipped unit, 684 hook tests.
- [x] `uv run just test-integration` green: 124 passed, 2 skipped.
- [x] Business rules in `domain/` (`naming.py:clean_abbreviation`, `concentration.py`); the
      router, mapper and queries carry none.
- [x] CQRS-lite: the four commands run inside `transactions.run` and return `Result`; the
      queries go through `ConcentrationQueries` and return contract models; the repository
      has no screen methods.
- [x] Contracts in `contracts.py`; `openapi.json` has the 6 new operation ids and the drift
      test (`tests/unit/test_openapi.py`) passes.
- [x] Errors are `Err(DomainError)` with the five codes of the plan's table, messages
      verbatim; unknown integrity errors re-raise (`sql_concentration_repository.py:53`).
- [x] `created_at` from `Clock`, id from `new_id()`; no money involved.
- [x] Migration 0006 is new, `down_revision` 0005, `pk_`/both `uq_` names as planned, no
      cross-schema FK, downgrade drops only the table; seed matches the plan's table
      literally. Round trip on the test DB recorded by the tester.
- [x] Routes on `public_router`/`admin_router`; admin router carries
      `require_permission(CATALOG_MANAGE)`; 401/403 covered by tests.
- [x] Wiring in `module.py` registers the six use cases with SQL adapters; container test
      green; adapters created only in `module.py` (same as families).
- [x] No secrets or personal data.
- [x] `## Deviations` honest. Spot-check: "downgrade round trip not exercised by the
      implementer" is consistent with the tester recording that run separately.
- [x] Docs: `docs/architecture.md:55` updated as planned; `apps/api/README.md` and
      `docs/modules.json` don't list catalog sub-lists (nothing stale).
- [ ] N/A PR body: no PR exists yet for `feat/catalog-concentrations`. The main session must
      write the six sections when it opens one.

**Bug hunt.** I traced create and update from request → use case → domain → repository →
response. Name is validated before abbreviation, then the name conflict is checked before the
abbreviation conflict. `except_id` keeps a row's own slugs. Under concurrency, the unchecked
race between `exists_*` and the write is caught: the savepoint maps either unique constraint
to a 409, and integration covers the case. The row lock in `get_for_update` serializes
updates to the same row. Archive and restore can't violate a unique constraint because the
slugs don't change. Authorization is complete: no public route reaches admin data. No events,
money or time windows are involved.

**Findings**

- Blocker / high / medium: none.
- Nit (non-blocking, no change required for this plan):
  `apps/api/src/fragancia_api/modules/catalog/domain/naming.py:1-5`. The module docstring
  still says "Name rules shared by the catalog aggregates (brands, olfactory families)" and
  only describes names. The file now also holds the abbreviation rules
  (`ABBREVIATION_*`, `clean_abbreviation`, lines 44-56). Effect: a reader of `naming.py`
  alone misses the abbreviation bounds; behavior is unaffected. It can be folded into plan 003
  or a later touch of the file.
- Observation, carried from the tester and confirmed by reading the code (not a defect): when
  a concurrent insert clashes on both texts at once, the result can be
  `..._ABBREVIATION_TAKEN` instead of the sequential "name first" order. That depends on which
  index PostgreSQL checks first. It is still a 409, never a 500.

All blocking checks passed → `status: verify`.

## Verification
