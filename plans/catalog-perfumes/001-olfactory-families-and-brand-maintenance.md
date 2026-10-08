---
status: verify
module: catalog
min_implementer: mid
depends_on: []
---

# 001 — Olfactory families, and renaming and archiving brands

## Context

**What exists.** The catalog has one aggregate, `Brand`
(`apps/api/src/fragancia_api/modules/catalog/domain/brand.py:54-70`): a `BrandName` value
(trimmed, inner whitespace collapsed, 2–80 characters, non-empty slug — `brand.py:30-43`), a
slug that identifies it (`slugify`, `brand.py:24-27`), `is_active` and `created_at`. New brands
are active and record `BrandCreated` (`brand.py:46-51`, `brand.py:66-70`). The write side is
`BrandRepository` with `exists_with_slug` and `add` only
(`apps/api/src/fragancia_api/modules/catalog/domain/repositories.py:8-15`). `add` inserts
inside a savepoint and maps the `uq_brands_slug` violation to `BrandAlreadyExists`
(`apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_brand_repository.py:32-47`).
The read side is `BrandQueries.list_active`/`list_all`
(`apps/api/src/fragancia_api/modules/catalog/application/ports.py:6-15`,
`apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_brand_queries.py:14-37`). Routes:
`GET /brands`, `POST /admin/brands`, `GET /admin/brands`
(`apps/api/src/fragancia_api/modules/catalog/http/router.py:19-55`). The table is
`catalog.brands` (`apps/api/src/fragancia_api/modules/catalog/infrastructure/tables.py:8-19`,
migration `apps/api/migrations/versions/0002_catalog_brands.py:19-31`). Wiring is in
`apps/api/src/fragancia_api/modules/catalog/module.py:15-30`. Errors live in
`apps/api/src/fragancia_api/modules/catalog/domain/errors.py:6-15`.

**A gap found in recon.** The catalog's admin router requires an admin session
(`apps/api/src/fragancia_api/shared/http/access.py:67-73`) but no permission. Identity defines
`catalog:manage` and gives it to both roles
(`apps/api/src/fragancia_api/modules/identity/domain/user.py:80-86`), and its users routes are
gated with `require_permission(USERS_MANAGE)`
(`apps/api/src/fragancia_api/modules/identity/http/router.py:241-244`). Nothing is exposed
today, because every admin has `catalog:manage`. This plan adds many catalog admin routes, so it
gates the catalog admin router now (README decision 17). The test actor
(`apps/api/tests/support.py:18-26`) has no permissions (`Actor.permissions` defaults to empty,
`apps/api/src/fragancia_api/shared/application/actor.py:15-18`), so it must gain
`catalog:manage` or the existing catalog HTTP tests turn 403.

**What we need.** README decisions 5 and 11. The back office keeps an editable list of olfactory
families (create, rename, archive, restore; a public list of the active ones for the storefront
filters of plan 002). Brands gain rename, archive and restore.

**Approach.** `OlfactoryFamily` is a second aggregate in `catalog`. It is a copy of `Brand` by
shape: same name rules and slug identity, `is_active`, `created_at`, its own table and
repository. I considered a single generic "lookup list" table with a `kind` column shared by
brands and families. I discarded it: brands already have their own table and contract, and plan
002 references both by id with different rules (an archived brand vs. an archived family).
"Archived" is `is_active = false`, which brands already have. No new column, and the public
list already filters on it (`sql_brand_queries.py:14-22`). The name rules move to a small
`domain/naming.py` that both aggregates use, so they cannot drift.

**Writes on an existing row** follow identity's load-for-update / mutate / save shape
(`apps/api/src/fragancia_api/modules/identity/application/commands/user_status.py:65-80`,
`apps/api/src/fragancia_api/modules/identity/infrastructure/sql_user_repository.py:48-49,66-75`).
A rename can collide with another row's slug under concurrency, so `save` uses the same
savepoint + constraint-name mapping as `add`. Rename, archive and restore publish no events:
nothing subscribes, and the outbox is for events that must not be lost (ADR 0006). Family
creation does not publish an event either, for the same reason. `BrandCreated` stays as it is.

**Imitated files.** Commands: `application/commands/create_brand.py:12-44` (create) and
identity's `user_status.py:65-80` (load, mutate, save). Queries:
`application/queries/list_brands.py:5-22`. SQL adapters: `sql_brand_repository.py`,
`sql_brand_queries.py`. In-memory: `infrastructure/in_memory.py:9-47`. Routes:
`http/router.py:23-52` (lists, create) and identity's `router.py:256-288` (`POST
/{id}/deactivate`-style actions returning 204). Tests: `apps/api/tests/unit/catalog/` and
`apps/api/tests/integration/catalog/test_sql_brands.py`.

**Rules fixed here.** These are derived, not new business rules:

- A family name follows the brand rules exactly: 2–80 characters after trimming and collapsing
  whitespace, at least one letter or digit. Two names with the same slug are the same family.
- Slugs stay unique across active **and** archived rows. Re-adding an archived name gives 409;
  the admin restores it instead.
- A rename that keeps the same slug ("dior" → "Dior") succeeds. A rename to another row's slug
  gives 409. The slug changes with the name.
- Archive and restore are idempotent: archiving an archived row is a 204 that changes nothing.

## Out of scope

- Perfumes, presentations, photos and the import (plans 002–004), and what an archived brand
  or family means for perfumes (open question for plan 002, see the README).
- Families beyond the nine seeded in step 6 (README decision 19); the owner adds the rest from
  the panel.
- Events for rename/archive/restore or for family creation, and any subscriber.
- Deleting brands or families (README decision 10).
- A public "one brand by slug" endpoint, family descriptions or icons, and sorting options.
- Moving `catalog:manage` to a shared constant: identity keeps its own, catalog uses the string
  (permissions are atomic strings, identity-access decision 3; modules may not import each
  other).
- The web app.

## Dependencies

None

## Steps

1. **Contracts**
   - Files: `apps/api/src/fragancia_api/modules/catalog/contracts.py` (modify)
   - Do: add, next to the brand models and with the same shapes:
     - `RenameBrandRequest` (`name: str = Field(max_length=200, examples=["Dior"])`, same
       comment as `CreateBrandRequest` at `contracts.py:11-13`).
     - `CreateOlfactoryFamilyRequest` and `RenameOlfactoryFamilyRequest` (`name: str =
       Field(max_length=200, examples=["Amaderada"])`).
     - `PublicOlfactoryFamily` (`id`, `name`, `slug`) and `AdminOlfactoryFamily` (`id`, `name`,
       `slug`, `is_active`, `created_at`).
     - `AdminOlfactoryFamilyPage(Page[AdminOlfactoryFamily])`.
     Each model gets a one-line docstring like the brand ones.
   - Observable result: the models import; `uv run just typecheck` passes.

2. **Shared name rules and domain changes**
   - Files: `apps/api/src/fragancia_api/modules/catalog/domain/naming.py` (create), `apps/api/src/fragancia_api/modules/catalog/domain/brand.py` (modify), `apps/api/src/fragancia_api/modules/catalog/domain/olfactory_family.py` (create), `apps/api/src/fragancia_api/modules/catalog/domain/errors.py` (modify), `apps/api/src/fragancia_api/modules/catalog/domain/repositories.py` (modify)
   - Do:
     - `naming.py`: move `NAME_MIN_LENGTH`, `NAME_MAX_LENGTH`, `_NOT_ALPHANUMERIC` and
       `slugify` out of `brand.py:19-27` unchanged. Add `clean_name(raw: str) -> str | None`:
       it returns `" ".join(raw.split())` when that value has 2–80 characters and a non-empty
       slug, else `None`.
     - `brand.py` imports `slugify`, `clean_name`, `NAME_MIN_LENGTH` and `NAME_MAX_LENGTH`
       from `naming`. `slugify` stays importable from `brand`, because
       `apps/api/tests/unit/catalog/test_brand_domain.py:5` imports it from there.
       `BrandName.create` uses `clean_name` and returns the same `BrandNameInvalid` details.
       Update the module docstring, which says new brands are active, to also say that
       brands are renamed, archived and restored. Add three methods to `Brand`:
       `rename(name: BrandName) -> None` (replaces `self.name`), `archive() -> None`
       (`is_active = False`) and `restore() -> None` (`is_active = True`). No events.
     - `olfactory_family.py`: `FamilyName` (frozen dataclass, `create(raw) ->
       Result[FamilyName, FamilyNameInvalid]` through `clean_name`, a `slug` property) and
       `OlfactoryFamily(AggregateRoot)`. The aggregate has `id`, `name`, `is_active` and
       `created_at`; a `slug` property; `create(name, *, created_at)`, which makes it active
       with `new_id()` and records no event; and `rename`, `archive` and `restore` as on
       `Brand`. The module docstring states the rules from "Rules fixed here".
     - `errors.py` adds:
       - `BrandNotFound(NotFoundError)`: `CATALOG_BRAND_NOT_FOUND`, "Brand not found".
       - `FamilyNameInvalid(InvalidValueError)`: `CATALOG_FAMILY_NAME_INVALID`, "An olfactory
         family name needs 2 to 80 characters, including letters or digits".
       - `FamilyAlreadyExists(ConflictError)`: `CATALOG_FAMILY_ALREADY_EXISTS`, "An olfactory
         family with this name already exists".
       - `FamilyNotFound(NotFoundError)`: `CATALOG_FAMILY_NOT_FOUND`, "Olfactory family not
         found".
     - `repositories.py`, on `BrandRepository`:
       - `exists_with_slug(slug, *, except_id: UUID | None = None)` ignores the row with that
         id.
       - `get_for_update(brand_id) -> Brand | None` locks the row.
       - `save(brand) -> Result[None, BrandAlreadyExists]` updates `name`, `slug` and
         `is_active`. Its error is Err when another brand already has the slug, also under
         concurrent renames.

       Add `OlfactoryFamilyRepository` with the same five methods, typed for families and
       `FamilyAlreadyExists`.
   - Observable result: `uv run just arch` stays green (the domain imports only the kernel and
     itself). The existing brand domain tests pass unchanged.

3. **Application: brand commands**
   - Files: `apps/api/src/fragancia_api/modules/catalog/application/commands/rename_brand.py` (create), `apps/api/src/fragancia_api/modules/catalog/application/commands/brand_status.py` (create), `apps/api/src/fragancia_api/modules/catalog/application/commands/create_brand.py` (modify)
   - Do:
     - `RenameBrand(brands, transactions).execute(brand_id, name) -> Result[None,
       DomainError]`. Inside `transactions.run`:
       1. Validate with `BrandName.create`; return Err `BrandNameInvalid` when it fails.
       2. `get_for_update`; return Err `BrandNotFound` if there is no row.
       3. `exists_with_slug(new_slug, except_id=brand_id)`; return Err `BrandAlreadyExists`
          if taken.
       4. `brand.rename(...)`, then `save`, passing on its Err.
     - `brand_status.py` holds `ArchiveBrand` and `RestoreBrand(brands,
       transactions).execute(brand_id) -> Result[None, DomainError]`: `get_for_update`, Err
       `BrandNotFound` if there is no row, call `archive()`/`restore()`, then `save`.
     - In `create_brand.py`, change only the `exists_with_slug` call to keyword form if mypy
       needs it. No behavior change.
   - Observable result: the commands type-check. They follow the shape of
     `create_brand.py:28-44` and identity's `user_status.py:72-80`.

4. **Application: family commands, ports and queries**
   - Files: `apps/api/src/fragancia_api/modules/catalog/application/commands/create_olfactory_family.py` (create), `apps/api/src/fragancia_api/modules/catalog/application/commands/rename_olfactory_family.py` (create), `apps/api/src/fragancia_api/modules/catalog/application/commands/olfactory_family_status.py` (create), `apps/api/src/fragancia_api/modules/catalog/application/ports.py` (modify), `apps/api/src/fragancia_api/modules/catalog/application/queries/list_olfactory_families.py` (create)
   - Do:
     - `CreateOlfactoryFamily(families, transactions, clock).execute(name) -> Result[UUID,
       DomainError]` follows `create_brand.py:28-44` without the event publisher.
     - `RenameOlfactoryFamily`, `ArchiveOlfactoryFamily` and `RestoreOlfactoryFamily` mirror
       step 3 with the family errors.
     - In `ports.py`, add `OlfactoryFamilyQueries` with `list_active() ->
       list[PublicOlfactoryFamily]` and `list_all(*, page, size) -> AdminOlfactoryFamilyPage`.
       Both order by name, case-insensitive, with the same docstrings as `BrandQueries`.
     - `list_olfactory_families.py` holds `ListPublicOlfactoryFamilies` and
       `ListAdminOlfactoryFamilies`, mirroring `list_brands.py:5-22`.
   - Observable result: `uv run just typecheck` and `uv run just arch` pass.

5. **Infrastructure: table, SQL adapters, in-memory adapters**
   - Files: `apps/api/src/fragancia_api/modules/catalog/infrastructure/tables.py` (modify), `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_brand_repository.py` (modify), `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_olfactory_family_repository.py` (create), `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_olfactory_family_queries.py` (create), `apps/api/src/fragancia_api/modules/catalog/infrastructure/in_memory.py` (modify)
   - Do:
     - `tables.py`: add `olfactory_families` with the same columns, types and schema as
       `brands`, plus `FAMILY_SLUG_UNIQUE = "uq_olfactory_families_slug"`.
     - `SqlBrandRepository`:
       - `exists_with_slug(slug, *, except_id=None)` adds `brands.c.id != except_id` when an
         id is given.
       - `get_for_update` uses `select(brands).where(id).with_for_update()` and maps the row
         back to a `Brand`. Add a `_to_brand(row)` mapper: `BrandName(row["name"])` (already
         clean in the database), `is_active` and `created_at`.
       - `save` runs `update(brands).where(id).values(name=..., slug=..., is_active=...)`
         inside `session.begin_nested()`. A `BRAND_SLUG_UNIQUE` violation maps to
         `Err(BrandAlreadyExists())`, exactly like `add` (`sql_brand_repository.py:32-42`).
     - `SqlOlfactoryFamilyRepository` and `SqlOlfactoryFamilyQueries` copy the brand
       adapters for the new table.
     - `in_memory.py`: `InMemoryBrands` gains the same methods (`except_id`, `get_for_update`
       returning the stored object, and `save` that refuses a slug held by another id). Add
       `InMemoryOlfactoryFamilies`, mirroring `InMemoryBrands` for families.
   - Observable result: `uv run just typecheck` passes.

6. **Migration**
   - Files: `apps/api/migrations/versions/` (create)
   - Do: `uv run just db-revision "catalog olfactory families"`, then review the generated
     file by hand against `0002_catalog_brands.py:19-35`. It must have revision `0005`,
     `down_revision` `0004` (the head is
     `0004_identity_invitations_and_password_resets.py`), `create_table("olfactory_families",
     …, schema="catalog")` with `pk_olfactory_families` and `uq_olfactory_families_slug`, and
     a `downgrade` that drops only that table, not the schema.
     Seed the starting list (README decision 19) in the same `upgrade`, after `create_table`.
     Use `op.bulk_insert` on a lightweight `sa.table("olfactory_families", …,
     schema="catalog")`; do not import application code into the migration. Each row has
     `id=uuid.uuid7()` (standard library, Python ≥ 3.14), `is_active=True` and
     `created_at=datetime.now(UTC)`. These are the names, active and in this order, with
     their slugs:

     | name | slug |
     | ---- | ---- |
     | Amaderada | `amaderada` |
     | Floral | `floral` |
     | Oriental | `oriental` |
     | Cítrica | `citrica` |
     | Aromática | `aromatica` |
     | Gourmand | `gourmand` |
     | Acuática | `acuatica` |
     | Chipre | `chipre` |
     | Fougère | `fougere` |

     Write the slugs literally; they are what `slugify` produces for these names. Apply the
     migration with `uv run just db-migrate` and `uv run just db-migrate --test`.
   - Observable result: `uv run just psql -c '\d catalog.olfactory_families'` shows the
     table and `select name, slug from catalog.olfactory_families` returns the nine rows. A
     downgrade to 0004 followed by an upgrade succeeds against the test database.

7. **HTTP, permission and wiring**
   - Files: `apps/api/src/fragancia_api/modules/catalog/http/router.py` (modify), `apps/api/src/fragancia_api/modules/catalog/module.py` (modify), `apps/api/src/fragancia_api/modules/catalog/__init__.py` (modify), `apps/api/tests/support.py` (modify), `apps/api/openapi.json` (modify)
   - Do:
     - In `router.py`, define `CATALOG_MANAGE = "catalog:manage"` and pass
       `dependencies=[Depends(require_permission(CATALOG_MANAGE))]` to the brands admin router
       and to the new families admin router. Import `require_permission` from
       `fragancia_api.shared.http`, as identity does at `router.py:54`. Add these routes, each
       docstring listing its error codes as `router.py:40-41` does:
       - `PATCH /admin/brands/{brand_id}`, body `RenameBrandRequest`: 204; 404
         `CATALOG_BRAND_NOT_FOUND`, 409 `CATALOG_BRAND_ALREADY_EXISTS`, 422
         `CATALOG_BRAND_NAME_INVALID`. Operation id `rename_brand`.
       - `POST /admin/brands/{brand_id}/archive` and `POST
         /admin/brands/{brand_id}/restore`: 204; 404. Ids `archive_brand` and
         `restore_brand`.
       - `public_router(prefix="/olfactory-families", tags=["catalog"])` with `GET ""`, which
         returns `list[PublicOlfactoryFamily]` with the active families ordered by name. Id
         `list_olfactory_families`.
       - `admin_router(prefix="/olfactory-families", tags=["catalog · admin"],
         dependencies=[…CATALOG_MANAGE…])` with:
         - `GET ""` (paged as `router.py:45-52`; id `list_all_olfactory_families`).
         - `POST ""`: 201 `CreatedResponse`; 409 `CATALOG_FAMILY_ALREADY_EXISTS`, 422
           `CATALOG_FAMILY_NAME_INVALID`. Id `create_olfactory_family`.
         - `PATCH "/{family_id}"`: 204; 404/409/422. Id `rename_olfactory_family`.
         - `POST "/{family_id}/archive"` and `"/{family_id}/restore"`: 204; 404. Ids
           `archive_olfactory_family` and `restore_olfactory_family`.

       Append the two new routers to `routers`.
     - `module.py`: register every new use case with the SQL adapters, following
       `module.py:15-27`.
     - `__init__.py`: the docstring says "Catalog: brands, olfactory families (and, later,
       perfumes…)".
     - `tests/support.py`: `TestActorResolver` returns the actor with
       `permissions=frozenset({"catalog:manage"})`, so the existing catalog HTTP tests keep
       passing behind the new permission. Update its docstring.
     - Run `uv run just openapi`.
   - Observable result: `uv run just check` is green. `apps/api/openapi.json` lists the 10 new
     operations, and every `/api/v1/admin/brands*` and `/api/v1/admin/olfactory-families*`
     operation is protected (`assert_admin_routes_are_protected`).

8. **Docs**
   - Files: `docs/architecture.md` (modify)
   - Do: change the `catalog` row (`docs/architecture.md:55`) to "✔ Brands (create, rename,
     archive), olfactory families. Next: perfumes, sizes (ml), prices, photos, availability: in
     stock / made to order".
   - Observable result: the row matches what was built.

9. **Tests (tester phase)** — added after review, see "Resolution" under Review findings
   - Files: `apps/api/tests/unit/catalog/` (create), `apps/api/tests/integration/catalog/` (create)
   - Do: the layers in "Test layers required".
   - Observable result: `uv run just check` and `uv run just test-integration` green.

## Acceptance criteria

- [ ] After migrating, `GET /api/v1/olfactory-families` returns the nine seeded families
      ordered by name, case-insensitive (the same `lower(name)` order as brands; accented
      names follow the database collation).
- [ ] `POST /api/v1/admin/olfactory-families {"name":"  Especiada "}` → 201 `{id}`. The family
      appears in `GET /api/v1/olfactory-families` as `{"id","name":"Especiada","slug":"especiada"}`.
- [ ] Creating "amaderada" (seeded) → 409 `CATALOG_FAMILY_ALREADY_EXISTS`. A one-character
      name → 422 `CATALOG_FAMILY_NAME_INVALID`.
- [ ] `PATCH /api/v1/admin/olfactory-families/{id} {"name":"Especiada Cálida"}` → 204, and
      both lists show the new name and slug `especiada-calida`. Renaming it to an existing
      family's name → 409. An unknown id → 404 `CATALOG_FAMILY_NOT_FOUND`.
- [ ] `POST …/olfactory-families/{id}/archive` → 204. The family disappears from the public
      list, stays in the admin list with `is_active: false`, and a second archive is also
      204. `…/restore` → 204 and it is public again.
- [ ] `PATCH /api/v1/admin/brands/{id} {"name":"DIOR"}` on brand "Dior" → 204 (same slug).
      Renaming it to another brand's name → 409 `CATALOG_BRAND_ALREADY_EXISTS`. An unknown id
      → 404 `CATALOG_BRAND_NOT_FOUND`.
- [ ] `POST /api/v1/admin/brands/{id}/archive` hides the brand from `GET /api/v1/brands` and
      leaves it in `GET /api/v1/admin/brands` with `is_active: false`. `…/restore` brings it
      back.
- [ ] Every new admin route returns 401 without a session. A signed-in staff user (who has
      `catalog:manage`) can call them.
- [ ] Two concurrent renames of two brands to the same new name: one gets 204, the other
      409. Neither gets a 500.
- [ ] Migration 0005 applies on the development database and downgrades cleanly.
      `apps/api/openapi.json` is regenerated and the drift check passes.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `clean_name`/`FamilyName` rules (bounds, slug identity, accents); `rename`/`archive`/`restore` on both aggregates; `slugify` still importable from `brand` |
| application | yes     | Rename: invalid name, not found, slug taken by another row, same slug kept, `save` conflict passed on; archive/restore not found and idempotent; family create duplicate |
| http        | yes     | Every new route: status codes and error `code`s, 401 without a session, 403 for an admin actor without `catalog:manage`, the public family list only shows active ones, payload `max_length` |
| integration | yes     | `olfactory_families` unique slug; `save` maps a concurrent slug clash to Err (no 500, transaction intact); `exists_with_slug(except_id=…)`; queries order and pagination; brand archive hides from `list_active` |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

None. Notes (not deviations): `create_brand.py` needed no change (the positional
`exists_with_slug` call type-checks). `InMemoryBrands.by_id` is now typed `dict[UUID, Brand]`
(was `dict[object, Brand]`). The development database was at 0003 when the work began, so
`just db-migrate` first applied 0004 (existing migration) before `db-revision` could run.

## Test coverage

Baseline (before writing tests): `uv run just check` green (614 passed, 3 skipped unit; 684
harness), `uv run just test-integration` green (82 passed, 2 skipped). No GAP and no NOT
CONFIRMED items: every plan promise checked here is implemented. Files:
`tests/unit/catalog/test_olfactory_family_domain.py` (domain),
`tests/unit/catalog/test_catalog_maintenance_commands.py` (application),
`tests/unit/catalog/test_catalog_maintenance_http.py` (http),
`tests/integration/catalog/test_sql_brand_maintenance.py` and
`tests/integration/catalog/test_sql_olfactory_families.py` (integration). The DB-backed families
tests empty the table through a fixture that restores the seed rows afterwards, so the seed test
is order-independent.

| Behavior | Source | Layer | Test | State |
| -------- | ------ | ----- | ---- | ----- |
| `clean_name` trims, collapses, bounds 2-80, needs a slug | `naming.py:21-26` | domain | `test_olfactory_family_domain.py::test_clean_name_*` | CONFIRMED |
| `slugify` still importable from `brand` | `brand.py:23` | domain | `::test_slugify_is_still_importable_from_the_brand_module` | CONFIRMED |
| Family name rules, slug identity, error code/details | `olfactory_family.py:25-38` | domain | `::test_family_names_are_normalized_and_slugged`, `::test_names_with_the_same_slug_are_the_same_family`, `::test_invalid_family_names` | CONFIRMED |
| Family create (active, no event), rename (slug follows), archive/restore idempotent | `olfactory_family.py:55-66` | domain | `::test_new_families_are_active_*`, `::test_renaming_a_family_*`, `::test_family_archive_and_restore_*` | CONFIRMED |
| Brand rename/archive/restore, no events | `brand.py:68-75` | domain | `::test_renaming_a_brand_*`, `::test_brand_archive_and_restore_*` | CONFIRMED |
| RenameBrand: slug follows, same slug ok, conflict (active and archived), invalid, not found, save Err passed on | `rename_brand.py:17-32` | application | `test_catalog_maintenance_commands.py::test_renam*_brand_*`, `::test_rename_passes_on_a_conflict_found_when_saving` | CONFIRMED |
| Archive/Restore brand idempotent, not found | `brand_status.py:16-42` | application | `::test_archives_and_restores_a_brand_idempotently`, `::test_archiving_or_restoring_an_unknown_brand_is_not_found` | CONFIRMED |
| CreateOlfactoryFamily: active, duplicate (also archived), invalid | `create_olfactory_family.py:25-40` | application | `::test_creates_an_active_family`, `::test_a_family_name_with_the_same_slug_is_a_conflict`, `::test_re_adding_an_archived_family_is_a_conflict`, `::test_an_invalid_family_name_creates_nothing` | CONFIRMED |
| Rename/Archive/Restore family incl. not found, conflict | `rename_olfactory_family.py`, `olfactory_family_status.py` | application | `::test_renam*_family_*`, `::test_archives_and_restores_a_family_idempotently`, `::test_archiving_or_restoring_an_unknown_family_is_not_found` | CONFIRMED |
| Family public/admin list queries (order, active filter, pagination) | `list_olfactory_families.py` | application | `::test_public_family_list_*`, `::test_admin_family_list_*` | CONFIRMED |
| Every new admin route: 401 without session | `router.py:100-209` | http | `test_catalog_maintenance_http.py::test_new_admin_routes_return_401_without_a_session` | CONFIRMED |
| Every new admin route (and existing brand ones): 403 for an admin without `catalog:manage` | `router.py:52-65` | http | `::test_new_admin_routes_return_403_*`, `::test_existing_brand_admin_routes_also_require_catalog_manage` | CONFIRMED |
| Brand PATCH 204/409/404/422, payload `max_length`, bad id | `router.py:100-117` | http | `::test_rename_brand_*` | CONFIRMED |
| Brand archive/restore 204, idempotent, hide/show, 404 | `router.py:120-135` | http | `::test_archive_hides_a_brand_*`, `::test_archiving_or_restoring_an_unknown_brand_is_404` | CONFIRMED |
| Family POST 201/409/422, payload bounds | `router.py:156-167` | http | `::test_create_family_*`, `::test_duplicate_family_is_409`, `::test_invalid_family_name_is_422_*`, `::test_family_payload_is_bounded_and_required` | CONFIRMED |
| Family PATCH, archive/restore, public list only active, admin list paged | `router.py:138-209` | http | `::test_rename_family_*`, `::test_family_archive_hides_*`, `::test_public_family_list_*`, `::test_admin_family_list_*` | CONFIRMED |
| Migration seeds the nine families, ordered by name | `0005_catalog_olfactory_families.py` | integration | `test_sql_olfactory_families.py::test_migration_seeds_the_nine_starting_families` | CONFIRMED (against `fragancia_test`) |
| Unique slug on `olfactory_families` (also archived), concurrent create | `tables.py`, `sql_olfactory_family_repository.py:47-57` | integration | `::test_the_slug_is_unique_in_the_table`, `::test_duplicate_family_is_a_conflict_*`, `::test_concurrent_creation_*` | CONFIRMED |
| `exists_with_slug(except_id=)`, `get_for_update` mapping, `save` clash to Err with usable tx | `sql_*_repository.py` | integration | `::test_exists_with_slug_*`, `::test_get_for_update_*`, `::test_save_maps_a_slug_clash_*` (families and brands) | CONFIRMED |
| Two concurrent renames to one name: one Ok, one 409-Err, no exception | `sql_*_repository.py` `save` | integration | `::test_concurrent_renames_to_the_same_name_*` (families and brands) | CONFIRMED (at use-case level; HTTP status mapping covered in http layer) |
| Queries order/pagination; brand and family archive hides from the public list | `sql_*_queries.py` | integration | `::test_queries_order_by_name_*`, `::test_archive_*` | CONFIRMED |

Closing run: see the final report of the tester run (commands and results below).

## Review findings

Reviewed 2026-10-08 against `git diff main...HEAD` (95f6089 implementation, 3d66e2d tests).
The worktree was clean. No PR exists yet.

**Checklist: 13/14 applicable items pass. FAILED: plans-scope.** (The PR-body item does not
apply because there is no PR yet.)

- [ ] `uv run just plans-scope` — **FAILS (exit 1).** Five test files are out of scope:
      `apps/api/tests/unit/catalog/test_olfactory_family_domain.py`,
      `test_catalog_maintenance_commands.py`, `test_catalog_maintenance_http.py`, and
      `apps/api/tests/integration/catalog/test_sql_brand_maintenance.py`,
      `test_sql_olfactory_families.py`. Also "declared but unchanged": `create_brand.py`,
      which matches the Deviations note and is fine. No hot files changed.
- [x] `uv run just check` green: 705 passed and 3 skipped (unit); 684 passed (harness); lint,
      types, arch, plans-lint and adapter drift all pass.
- [x] `uv run just test-integration` green.
- [x] Business rules are in `domain/` (`naming.py`, `brand.py`, `olfactory_family.py`). The
      routers, mappers and queries contain none.
- [x] CQRS-lite. Commands run inside `transactions.run` and return `Result`. Queries go
      through `OlfactoryFamilyQueries` and `reader()`. The repositories have no
      screen-specific methods.
- [x] Contracts are in `contracts.py`. `openapi.json` is regenerated (`test_openapi`
      checks for drift) and has 9 new operations. The plan's step 7 says "10", but its own
      list adds up to 9, so the plan text is wrong.
- [x] Error codes are `CATALOG_BRAND_NOT_FOUND`, `CATALOG_FAMILY_NAME_INVALID`,
      `CATALOG_FAMILY_ALREADY_EXISTS` and `CATALOG_FAMILY_NOT_FOUND`, all as `Err` subclasses.
- [x] No money is involved. `created_at` comes from `Clock`, and ids come from `new_id()`.
      The migration seed uses `uuid.uuid7()` and `datetime.now(UTC)`, as the plan specified.
- [x] Migration 0005 is new and `down_revision` is 0004. It has no drops beyond its own
      table and no cross-schema foreign keys. On `fragancia_test`, `downgrade 0004` and then
      `upgrade head` both succeeded, and `alembic check` reported "No new upgrade operations
      detected".
- [x] Routes use `public_router`/`admin_router`. Both admin routers carry
      `require_permission("catalog:manage")` (`http/router.py:55-65`), and tests cover 401
      and 403.
- [x] Wiring in `module.py` resolves (`test_container` green). The module is registered once,
      and adapters are created only in `module.py`.
- [x] No secrets or personal data.
- [x] Deviations are honest. I spot-checked the claims that `create_brand.py` is unchanged
      and that `InMemoryBrands.by_id` is now `dict[UUID, Brand]`; both are true.
- [x] Docs: the `docs/architecture.md:55` row is updated (see Low 2 for one stale line).
- [-] PR body: N/A, no PR yet.

### Blocking (process): test files not declared in the plan

- `plans/catalog-perfumes/001-olfactory-families-and-brand-maintenance.md` `## Steps`: no
  step has a `Files:` line for `apps/api/tests/unit/catalog/` or
  `apps/api/tests/integration/catalog/`. The "Test layers required" table asks for tests in
  four layers, and the tester wrote them in exactly those directories. So the content is
  expected; the gap is that the plan never declared those paths. The result is that
  `plans-scope` exits 1, and the reviewer may not approve while a checklist item fails. No
  product code needs to change. The main session (with the user) needs to amend the plan:
  either a `Files:` line declaring `apps/api/tests/unit/catalog/` and
  `apps/api/tests/integration/catalog/`, as identity-access 001/002 did, or a recorded
  deviation. Then `plans-scope` needs to be re-run. After that, this review can be
  re-confirmed without another bug hunt.

### Bug hunt (correctness)

I traced each flow end to end, from validation through the use case, domain, SQL, `unwrap`
and the HTTP status. Rename validates first, then locks the row (`get_for_update`), then
runs `exists_with_slug(except_id)`, then saves inside a savepoint. When two brands are renamed
to the same name concurrently, the second UPDATE waits on the unique index, then raises
`uq_*_slug`, which is mapped to `Err` and returned as 409. The runner rolls back on `Err`.
Archive and restore are idempotent under the row lock. The public family list filters on
`is_active`. Admin routes are gated by both the session and `catalog:manage`. I found no
critical or high issues.

- **Low 1 (uncertain, not reproduced at runtime).** `domain/naming.py:59-64` bounds the
  name, not the slug, and the slug column is `String(100)` (`infrastructure/tables.py:26`).
  NFKD expands compatibility characters, so 80 × "Ⅷ" gives a 320-character slug. PostgreSQL
  then raises `StringDataRightTruncation`, which is a `DataError` that `save`/`add` do not
  catch. The response is 500 instead of 422 on family create and rename and on brand rename.
  The same gap already existed for brand create on `main`. Only admins can reach it, and
  only with unusual input. Logged as `plans/findings/catalog-slug-longer-than-column.md`. It
  is not a blocker for this plan, because the rule was "move the brand rules unchanged".
- **Low 2 (docs).** `docs/architecture.md:124-127` ("A request, end to end", `POST
  /api/v1/admin/brands`) lists only `require_admin` → 401/403. That route now also passes
  through `require_permission("catalog:manage")` → 403. This is a reference trace and is
  now incomplete. One line would fix it. The plan's step 8 did not list this line.
- **Info.** `## Test coverage` ends with "Closing run: see the final report of the tester run
  (commands and results below)", but nothing follows it. The tester's closing commands and
  results are not recorded in the plan. My own runs (above) cover the gap for review
  purposes.

Status stays `review`. The scope gap is a plan amendment, not a code repair. Low 2 is
optional, and if it is fixed, `docs/architecture.md` is already declared.

### Resolution (main session, 2026-10-08)

- **Blocking (scope):** the architect's omission. The plan now has step 9, which declares
  `apps/api/tests/unit/catalog/` and `apps/api/tests/integration/catalog/`. These are the
  layers the approved plan already required; no new scope. `plans-scope` re-run: clean.
- **Low 1:** left as the open finding `plans/findings/catalog-slug-longer-than-column.md`, for
  the user to triage. It already existed on `main` for brand create.
- **Low 2:** fixed. The request trace in `docs/architecture.md` now shows the
  `require_permission` → 403 step.
- **Info:** step 7 says "10 new operations" but there are 9; the review count is right.

Review passed → `verify`.

## Verification
