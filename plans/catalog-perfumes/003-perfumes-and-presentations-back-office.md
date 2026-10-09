---
status: review
module: catalog
min_implementer: high
depends_on: ["001", "002"]
---

# 003 — Perfumes and presentations in the back office

## Context

**What exists** (plans 001 and 002, both `done`). The `catalog` module has three editable
lists. Each has an aggregate, a repository with `exists_with_slug`/`add`/`get_for_update`/
`save`, SQL and in-memory adapters, and admin routes gated by `catalog:manage`:

- **Brands:** `domain/brand.py`, `infrastructure/sql_brand_repository.py`.
- **Olfactory families:** `domain/olfactory_family.py`,
  `infrastructure/sql_olfactory_family_repository.py`.
- **Concentrations:** `domain/concentration.py:35-72`,
  `infrastructure/sql_concentration_repository.py:57`.

Paths are under `apps/api/src/fragancia_api/modules/catalog/`. The repository protocols are
in `domain/repositories.py:16-90`. Name rules (`clean_name`, `slugify`, `SLUG_MAX_LENGTH`)
are in `domain/naming.py`. `Money` (integer cents, MXN) is in
`apps/api/src/fragancia_api/shared/kernel/money.py:8-36`, and `BusinessRuleViolationError` (422)
is in `shared/kernel/errors.py:38-40`. The admin routers share `CATALOG_MANAGE`
(`http/router.py:70`) and the module wires everything in `module.py:60-125`. The router file
is already 321 lines long, and `infrastructure/in_memory.py` holds three adapters
(`in_memory.py:28,78,128`). Foreign keys inside one module schema are allowed; identity uses
them (`apps/api/src/fragancia_api/modules/identity/infrastructure/tables.py:27`). The last
migration is `apps/api/migrations/versions/0006_catalog_concentrations.py`.

**What we need.** README decisions 2–3, 6–10, 23–25, 28, 32 and 35–37. The back office
creates perfumes, edits them, publishes and hides them, archives and restores them, and
manages their presentations. The storefront (plan 004), photos (005) and the import (006)
build on this.

**Approach.** `Perfume` is one aggregate, and its `Presentation`s are child entities saved
with it. The rules that span presentations (unique ml, publish needs an active one, the last
active one of a published perfume cannot be archived) are then checked in one place. Every
command loads the perfume with its row locked (`get_for_update`), so two admins editing the
same perfume are serialized. The repository persists presentations with a PostgreSQL upsert
by id (`insert … on conflict (id) do update`), because presentations are never deleted.

I considered making presentations their own aggregate, so that they can be edited without
locking the perfume. I discarded it: the publish and last-presentation rules would then need
cross-aggregate locking.

The use case reads the brand, family and concentration (new plain `get` on their
repositories) to check that they are active (decision 23) and to build the slug from the
brand name and the concentration abbreviation (decisions 31 and 37). An archived reference
that is not being changed is allowed. No events are published: nothing subscribes yet, and
the publish events can come with the plan that needs them.

New perfume code goes in **new files** (`http/perfume_router.py`,
`infrastructure/in_memory_perfumes.py`) so the existing ones do not keep growing.

**Imitated files:**

- Commands: `application/commands/update_concentration.py:14` (validate → `get_for_update`
  → mutate → `save`).
- Status commands: `concentration_status.py`.
- SQL repository with savepoint and constraint mapping: `sql_concentration_repository.py`.
- Routes: the concentration routes in `http/router.py:84-313`.
- Seed-free migration shape: `0006_catalog_concentrations.py`.

### Domain language and rules fixed here

They come from the README decisions; the limits are decision 32.

**Perfume fields:**

- `name`: `clean_name` rules (2–80 characters, letters or digits, slug ≤ 100).
- `gender`: `women`, `men` or `unisex` (decision 25).
- `description`: trimmed, 0–2,000 characters.
- `top_notes`, `heart_notes`, `base_notes`: each a list of at most 10 notes. Each note is
  trimmed with inner whitespace collapsed, 1–40 characters, and the order is kept. An empty
  note is invalid.
- `brand_id`, `concentration_id`, `family_id`.
- `slug`: `slugify(f"{brand name} {perfume name} {concentration abbreviation}")`, stored,
  recomputed on every create and update (decision 37).
- `is_published`, `first_published_at` (null until the first publish, never cleared:
  decision 38), `is_archived`, `created_at`, `updated_at`.

**Identity:** brand + perfume name slug + concentration (decision 3). A duplicate is a 409
`CATALOG_PERFUME_ALREADY_EXISTS`. A clash on the stored slug, which is only possible with odd
names, maps to the same 409.

**References:**

- On create, the brand, family and concentration must exist and be active (422
  `CATALOG_PERFUME_<BRAND|FAMILY|CONCENTRATION>_UNAVAILABLE`).
- On update, only a **changed** reference must be active. An unchanged archived one is kept
  (decision 23).

**State:**

- New perfumes are hidden and not archived.
- `publish(now)` needs at least one active presentation (422
  `CATALOG_PERFUME_NOTHING_TO_SELL`, decision 24) and a non-archived perfume. It sets
  `first_published_at` only if it is null. Publishing twice is a 204.
- `hide()` is idempotent.
- `archive()` also hides the perfume and is idempotent. `restore()` leaves it hidden.
- An archived perfume is **read-only**: update, publish and every presentation change give
  422 `CATALOG_PERFUME_ARCHIVED`. Only restore and hide work on it.

**Presentation fields:**

- `ml`: an integer from 1 to 1000, unique within the perfume, archived presentations
  included.
- `price`: `Money`, 1 to 10,000,000 cents.
- `sale`: optional `price` + `starts_at` + `ends_at`. The sale price is 1 cent or more and
  lower than `price`. Each date is optional and timezone-aware; when both are present,
  `ends_at > starts_at` (decision 28). Past windows are allowed.
- `availability`: `in_stock` with no lead time, or `made_to_order` with
  `lead_time_min_days` and `lead_time_max_days`, both from 1 to 90, min ≤ max (decision 9).
- `is_active`, `created_at`.

**Presentation operations:**

- Adding a presentation whose ml exists → 409 `CATALOG_PRESENTATION_ALREADY_EXISTS`.
- Updating replaces ml, price, sale and availability; the same 409 applies when another
  presentation has the new ml.
- Archiving the **last active presentation of a published perfume** → 409
  `CATALOG_PERFUME_LAST_PRESENTATION` (decision 36).
- Archive and restore are idempotent.

## Out of scope

- The storefront (plan 004): public routes, effective price, "from" price, filters, search,
  sorting, hiding perfumes of archived brands from the public list (decision 22).
- Photos (005) and the Excel import (006).
- Events (`perfume.published` and so on), inventory and stock.
- Recomputing slugs when a brand or concentration is renamed (decision 37 says not to).
- Admin list filters beyond `archived`, admin search, bulk actions.
- Deleting anything (decision 10). Partial `PATCH` of a perfume.
- Fixing the reviewer's nit on plan 002 about `naming.py`'s docstring: step 2 does it,
  because the step touches that file anyway.
- The web app.

## Dependencies

- **001** (`done`) provides the brands and families repositories, and `CATALOG_MANAGE` in
  `http/router.py:70`.
- **002** (`done`) provides the concentrations repository and their `abbreviation`.

## Steps

1. **Contracts**
   - Files: `apps/api/src/fragancia_api/modules/catalog/contracts.py` (modify)
   - Do: add the following models. The bounds only cap the payload; the domain applies the
     real rules.
     - `PerfumeRequest`, for both create and update:

       | Field | Type |
       | ----- | ---- |
       | `brand_id`, `concentration_id`, `family_id` | `UUID` |
       | `name` | `str = Field(max_length=200)` |
       | `gender` | `Literal["women", "men", "unisex"]` |
       | `description` | `str = Field(default="", max_length=4000)` |
       | `top_notes`, `heart_notes`, `base_notes` | `list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=50)` |

     - `PresentationRequest`:

       | Field | Type |
       | ----- | ---- |
       | `ml`, `price_cents` | `int` |
       | `sale_price_cents` | `int \| None = None` |
       | `sale_starts_at`, `sale_ends_at` | `AwareDatetime \| None = None` |
       | `availability` | `Literal["in_stock", "made_to_order"]` |
       | `lead_time_min_days`, `lead_time_max_days` | `int \| None = None` |

     - `AdminPresentation` holds every field of `PresentationRequest`, plus `id`,
       `is_active` and `created_at`.
     - `AdminPerfume` (the detail):
       - `id`, `slug`, `name`, `gender`, `description` and the three note lists.
       - `brand`, `concentration` and `family`, each a small model:
         - `PerfumeBrandRef`: `id`, `name`, `is_active`.
         - `PerfumeConcentrationRef`: `id`, `name`, `abbreviation`, `is_active`.
         - `PerfumeFamilyRef`: `id`, `name`, `is_active`.
       - `is_published`, `first_published_at`, `is_archived`, `created_at`, `updated_at`.
       - `presentations: list[AdminPresentation]`, ordered by ml.
     - `AdminPerfumeSummary`: `id`, `slug`, `name`, `brand: PerfumeBrandRef`,
       `concentration: PerfumeConcentrationRef`, `gender`, `is_published`, `is_archived`,
       `active_presentations: int`, `created_at`.
     - `AdminPerfumePage(Page[AdminPerfumeSummary])`.
   - Observable result: `uv run just typecheck` passes.

2. **Domain: values, errors and the aggregate**
   - Files: `apps/api/src/fragancia_api/modules/catalog/domain/naming.py` (modify), `apps/api/src/fragancia_api/modules/catalog/domain/perfume.py` (create), `apps/api/src/fragancia_api/modules/catalog/domain/errors.py` (modify)
   - Do:
     - **`naming.py`:** extend the module docstring (lines 1-5) so it also describes the
       abbreviation rules. This is the nit from plan 002's review. Do not change the code.
     - **`errors.py`**, all with the category given and a one-sentence English message:

       | Category | Errors |
       | -------- | ------ |
       | `InvalidValueError` (422) | `PerfumeNameInvalid` (`CATALOG_PERFUME_NAME_INVALID`), `PerfumeDescriptionTooLong` (`CATALOG_PERFUME_DESCRIPTION_TOO_LONG`, details `{"max": 2000}`), `PerfumeNotesInvalid` (`CATALOG_PERFUME_NOTES_INVALID`, details `{"max_per_level": 10, "max_length": 40}`), `PresentationMlInvalid` (`CATALOG_PRESENTATION_ML_INVALID`), `PresentationPriceInvalid` (`CATALOG_PRESENTATION_PRICE_INVALID`), `PresentationSaleInvalid` (`CATALOG_PRESENTATION_SALE_INVALID`), `PresentationAvailabilityInvalid` (`CATALOG_PRESENTATION_AVAILABILITY_INVALID`) |
       | `BusinessRuleViolationError` (422) | `PerfumeBrandUnavailable`, `PerfumeFamilyUnavailable`, `PerfumeConcentrationUnavailable` (`CATALOG_PERFUME_<X>_UNAVAILABLE`), `PerfumeNothingToSell` (`CATALOG_PERFUME_NOTHING_TO_SELL`), `PerfumeArchived` (`CATALOG_PERFUME_ARCHIVED`) |
       | `ConflictError` (409) | `PerfumeAlreadyExists` (`CATALOG_PERFUME_ALREADY_EXISTS`), `PresentationAlreadyExists` (`CATALOG_PRESENTATION_ALREADY_EXISTS`), `PerfumeLastPresentation` (`CATALOG_PERFUME_LAST_PRESENTATION`) |
       | `NotFoundError` (404) | `PerfumeNotFound` (`CATALOG_PERFUME_NOT_FOUND`), `PresentationNotFound` (`CATALOG_PRESENTATION_NOT_FOUND`) |

     - **`perfume.py`:** the module docstring states every rule in "Domain language and
       rules fixed here". It contains:
       - `class Gender(StrEnum)` with `WOMEN`, `MEN` and `UNISEX`.
       - Frozen values, each with a `create(...) -> Result[…, <Error>]`:
         - `PerfumeName`, through `clean_name`, with a `slug`.
         - `Description`.
         - `Notes`, three tuples, built from three lists.
         - `Ml`.
         - `Price`, wrapping `Money`.
         - `Sale` (`price: Money`, `starts_at`, `ends_at`), validated against the regular
           price.
         - `Availability` (`kind: Literal["in_stock","made_to_order"]`, `min_days`,
           `max_days`).
       - `perfume_slug(brand_name: str, perfume_name: PerfumeName, abbreviation: str) ->
         str`.
       - `class Presentation`, a plain entity with `id`, `ml`, `price`, `sale`,
         `availability`, `is_active` and `created_at`.
       - `class Perfume(AggregateRoot)` with every field above and
         `presentations: list[Presentation]`. Its methods return `Result` when a rule can
         fail:

         | Method | Returns | Behavior |
         | ------ | ------- | -------- |
         | `Perfume.create(...)` | `Perfume` | hidden, not archived |
         | `update(...)` | `Result` | Err `PerfumeArchived` |
         | `publish(now)` | `Result` | archived, then nothing to sell; sets `first_published_at` if null |
         | `hide()` | — | idempotent |
         | `archive()` | — | also hides |
         | `restore()` | — | leaves it hidden |
         | `add_presentation(ml, price, sale, availability, *, created_at)` | `Result[UUID, …]` | archived; same ml (any presentation) |
         | `update_presentation(presentation_id, ml, price, sale, availability)` | `Result` | archived; not found; another presentation with that ml |
         | `archive_presentation(presentation_id)` | `Result` | archived; not found; `PerfumeLastPresentation` when published and it is the only active one; idempotent |
         | `restore_presentation(presentation_id)` | `Result` | archived; not found; idempotent |

         `update`, `add_presentation` and `update_presentation` set `updated_at` from a
         `now` argument.
   - Observable result: `uv run just arch` is green (the domain imports only the kernel and
     itself).

3. **Repositories (protocols)**
   - Files: `apps/api/src/fragancia_api/modules/catalog/domain/repositories.py` (modify)
   - Do:
     - Add `async def get(self, <x>_id: UUID) -> <X> | None` (no lock) to
       `BrandRepository`, `OlfactoryFamilyRepository` and `ConcentrationRepository`.
     - Add `PerfumeRepository` with these methods:

       | Method | Returns | Notes |
       | ------ | ------- | ----- |
       | `exists_with_identity(brand_id, name_slug, concentration_id, *, except_id=None)` | `bool` | |
       | `add(perfume)` | `Result[None, PerfumeAlreadyExists]` | Err also under a concurrent insert, on the identity or the slug constraint |
       | `get_for_update(perfume_id)` | `Perfume \| None` | locks the perfume row and loads every presentation |
       | `save(perfume)` | `Result[None, PerfumeAlreadyExists \| PresentationAlreadyExists]` | updates the perfume row and upserts every presentation by id |

   - Observable result: `uv run just typecheck` passes once steps 5–6 implement them.

4. **Application: commands and queries**
   - Files: `apps/api/src/fragancia_api/modules/catalog/application/commands/perfumes.py` (create), `apps/api/src/fragancia_api/modules/catalog/application/commands/presentations.py` (create), `apps/api/src/fragancia_api/modules/catalog/application/ports.py` (modify), `apps/api/src/fragancia_api/modules/catalog/application/queries/perfumes.py` (create)
   - Do. Every command runs inside `transactions.run` and returns `Result[…, DomainError]`,
     following the shape of `update_concentration.py:14`.
     - **`CreatePerfume(perfumes, brands, families, concentrations, transactions, clock)`**,
       `execute(request: PerfumeRequest) -> Result[UUID, DomainError]`:
       1. Build the values in the order name, description, notes, and return the first Err.
       2. Load the brand, family and concentration with `get`. A missing or archived one is
          Err `Perfume<X>Unavailable`, checked in the order brand, family, concentration.
       3. `exists_with_identity` → Err `PerfumeAlreadyExists`.
       4. Compute `perfume_slug(brand.name.value, name, concentration.abbreviation.value)`.
       5. `Perfume.create`, then `add`.
     - **`UpdatePerfume(…same…)`**, `execute(perfume_id, request) -> Result[None, …]`:
       1. Validate the values.
       2. `get_for_update`; Err `PerfumeNotFound` if missing.
       3. For each reference that **differs** from the stored one, load it and require it to
          be active. For an unchanged one, load it only to read the brand name and the
          abbreviation for the slug.
       4. `exists_with_identity(..., except_id=perfume_id)`.
       5. Recompute the slug, call `perfume.update(...)`, then `save`.
     - **`PublishPerfume`, `HidePerfume`, `ArchivePerfume` and `RestorePerfume(perfumes,
       transactions, clock)`**, `execute(perfume_id)`: `get_for_update`, then the domain
       method, then `save`.
     - **`presentations.py`:**
       - `AddPresentation(perfumes, transactions, clock).execute(perfume_id, request) ->
         Result[UUID, …]`: build `Ml`, `Price`, `Sale` (against the price) and
         `Availability`, in that order, returning the first Err. Then `get_for_update`, then
         `perfume.add_presentation`, then `save`.
       - `UpdatePresentation`, `ArchivePresentation` and `RestorePresentation`, with the
         same shape.
     - **`ports.py`:** add `PerfumeQueries` with two methods:
       - `list_admin(*, page, size, archived: bool) -> AdminPerfumePage`: non-archived
         perfumes by default, the archived ones when `archived` is true. Ordered by brand
         name, then perfume name (case-insensitive), then id.
       - `get_admin(perfume_id) -> AdminPerfume | None`.
     - **`queries/perfumes.py`:** `ListAdminPerfumes`, and `GetAdminPerfume`, which returns
       `Result[AdminPerfume, PerfumeNotFound]` so the router can `unwrap`.
   - Observable result: `uv run just typecheck` and `uv run just arch` pass.

5. **Infrastructure: tables and SQL adapters**
   - Files: `apps/api/src/fragancia_api/modules/catalog/infrastructure/tables.py` (modify), `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_brand_repository.py` (modify), `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_olfactory_family_repository.py` (modify), `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_concentration_repository.py` (modify), `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_perfume_repository.py` (create), `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_perfume_queries.py` (create)
   - Do:
     - **`tables.py`**, two new tables. Import `ForeignKey`, `Integer`, `Text` and
       `UniqueConstraint`, plus `ARRAY` from `sqlalchemy.dialects.postgresql`.
       - `perfumes`:

         | Column | Type | Constraints |
         | ------ | ---- | ----------- |
         | `id` | `Uuid` | primary key |
         | `brand_id` | `Uuid` | FK `catalog.brands.id`, not null, indexed |
         | `concentration_id` | `Uuid` | FK `catalog.concentrations.id`, not null |
         | `family_id` | `Uuid` | FK `catalog.olfactory_families.id`, not null, indexed |
         | `name` | `String(80)` | |
         | `name_slug` | `String(100)` | |
         | `slug` | `String(240)` | unique |
         | `gender` | `String(10)` | |
         | `description` | `Text` | |
         | `top_notes`, `heart_notes`, `base_notes` | `ARRAY(String(40))` | |
         | `is_published`, `is_archived` | `Boolean` | |
         | `first_published_at` | `DateTime(timezone=True)` | nullable |
         | `created_at`, `updated_at` | `DateTime(timezone=True)` | |

         Every column is not null except `first_published_at`. Add
         `UniqueConstraint("brand_id", "name_slug", "concentration_id",
         name="uq_perfumes_identity")`, and the constants `PERFUME_IDENTITY_UNIQUE` and
         `PERFUME_SLUG_UNIQUE = "uq_perfumes_slug"`.
       - `presentations`:

         | Column | Type | Constraints |
         | ------ | ---- | ----------- |
         | `id` | `Uuid` | primary key |
         | `perfume_id` | `Uuid` | FK `catalog.perfumes.id`, not null, indexed |
         | `ml` | `Integer` | |
         | `price_cents` | `Integer` | |
         | `sale_price_cents` | `Integer` | nullable |
         | `sale_starts_at`, `sale_ends_at` | `DateTime(timezone=True)` | nullable |
         | `availability` | `String(20)` | |
         | `lead_time_min_days`, `lead_time_max_days` | `Integer` | nullable |
         | `is_active` | `Boolean` | |
         | `created_at` | `DateTime(timezone=True)` | |

         Add `UniqueConstraint("perfume_id", "ml", name="uq_presentations_perfume_ml")` and
         the constant `PRESENTATION_ML_UNIQUE`.
     - **The three existing SQL repositories** gain `get`, the same query as
       `get_for_update` without `.with_for_update()`.
     - **`SqlPerfumeRepository`:**
       - Mappers `_to_perfume(row, presentation_rows)` and `_to_presentation(row)`. Values
         read from the database are trusted: build them directly, without `create`.
       - `add` inserts the perfume row and its presentations (none at creation) inside a
         savepoint. A violation of `PERFUME_IDENTITY_UNIQUE` or `PERFUME_SLUG_UNIQUE` →
         `Err(PerfumeAlreadyExists())`.
       - `get_for_update` runs `select … with_for_update()` on the perfume, then selects its
         presentations ordered by ml.
       - `save` runs inside one savepoint:
         1. Update the perfume row.
         2. For each presentation, run
            `postgresql.insert(presentations).values(...).on_conflict_do_update(index_elements=["id"],
            set_={every column except id, perfume_id and created_at})`.

         A violation of `PRESENTATION_ML_UNIQUE` → `Err(PresentationAlreadyExists())`, and a
         perfume constraint → `Err(PerfumeAlreadyExists())`. Copy `_violates` from
         `sql_concentration_repository.py`.
     - **`SqlPerfumeQueries`:**
       - `list_admin` joins brands and concentrations, counts active presentations with a
         correlated subquery, filters `is_archived == archived`, orders and paginates like
         `sql_brand_queries.py`, and returns the total.
       - `get_admin` loads the perfume with its three refs and its presentations, ordered by
         ml.
   - Observable result: `uv run just typecheck` passes.

6. **In-memory adapters**
   - Files: `apps/api/src/fragancia_api/modules/catalog/infrastructure/in_memory.py` (modify), `apps/api/src/fragancia_api/modules/catalog/infrastructure/in_memory_perfumes.py` (create)
   - Do:
     - **`in_memory.py`:** `InMemoryBrands`, `InMemoryOlfactoryFamilies` and
       `InMemoryConcentrations` gain `get`.
     - **`in_memory_perfumes.py`:** `InMemoryPerfumes(brands, families, concentrations)` is
       both the repository and the queries over one store:
       - It refuses a duplicate identity or slug on `add` and `save`, and a duplicate ml
         within a perfume on `save`.
       - It returns deep copies from `get_for_update`, so an unsaved mutation does not leak,
         as the SQL adapter behaves.
       - It builds the `AdminPerfume`/`AdminPerfumeSummary` refs from the three injected
         stores.
   - Observable result: `uv run just typecheck` passes.

7. **Migration**
   - Files: `apps/api/migrations/versions/` (create)
   - Do:
     1. Run `uv run just db-revision "catalog perfumes and presentations"` and review the
        file by hand. It must have revision `0007` and `down_revision` `0006`.
     2. Check that it creates `catalog.perfumes`, then `catalog.presentations`, with the
        foreign keys, `ix_*` indexes, `pk_*` keys, `uq_perfumes_slug`,
        `uq_perfumes_identity` and `uq_presentations_perfume_ml`, and the columns and types
        from step 5. Check that the `ARRAY` columns render as
        `postgresql.ARRAY(sa.String(length=40))`.
     3. The `downgrade` drops `presentations`, then `perfumes`. There is no seed.
     4. Apply it with `uv run just db-migrate` and `uv run just db-migrate --test`.
   - Observable result: `uv run just psql -c '\d catalog.presentations'` shows the FK and
     the unique constraint. Downgrading to 0006 and upgrading again works on the test
     database.

8. **HTTP and wiring**
   - Files: `apps/api/src/fragancia_api/modules/catalog/http/perfume_router.py` (create), `apps/api/src/fragancia_api/modules/catalog/module.py` (modify), `apps/api/src/fragancia_api/modules/catalog/__init__.py` (modify), `apps/api/openapi.json` (modify)
   - Do:
     - **`perfume_router.py`:**
       `admin_perfumes = admin_router(prefix="/perfumes", tags=["catalog · perfumes"],
       dependencies=[Depends(require_permission(CATALOG_MANAGE))])`. Import
       `CATALOG_MANAGE` from `http/router.py`, and define
       `perfume_routers = (admin_perfumes,)`. Each route's docstring lists its error codes,
       and the route name is its operation id.

       | Method | Path | Answer | Errors | Operation id |
       | ------ | ---- | ------ | ------ | ------------ |
       | `GET` | `""` | `AdminPerfumePage` | — | `list_admin_perfumes` |
       | `POST` | `""` | 201 `CreatedResponse` | 409, 422 | `create_perfume` |
       | `GET` | `"/{perfume_id}"` | `AdminPerfume` | 404 | `get_admin_perfume` |
       | `PUT` | `"/{perfume_id}"` | 204 | 404, 409, 422 | `update_perfume` |
       | `POST` | `"/{perfume_id}/publish"` | 204 | 404, 422 | `publish_perfume` |
       | `POST` | `"/{perfume_id}/hide"` | 204 | 404 | `hide_perfume` |
       | `POST` | `"/{perfume_id}/archive"` | 204 | 404 | `archive_perfume` |
       | `POST` | `"/{perfume_id}/restore"` | 204 | 404 | `restore_perfume` |
       | `POST` | `"/{perfume_id}/presentations"` | 201 `CreatedResponse` | 404, 409, 422 | `add_presentation` |
       | `PUT` | `"/{perfume_id}/presentations/{presentation_id}"` | 204 | 404, 409, 422 | `update_presentation` |
       | `POST` | `"/{perfume_id}/presentations/{presentation_id}/archive"` | 204 | 404, 409, 422 | `archive_presentation` |
       | `POST` | `"/{perfume_id}/presentations/{presentation_id}/restore"` | 204 | 404, 422 | `restore_presentation` |

       The list takes `page`/`size` as at `router.py` (the brand list) plus `archived: bool
       = False`.
     - **`module.py`:**
       - Register the 12 new use cases. Use one `SqlPerfumeRepository` and one
         `SqlPerfumeQueries`, reusing the brand, family and concentration repository
         instances already created there.
       - Change `AppModule(..., routers=routers)` to `routers=(*routers,
         *perfume_routers)`.
     - **`__init__.py`:** the docstring mentions perfumes and presentations.
     - Run `uv run just openapi`.
   - Observable result: `uv run just check` is green. `openapi.json` has the 12 new
     operations, all under `/api/v1/admin/perfumes`, and all protected.

9. **Existing integration fixtures and docs**
   - Files: `apps/api/tests/integration/catalog/test_sql_brands.py` (modify), `apps/api/tests/integration/catalog/test_sql_brand_maintenance.py` (modify), `apps/api/tests/integration/catalog/test_sql_olfactory_families.py` (modify), `apps/api/tests/integration/catalog/test_sql_concentrations.py` (modify), `docs/architecture.md` (modify)
   - Do:
     - Once `catalog.perfumes` references these tables, PostgreSQL refuses `TRUNCATE` on a
       referenced table. Change every `TRUNCATE catalog.<table>` in these four files
       (`test_sql_brands.py:23`, `test_sql_brand_maintenance.py:33`,
       `test_sql_olfactory_families.py:56,59`, `test_sql_concentrations.py:57,60`) to
       `TRUNCATE catalog.<table> CASCADE`. Nothing else in those tests changes.
     - In `docs/architecture.md:55`, the `catalog` row becomes "✔ Brands, olfactory
       families, concentrations, perfumes and presentations (back office). Next: public
       catalog, photos, Excel import".
   - Observable result: `uv run just test-integration` is green with the new tables in
     place.

10. **Tests (tester phase)**
    - Files: `apps/api/tests/unit/catalog/` (create), `apps/api/tests/integration/catalog/` (create)
    - Do: the layers in "Test layers required".
    - Observable result: `uv run just check` and `uv run just test-integration` are green.

## Acceptance criteria

All checked against `uv run just api` as an owner. "Seeded" means the families and
concentrations from migrations 0005 and 0006.

- [ ] `POST /api/v1/admin/perfumes` with an active brand "Versace", the seeded EDT and
      Aromática, name "Eros", gender `men` and notes → 201 `{id}`. `GET …/{id}` shows
      `slug: "versace-eros-edt"`, `is_published: false`, `first_published_at: null` and
      `presentations: []`.
- [ ] Creating it again (same brand, "EROS", EDT) → 409 `CATALOG_PERFUME_ALREADY_EXISTS`. With
      EDP instead → 201 and slug `versace-eros-edp`.
- [ ] An archived brand, family or concentration → 422 `CATALOG_PERFUME_*_UNAVAILABLE`, and
      an unknown id gives the same. 11 top notes, or a 41-character note → 422
      `CATALOG_PERFUME_NOTES_INVALID`. A 2,001-character description → 422
      `CATALOG_PERFUME_DESCRIPTION_TOO_LONG`.
- [ ] `POST …/{id}/publish` with no presentations → 422 `CATALOG_PERFUME_NOTHING_TO_SELL`.
- [ ] `POST …/{id}/presentations` adds the following:
      - `{ml:100, price_cents:250000, availability:"in_stock"}` → 201.
      - `{ml:200, price_cents:390000, sale_price_cents:350000, sale_ends_at:<future>,
        availability:"made_to_order", lead_time_min_days:7, lead_time_max_days:10}` → 201.
      - 100 ml again → 409 `CATALOG_PRESENTATION_ALREADY_EXISTS`.
- [ ] Presentation validation:
      - A sale price ≥ the price, or `sale_ends_at` ≤ `sale_starts_at` → 422
        `CATALOG_PRESENTATION_SALE_INVALID`.
      - `made_to_order` without lead days, or with min > max → 422
        `..._AVAILABILITY_INVALID`.
      - `in_stock` with lead days → 422 `..._AVAILABILITY_INVALID`.
      - ml 0 → 422 `..._ML_INVALID`.
      - Price 0 or 10,000,001 → 422 `..._PRICE_INVALID`.
- [ ] `publish` → 204 and sets `first_published_at`. `hide`, then `publish` again, keeps
      the same `first_published_at`.
- [ ] On the published perfume:
      - Archiving the 200 ml presentation → 204.
      - Archiving the 100 ml one, now the last active, → 409
        `CATALOG_PERFUME_LAST_PRESENTATION`.
      - After `hide`, archiving it → 204.
- [ ] `PUT …/{id}` renaming the perfume to "Eros Flame" → 204 with slug
      `versace-eros-flame-edt`. Renaming the brand "Versace" afterwards does **not** change
      the perfume's slug.
- [ ] Changing the family of a perfume whose current family is archived, to an active one →
      204. Keeping the archived family unchanged on update → 204. Changing to an archived
      family → 422.
- [ ] `archive` hides the perfume and moves it to `GET /admin/perfumes?archived=true`. While
      archived, `PUT`, `publish` and `POST …/presentations` → 422 `CATALOG_PERFUME_ARCHIVED`.
      `restore` → 204, and it stays hidden.
- [ ] `GET /admin/perfumes` lists non-archived perfumes ordered by brand, then name, with
      `active_presentations`. Every new route returns 401 without a session.
- [ ] Two concurrent `POST …/presentations` with the same ml on one perfume: one 201 and one
      409, never a 500.
- [ ] Migration 0007 applies on the development database. `openapi.json` is regenerated and
      the drift check passes. The existing integration suites still pass.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | Every value's bounds (name, description, notes, ml, price, sale vs. price and dates, availability combinations); `perfume_slug`; publish (nothing to sell, archived, `first_published_at` set once); hide/archive/restore; add/update/archive/restore presentation, including duplicate ml, last active presentation of a published perfume, idempotency, and read-only when archived |
| application | yes     | Create and update: validation order, reference checks (missing, archived, unchanged archived kept, changed to archived refused), identity conflict, slug recomputed from the current brand name and abbreviation; each status command and presentation command passes on domain and `save` errors; not found |
| http        | yes     | All 12 routes: status codes and error `code`s, 401, 403 without `catalog:manage`, payload bounds (`Literal` gender and availability, `AwareDatetime`, list sizes), the `archived` filter, detail shape |
| integration | yes     | Both new tables and their constraints (identity, slug, perfume+ml, FKs); `save` upserts presentations and maps the ml clash to Err with the transaction usable; concurrent same-ml adds; `get_for_update` locks and loads presentations in ml order; admin list order, count of active presentations, pagination and the `archived` filter; `get` on the three reference repositories |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

Implementation, 2026-10-09. Steps 1–9 done; step 10 (tests) left to the tester phase, as the
dispatch asked. Every deviation below is cosmetic or fills a gap the plan left open. None changes
scope.

1. **Sale dates without a sale price → 422 `CATALOG_PRESENTATION_SALE_INVALID`.** The plan does
   not say what happens when `sale_starts_at`/`sale_ends_at` are sent without
   `sale_price_cents`. Dropping the dates silently looked worse than refusing them.
   `Sale.create` returns `Ok(None)` only when there is no price and no dates
   (`domain/perfume.py`, `Sale.create`). Reviewer: confirm or flag it.
2. **Error details.** The plan fixed `details` only for the description and notes errors. The
   ml, price and availability errors also carry their bounds, like the name errors already do:
   `{"min": 1, "max": 1000}`, `{"min_cents": 1, "max_cents": 10000000}` and
   `{"min_days": 1, "max_days": 90}`. The name error carries `{"min": 2, "max": 80}`, like the
   brand one. The sale error has no details.
3. **The domain also checks that the sale dates are timezone-aware**, a rule from "Domain
   language". The HTTP contract (`AwareDatetime`) already guarantees it.
4. **The update check order is followed literally**: validate, lock, references, identity, then
   `perfume.update`. An archived perfume with a changed reference that is archived or missing
   therefore answers `CATALOG_PERFUME_<X>_UNAVAILABLE`, not `CATALOG_PERFUME_ARCHIVED`.
5. **`updated_at` is touched only by `update`, `add_presentation` and `update_presentation`**,
   as the plan lists. `add_presentation` uses its `created_at` as the `now`. `publish`, `hide`,
   `archive`, `restore` and the presentation archive and restore do not change it.
6. **`InMemoryPerfumes(brands, families, concentrations, *perfumes)`** also accepts optional
   seed perfumes, like the other in-memory adapters accept seed aggregates.
7. **Migration 0007:** formatted like 0006. The two explicit unique names are wrapped in
   `op.f(...)` for consistency. The autogenerated `drop_index` calls are kept in `downgrade`.
   They come before each `drop_table`, in the order presentations, then perfumes.
   `alembic check` reports no drift. Downgrading to 0006 and upgrading again worked on the test
   database.
8. **`module.py`** registers the 12 use cases with two loops (one for the two commands that
   need the reference repositories, one for the eight that take
   `perfumes, transactions, clock`), instead of 12 separate `services.add` blocks.
9. **`perfume_router.py`** shares three typed `responses` dicts (`_404`, `_404_422`,
   `_404_409_422`). mypy rejects inline dict literals passed as variables without the
   `dict[int | str, dict[str, Any]]` annotation.

**Not verified by the implementer:** the 12 routes were not called with an owner session, and
nothing exercises the SQL perfume repository and queries yet, because step 10's integration
tests do not exist. The running API answers 401 on `GET /api/v1/admin/perfumes` without a
session, so the routes are mounted and protected.

**Commands run:**

| Command | Result |
| ------- | ------ |
| `uv run just typecheck`, `uv run just arch` | green |
| `uv run just db-migrate`, `uv run just db-migrate --test` | 0006 → 0007 applied |
| `alembic check` | no new operations |
| `alembic -x test=true downgrade 0006`, then `upgrade head` | ok |
| `uv run just openapi` | the 12 operation ids are present |
| `uv run just check` | green: 803 unit tests passed, 3 skipped; 684 harness tests passed |
| `uv run just test-integration` | 124 passed, 2 skipped |
| `uv run just plans-scope` | 29 declared, 29 changed, every change inside the plan |

**Finding filed:** `plans/findings/platform-format-hook-fix-rewrites-code-mid-edit.md`. The
format hook's `ruff check --fix` stripped the quotes of `Literal["women", …]` between two
edits. Fixed in place, and the code is unaffected.

## Test coverage

Tester phase, 2026-10-09. No product code was touched. Files (all under `apps/api/tests/`):

- `unit/catalog/perfume_support.py`: shared builders (not a test module).
- `unit/catalog/test_perfume_domain.py` (83 tests, domain).
- `unit/catalog/test_perfume_commands.py` (47) and `test_presentation_commands.py` (31), application.
- `unit/catalog/test_perfume_http.py` (96, http).
- `integration/catalog/test_sql_perfumes.py` (44, integration, against `fragancia_test`).

The integration module owns its brands, families and concentrations (it does not depend on the
seeds) and truncates `catalog.perfumes CASCADE` before and after each test.

Every row below is CONFIRMED by a test that passes. There are no GAP and no NOT CONFIRMED
items: nothing the plan promises for the tested layers was found missing.

| Behavior | Source | Layer | Tests | State |
| -------- | ------ | ----- | ----- | ----- |
| Name, description, notes bounds (inclusive limits, trimming, collapse, order and duplicates kept, empty note invalid) | `domain/perfume.py` `PerfumeName`, `Description`, `Notes` | domain | `test_perfume_domain.py` (name, description, notes tests) | CONFIRMED |
| Ml 1-1000, price 1-10,000,000 cents, with error details | `Ml.create`, `Price.create` | domain | `test_ml_*`, `test_price_*` | CONFIRMED |
| Sale: price below the regular one and at least 1, window order, past window, open-ended, naive datetimes refused, dates without price refused (deviation 1) | `Sale.create` | domain | `test_sale_*` | CONFIRMED |
| Availability combinations (in_stock without lead time, made_to_order 1-90 with min <= max) | `Availability.create` | domain | `test_in_stock_*`, `test_made_to_order_*` | CONFIRMED |
| `perfume_slug` (brand + name + abbreviation, accents and symbols) | `perfume_slug` | domain | `test_perfume_slug_*` | CONFIRMED |
| New perfume hidden, unarchived, no presentations; update replaces all fields; archived refuses update | `Perfume.create`, `update` | domain | `test_a_new_perfume_*`, `test_update_*`, `test_an_archived_perfume_cannot_be_updated` | CONFIRMED |
| Publish (nothing to sell, ignores archived presentations, archived refused, `first_published_at` set once, idempotent); hide, archive, restore idempotent and as specified | `publish`, `hide`, `archive`, `restore` | domain | `test_publish_*`, `test_republishing_*`, `test_hide_*`, `test_archive_*`, `test_restore_*` | CONFIRMED |
| Presentations: add (duplicate ml incl. archived), update (own ml allowed, another's refused, archived one's refused), not found, idempotent archive/restore, last active of a published perfume, read-only when archived | `add_presentation` .. `restore_presentation` | domain | `test_adding_*`, `test_update_presentation_*`, `test_unknown_presentations_*`, `test_the_last_active_*`, `test_an_archived_perfume_refuses_presentation_changes` | CONFIRMED |
| Create: values validated in order name, description, notes, before references; references missing or archived refused in order brand, family, concentration, before the identity check; identity conflict (case, spacing, archived perfume); other brand or concentration is a new perfume; conflict found on `add` is returned | `commands/perfumes.py` `CreatePerfume` | application | `test_perfume_commands.py` create section | CONFIRMED |
| Update: not found; validation before lookup; own identity allowed, another's refused; unchanged archived references kept; changed reference must be active (missing, archived); slug recomputed from the current brand name and abbreviation; archived perfume refused; deviation 4 (changed archived reference answers UNAVAILABLE before ARCHIVED); `save` error returned | `UpdatePerfume` | application | `test_perfume_commands.py` update section | CONFIRMED |
| Publish, hide, archive, restore: effect, not found, domain errors and `save` error passed through | `PublishPerfume` .. `RestorePerfume` | application | `test_perfume_commands.py` status section | CONFIRMED |
| Add/update/archive/restore presentation: effect, validation order ml, price, sale, availability (before the lookup), not found (perfume and presentation), archived perfume, duplicate ml, last active, `save` error | `commands/presentations.py` | application | `test_presentation_commands.py` | CONFIRMED |
| In-memory adapter returns copies (unsaved mutations do not leak), as the SQL adapter behaves | `in_memory_perfumes.py` | application | `test_an_unsaved_mutation_does_not_leak_into_the_store` | CONFIRMED |
| All 12 routes: 401 without a session, 403 without `catalog:manage`, admin routes declared protected | `http/perfume_router.py` | http | `test_routes_return_401_*`, `test_routes_return_403_*`, `assert_admin_routes_are_protected` in the fixture | CONFIRMED |
| Create/detail shape, slug, hidden state, 409 and every 422 code with details; payload bounds (`Literal` gender and availability, `AwareDatetime`, list sizes, lengths, UUIDs, required fields) | `contracts.py`, router | http | `test_create_*`, `test_presentation_payload_is_bounded`, `test_the_detail_*` | CONFIRMED |
| Update, presentation add/update/archive/restore, publish/hide/archive/restore status codes and codes (404, 409, 422), archived perfume read-only on six routes, first publication kept, last presentation 409 | router | http | `test_update_*`, `test_add_presentation_*`, `test_publish_*`, `test_archive_hides_*`, `test_the_last_active_*` | CONFIRMED |
| List: order brand then name, active presentation count, `archived` filter, pagination, bad parameters | router, `InMemoryPerfumes.list_admin` | http | `test_the_list_*` | CONFIRMED |
| Constraints: `uq_perfumes_identity`, `uq_perfumes_slug`, `uq_presentations_perfume_ml` (per perfume only), the three perfume FKs and the presentation FK, RESTRICT on a referenced brand, TRUNCATE CASCADE | `tables.py`, migration 0007 | integration | `test_identity_is_unique_*`, `test_the_stored_slug_is_unique`, `test_ml_is_unique_*`, `test_a_perfume_needs_*`, `test_a_presentation_needs_*`, `test_a_referenced_brand_*` | CONFIRMED |
| `add`: row, notes arrays, slug, defaults; presentations of the aggregate; identity and slug violations mapped to Err with the transaction usable; `exists_with_identity` with `except_id`; concurrent creation yields one conflict | `sql_perfume_repository.py` | integration | `test_create_persists_*`, `test_add_*`, `test_exists_with_identity_*`, `test_concurrent_creation_*` | CONFIRMED |
| `get_for_update`: None when unknown, mapping back (sale, availability, notes, state), presentations in ml order, row lock blocks a second transaction until the first ends | `get_for_update` | integration | `test_get_for_update_*` | CONFIRMED |
| `save`: updates the row and keeps `created_at`; upserts presentations by id (no duplicates, `created_at` kept, sale and lead time removable); ml clash mapped to Err, savepoint rolls the perfume update back, transaction usable; perfume identity and slug clash mapped to Err; flags and `first_published_at` persisted | `save` | integration | `test_save_*`, `test_an_update_can_remove_*`, `test_moving_a_presentation_*`, `test_status_commands_persist_*`, `test_update_keeps_an_unchanged_archived_family_*` | CONFIRMED |
| Concurrency: same-ml adds give one 201-equivalent and four conflicts, never an error; different sizes are all kept; two updates to one ml are serialized | `get_for_update` + upsert | integration | `test_concurrent_adds_*`, `test_concurrent_updates_*` | CONFIRMED |
| Admin queries: detail with refs (including inactive ones) and presentations by ml; unknown is 404; list order (case-insensitive brand, name, id), correlated count of active presentations, `archived` filter with totals, pagination, empty page | `sql_perfume_queries.py` | integration | `test_get_admin_*`, `test_the_list_*`, `test_an_empty_list_*` | CONFIRMED |
| `get` on the brand, family and concentration repositories (found, archived found, unknown None) | `sql_*_repository.py` | integration | `test_brand_get_*`, `test_family_get_*`, `test_concentration_get_*` | CONFIRMED |
| e2e | n/a | e2e | not applicable (no e2e infrastructure) | n/a |

Not covered on purpose: the acceptance criterion "two concurrent `POST` with the same ml: one
201 and one 409" is proved at the use case level (`test_concurrent_adds_of_the_same_ml_*`), not
through the HTTP stack; the verifier should still drive it against the running app.

**Runs.** Baseline (before any test): `uv run just check` green (684 harness tests; unit suite
green) and `uv run just test-integration` 124 passed, 2 skipped. Closing (after the tests):
`uv run just check` green and `uv run just test-integration` 168 passed, 2 skipped (+44). Between
the two runs only the touched files were run, plus `uv run mypy` once after fixing four typing
errors in the new tests.

## Review findings

## Verification
