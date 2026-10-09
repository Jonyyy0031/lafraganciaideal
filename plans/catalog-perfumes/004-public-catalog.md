---
status: approved
module: catalog
min_implementer: mid
depends_on: ["003"]
---

# 004 — The public catalog

## Context

**What exists** (plan 003, `done`). Perfumes and presentations live in `catalog.perfumes` and
`catalog.presentations`, defined in
`apps/api/src/fragancia_api/modules/catalog/infrastructure/tables.py:62-108`.

- Each perfume stores:
  - its `slug` (unique), `name`, `gender`, `description` and three `ARRAY(String(40))` note
    columns;
  - `is_published`, `is_archived` and `first_published_at`;
  - foreign keys to brands, concentrations and olfactory families.
- Each presentation stores `ml`, `price_cents`, an optional `sale_price_cents` with optional
  `sale_starts_at`/`sale_ends_at`, `availability` and the lead days, and `is_active`.

In the domain (`domain/perfume.py`), `Sale` (`perfume.py:176-203`) holds the price and the
optional dates. `Perfume.update` (`perfume.py:334-359`) overwrites `slug`, which the use case
recomputes (README decision 37). `SqlPerfumeRepository.save`
(`infrastructure/sql_perfume_repository.py:191-220`) updates the perfume row and upserts its
presentations inside one savepoint.

The read side is `PerfumeQueries` (`application/ports.py:52-62`), with `list_admin` and
`get_admin`, implemented by `SqlPerfumeQueries` (`infrastructure/sql_perfume_queries.py:53`).
Routes are in `http/perfume_router.py`. Only the admin router exists, and
`perfume_routers = (admin_perfumes,)` at `perfume_router.py:215`. `Page` caps `size` at
`MAX_PAGE_SIZE = 100` (`apps/api/src/fragancia_api/shared/contracts/__init__.py:5-19`). The
last migration is `0007_catalog_perfumes_and_presentations.py`.

Locally, the database role is a superuser and the `unaccent` extension is available but not
installed. I checked both on 2026-10-09 with `pg_roles` and `pg_available_extensions`.

**What we need.** README decisions 12, 22, 29–31 and 40–43. Customers browse published
perfumes:

- **Filters:** brand, family, gender, text, and a price range on the "from" price.
- **Sorting:** name, price ascending or descending, and newest.
- **Detail:** by slug, with the active presentations and their current price. Old slugs
  still find the perfume.

**Approach.**

- **Effective price.** It is defined once in the domain: `Sale.is_active(now)` and
  `Presentation.effective_price(now)`. The SQL list mirrors it with a `CASE` expression,
  because filtering and sorting by price must happen in the database. The integration tests
  pin the two to the same results.
- **Visibility.** A perfume is visible when it is published, not archived, and its brand is
  active (decision 22). The family and concentration do not matter (decisions 22 and 33).
- **Slug history.** It lives in a new table, `catalog.perfume_slug_history`.
  `Perfume.update` records the slug it replaces in `retired_slugs`, and `save` inserts those
  rows. The public detail matches the current slug first, then the history, and always
  answers with the **current** slug. The web compares the two and issues the 301.
- **Search.** `unaccent(lower(…)) LIKE unaccent(lower('%q%'))` over the perfume name, the
  brand name and the notes, joined into one string. The `%` and `_` characters in `q` are
  escaped.
- **Discarded:**
  - Answering an old slug with an HTTP 301 from the API: fetch clients follow the redirect
    silently, so the web would never learn the URL changed.
  - Full-text search (`tsvector`): it matches word stems, not substrings like "lanc".

**Imitated files:**
- `infrastructure/sql_perfume_queries.py` (`list_admin`, for the joins, page and total).
- `application/queries/perfumes.py`, the query use cases.
- `http/perfume_router.py`, the route shape.
- `0007_…` for the migration.

## Out of scope

- Photos (plan 005); the import (006).
- Indexes for search or price. The catalog has hundreds of perfumes, not millions; add them
  when a measurement says so.
- Typo-tolerant search (`pg_trgm`), facet counts, and price-range bounds for a slider.
- Filtering by concentration. It was not asked for; decision 12 lists brand, gender, family,
  text and price.
- The 301 itself, which belongs to the web (phase 3). The API only reports the current slug.
- Stock or inventory (a later initiative). Caching.
- Changing the admin routes or the admin list.

## Dependencies

- **003** (`done`) provides the `perfumes`/`presentations` tables, `Perfume`/`Presentation`/
  `Sale`, `SqlPerfumeRepository.save`, `PerfumeQueries` and `perfume_router.py`.

## Steps

1. **Contracts**
   - Files: `apps/api/src/fragancia_api/modules/catalog/contracts.py` (modify)
   - Do: add these models.
     - Refs: `PublicPerfumeBrand` (`name`, `slug`), `PublicPerfumeConcentration` (`name`,
       `abbreviation`) and `PublicPerfumeFamily` (`name`, `slug`).
     - `PublicPerfumeCard`:
       - `slug`, `name`, `gender`;
       - `brand`, `concentration`, `family` (the refs above);
       - `price_from_cents`: the lowest effective price among the active presentations;
       - `on_sale`: true when any active presentation has an active sale now.
     - `PublicPerfumePage(Page[PublicPerfumeCard])`.
     - `PublicPresentation`:
       - `id`, `ml`, `availability`, `lead_time_min_days`, `lead_time_max_days`;
       - `price_cents`: the effective price;
       - `regular_price_cents: int | None`, set only while a sale is active;
       - `sale_ends_at: datetime | None`, set only while a sale is active.
     - `PublicPerfume`:
       - `slug` (always the current one), `name`, `gender`, `description`;
       - `brand`, `concentration`, `family`;
       - `top_notes`, `heart_notes`, `base_notes`;
       - `presentations: list[PublicPresentation]`, active ones only, ordered by ml.

     Each model gets a one-line docstring. `PublicPerfume`'s docstring states that the web
     redirects when `slug` differs from the requested one.
   - Observable result: `uv run just typecheck` passes.

2. **Domain**
   - Files: `apps/api/src/fragancia_api/modules/catalog/domain/perfume.py` (modify)
   - Do:
     - **`Sale.is_active(now: datetime) -> bool`.** True when `(starts_at is None or
       starts_at <= now) and (ends_at is None or now < ends_at)`.
     - **`Presentation.effective_price(now) -> Money`.** The sale price while the sale is
       active, the regular price otherwise.
     - **`Perfume.retired_slugs: list[str]`.** It starts empty, both in the constructor and
       in `create`. `update` appends the old slug when the new one differs, before
       overwriting it.
     - Update the module docstring with the effective-price rule. The start is inclusive and
       the end is exclusive.
   - Observable result: `uv run just arch` is green.

3. **Application**
   - Files: `apps/api/src/fragancia_api/modules/catalog/application/ports.py` (modify), `apps/api/src/fragancia_api/modules/catalog/application/queries/perfumes.py` (modify)
   - Do:
     - **In `ports.py`**, add a frozen dataclass `PublicPerfumeFilters` with these fields:
       - `q: str | None`;
       - `brands: tuple[str, ...]` and `families: tuple[str, ...]`, both slugs;
       - `genders: tuple[str, ...]`;
       - `min_price_cents: int | None` and `max_price_cents: int | None`;
       - `sort: Literal["name", "price_asc", "price_desc", "newest"]`;
       - `page: int` and `size: int`.
     - **Extend `PerfumeQueries`:**
       - `list_public(filters, *, now) -> PublicPerfumePage`.
       - `get_public(slug, *, now) -> PublicPerfume | None`. It returns only a visible
         perfume, and matches its current slug first, then the history.

       The docstrings state the visibility rule and the ordering:
       - `name`: brand name, then perfume name (both case-insensitive), then id.
       - `price_asc`/`price_desc`: `price_from` then name.
       - `newest`: `first_published_at` descending, then id.
     - **In `queries/perfumes.py`**, add two use cases:
       - `ListPublicPerfumes(queries, clock).execute(filters)`.
       - `GetPublicPerfume(queries, clock).execute(slug) -> Result[PublicPerfume,
         PerfumeNotFound]`.

       Both pass `clock.now()`. Before the query, `ListPublicPerfumes` trims `q` and turns a
       blank one into `None`.
   - Observable result: `uv run just typecheck` and `uv run just arch` pass.

4. **Infrastructure**
   - Files: `apps/api/src/fragancia_api/modules/catalog/infrastructure/tables.py` (modify), `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_perfume_repository.py` (modify), `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_perfume_queries.py` (modify), `apps/api/src/fragancia_api/modules/catalog/infrastructure/in_memory_perfumes.py` (modify)
   - Do:
     - **`tables.py`.** Add `perfume_slug_history` with these columns:
       - `slug` `String(240)`, primary key;
       - `perfume_id` `Uuid`, FK `catalog.perfumes.id`, not null, indexed;
       - `retired_at` `DateTime(timezone=True)`, not null.
     - **`SqlPerfumeRepository.save`.** Inside the same savepoint, after the perfume update,
       insert each slug in `perfume.retired_slugs` with
       `postgresql.insert(perfume_slug_history).values(slug=…, perfume_id=perfume.id,
       retired_at=perfume.updated_at).on_conflict_do_update(index_elements=["slug"],
       set_={"perfume_id": …, "retired_at": …})`. The latest perfume to drop a slug owns it.
       Then clear `perfume.retired_slugs`.
     - **`SqlPerfumeQueries`.** Add three private pieces:
       - `_effective_price(now)`: a `case(...)` that mirrors `Presentation.effective_price`.
       - `_sale_active(now)`: the boolean.
       - `_visible`: `perfumes.c.is_published & ~perfumes.c.is_archived & brands.c.is_active`.

       Then implement:
       - **`list_public`.** A subquery over the active presentations, grouped by
         `perfume_id`, gives `price_from = min(effective)` and `on_sale = bool_or(sale
         active)`. Join it with perfumes, brands, concentrations and families under
         `_visible`. Apply each filter only when it is set:
         - `brands.c.slug.in_(…)`, `olfactory_families.c.slug.in_(…)`,
           `perfumes.c.gender.in_(…)`;
         - `price_from >= min` and `price_from <= max`;
         - `q`: `func.unaccent(func.lower(haystack)).like(func.unaccent(func.lower(pattern)),
           escape="\\")`, where `haystack = perfumes.c.name || ' ' || brands.c.name || ' ' ||
           array_to_string(top_notes || heart_notes || base_notes, ' ')`, and `pattern` is
           `%<q with \, % and _ escaped>%`.

         Sort as the port says, then count the total and paginate as `list_admin` does.
       - **`get_public`.** Find the perfume id by `perfumes.c.slug == slug` under `_visible`.
         If there is none, look the slug up in `perfume_slug_history`, joined to perfumes,
         under `_visible`. Then load its active presentations ordered by ml, and map each one
         through `Presentation.effective_price` and `Sale.is_active` (build the domain values
         from the row, as `_to_presentation` does in `sql_perfume_repository.py:91`).
     - **`InMemoryPerfumes`.** Implement `list_public`/`get_public` with the same rules in
       Python, using the domain methods, and keep a slug history map updated on `save`.
   - Observable result: `uv run just typecheck` passes.

5. **Migration**
   - Files: `apps/api/migrations/versions/` (create)
   - Do:
     1. Run `uv run just db-revision "catalog perfume slug history and unaccent"`. Revision
        `0008`, `down_revision` `0007`.
     2. Review the file by hand. `upgrade` must first run `op.execute("CREATE EXTENSION IF
        NOT EXISTS unaccent")`, then create `catalog.perfume_slug_history` with
        `pk_perfume_slug_history`, the FK and `ix_…_perfume_id`.
     3. `downgrade` drops only the table. It leaves the extension, because dropping it is
        not reversible-safe if anything else starts using it, and leaving it is harmless.
        Say so in a comment.
     4. Apply with `uv run just db-migrate` and `uv run just db-migrate --test`.
   - Observable result: `uv run just psql -c "select unaccent('Lancôme')"` returns
     `Lancome`. A downgrade to 0007 and an upgrade work on the test database.

6. **HTTP and wiring**
   - Files: `apps/api/src/fragancia_api/modules/catalog/http/perfume_router.py` (modify), `apps/api/src/fragancia_api/modules/catalog/module.py` (modify), `apps/api/openapi.json` (modify)
   - Do:
     - **In `perfume_router.py`**, add `public_perfumes = public_router(prefix="/perfumes",
       tags=["catalog"])` with two routes:

       | Route | Operation id | Answers |
       | ----- | ------------ | ------- |
       | `GET ""` | `list_public_perfumes` | `PublicPerfumePage` |
       | `GET "/{slug}"` | `get_public_perfume` | `PublicPerfume`; 404 `CATALOG_PERFUME_NOT_FOUND` |

       `GET ""` takes these query parameters:
       - `q: str | None = Query(None, max_length=100)`;
       - `brand: list[str] = Query([])`, `family: list[str] = Query([])` and `gender:
         list[Literal["women","men","unisex"]] = Query([])`, all repeatable;
       - `min_price_cents` and `max_price_cents: int | None = Query(None, ge=0)`;
       - `sort: Literal["name","price_asc","price_desc","newest"] = "name"`;
       - `page: int = Query(1, ge=1)` and `size: int = Query(24, ge=1, le=48)`.

       It builds `PublicPerfumeFilters` from them. `GET "/{slug}"` takes `slug` with
       `max_length=240`, and its docstring says that the web redirects when the returned
       `slug` differs. Change the tuple to `perfume_routers = (public_perfumes,
       admin_perfumes)`.
     - **In `module.py`**, register `ListPublicPerfumes` and `GetPublicPerfume` with the
       existing `SqlPerfumeQueries` and `platform.clock`.
     - Run `uv run just openapi`.
   - Observable result: `uv run just check` is green and `openapi.json` gains the 2 public
     operations.

7. **Docs**
   - Files: `docs/architecture.md` (modify)
   - Do: the `catalog` row (`docs/architecture.md:55`) becomes "✔ Brands, olfactory
     families, concentrations, perfumes and presentations (back office), public catalog
     (filters, search, sort, detail by slug). Next: photos, Excel import".
   - Observable result: the row matches what was built.

8. **Tests (tester phase)**
   - Files: `apps/api/tests/unit/catalog/` (create), `apps/api/tests/integration/catalog/` (create)
   - Do: the layers in "Test layers required".
   - Observable result: `uv run just check` and `uv run just test-integration` are green.

## Acceptance criteria

All checked against `uv run just api`, with data created through the admin routes of plan 003.

- [ ] `GET /api/v1/perfumes` lists only perfumes that are published, not archived, and whose
      brand is active. A hidden, archived, or archived-brand perfume does not appear. A
      perfume whose family or concentration is archived still appears.
- [ ] A perfume with presentations at 250000 and 390000 (sale 350000, active) shows
      `price_from_cents: 250000` and `on_sale: true`.
- [ ] A sale whose `sale_starts_at` is in the future does not count: the price is the
      regular one and `on_sale: false`. A sale whose `sale_ends_at` has passed does not
      count either.
- [ ] Filters:
  - [ ] `?brand=<slug>&brand=<slug2>` returns perfumes of either brand.
  - [ ] `?family=<slug>` and `?gender=men` filter by family and gender.
  - [ ] `?min_price_cents=300000` excludes a perfume whose from price is 250000.
- [ ] Search:
  - [ ] `?q=lanc` finds a perfume of brand "Lancôme".
  - [ ] `?q=VAIN` finds one with the note "Vainilla".
  - [ ] `?q=100%` matches nothing literally, and does not match everything.
- [ ] Sorting:
  - [ ] `?sort=price_asc` and `price_desc` order by the from price.
  - [ ] `?sort=newest` orders by first publication, newest first.
  - [ ] The default order is brand, then name.
- [ ] Pagination: the default page size is 24. `?size=49` → 422 `VALIDATION_ERROR`.
- [ ] Detail: `GET /api/v1/perfumes/<slug>` returns the perfume with only active
      presentations, ordered by ml. A presentation on sale has `price_cents` = the sale
      price, `regular_price_cents` and `sale_ends_at`. One without a sale has both null.
- [ ] Old slugs: after renaming the perfume in the admin, `GET /perfumes/<old slug>` → 200
      with the **new** `slug`. A hidden perfume, or an unknown slug → 404
      `CATALOG_PERFUME_NOT_FOUND`.
- [ ] Migration 0008 applies on the development database, and `unaccent` is installed.
      `openapi.json` is regenerated and the drift check passes.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `Sale.is_active` boundaries (start inclusive, end exclusive, open ends); `effective_price`; `retired_slugs` appended only when the slug changes |
| application | yes     | `q` trimmed/blank → None; `clock.now()` passed; get → `PerfumeNotFound` |
| http        | yes     | Both routes: query parsing (repeatable `brand`/`family`/`gender`, `Literal` values, `size` ≤ 48, `ge=0` prices, `q` max length), 404 code, response shapes; public routes need no session |
| integration | yes     | Visibility (published, archived, brand archived, family/concentration archived); effective price and `on_sale` in SQL agree with the domain at window boundaries; each filter and combination; accent/case-insensitive search across name, brand and notes; `%`/`_` escaping; every sort and tie-breaks; pagination totals; slug history written on rename, lookup via old slug returns current slug, re-used slug ownership; `unaccent` installed by the migration |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

## Test coverage

## Review findings

## Verification
