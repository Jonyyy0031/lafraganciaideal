---
status: testing
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

- Step 4, `list_public`: the search haystack uses `concat_ws(' ', name, brand, array_to_string(top), array_to_string(heart), array_to_string(base))` instead of `||` over the concatenated arrays. Same result, and no NULL or array-concat operator handling. Cosmetic.
- Step 4/6: `list_public` inner-joins the active-presentation subquery, so a visible perfume with no active presentation is not listed (it has no "from" price). Documented in the port docstring.
- Step 8 (tests) skipped on purpose: it belongs to the tester phase.
- Smoke test against the running API covered only an empty catalog (no admin session used): routes, validation (`size=49` -> 422), 404 code and SQL execution including `unaccent`. Acceptance criteria on real data are NOT yet verified.

**Repair round 1** (2026-10-09, main session): review → implementing for the review's Low
finding.
- **What breaks:** full-width `％`, `＿` and `＼` in `q` become wildcards. `unaccent` maps
  them to `%`, `_` and `\` *after* the Python escaping (`sql_perfume_queries.py:100-103`,
  `:157-161`). So `?q=％` lists the whole catalog.
- **In scope:** step 4's search escaping, and acceptance criterion "`?q=100%` … does not
  match everything".
- **Repair:** escape after normalizing, so no character can turn into a wildcard after
  being escaped.
- **What stays valid:** the tests and review evidence above still hold for everything else.
  The tester adds the full-width regression test.

**Repair round 1, done** (2026-10-10). The implementer subagent was interrupted when the
session ended; the main session checked its partial diff and finished the round.

- **The change.** `_like_pattern` in `sql_perfume_queries.py` now builds the pattern in SQL.
  It applies `unaccent(lower(q))` first, then `replace` for `\`, `%` and `_` (in that order),
  wrapped in `'%' … '%'`. The call site compares `unaccent(lower(haystack))` with that
  pattern using `escape="\\"`. Because the folding happens before the escaping, no character
  can become a wildcard afterwards.
- **The in-memory adapter** needs no change: it matches by substring (`in`), not `LIKE`.
- **psql check on `fragancia_test`.** Old pattern vs new:
  - `＿` and `％` no longer match `'abc'`; before, they matched.
  - `％` and `100％` match `'100% oud'` literally.
  - `＼` matches nothing.
- **Runs.** `uv run just check` is green (1120 passed, 3 skipped; harness 684).
  `uv run just test-integration` gives 235 passed, 2 skipped.

## Test coverage

Tester phase, 2026-10-09. Files (all new): `apps/api/tests/unit/catalog/test_perfume_public_domain.py`
(23), `test_public_perfume_queries.py` (14), `test_public_perfume_http.py` (23);
`apps/api/tests/integration/catalog/test_sql_public_perfumes.py` (67). No GAP and no NOT
CONFIRMED: every behavior in the plan's "Test layers required" row was confirmed from code or a
local run. Product code was not touched.

| Behavior | Source | Layer | Test (file::name) | State |
| -------- | ------ | ----- | ----------------- | ----- |
| Sale start inclusive, end exclusive, open ends, tz-aware instants | `perfume.py:211` | domain | `test_perfume_public_domain.py::test_sale_is_active_with_the_start_inclusive_and_the_end_exclusive` (11 cases), `::test_sale_activity_is_compared_as_instants_across_time_zones` | CONFIRMED |
| `effective_price`: sale price while active, regular otherwise | `perfume.py:272` | domain | `::test_the_effective_price_*` (3 tests, 6 cases) | CONFIRMED |
| `retired_slugs`: empty on create, appended only when the slug changes, in order, nothing on a refused update | `perfume.py:315,375` | domain | `::test_a_new_perfume_has_no_retired_slugs`, `::test_update_records_the_slug_it_replaces`, `::test_update_that_keeps_the_slug_retires_nothing`, `::test_successive_renames_accumulate_*`, `::test_a_refused_update_retires_nothing` | CONFIRMED |
| `q` trimmed; blank becomes None; other filters untouched | `queries/perfumes.py:34` | application | `test_public_perfume_queries.py::test_a_blank_search_becomes_no_search`, `::test_the_search_is_trimmed_before_the_query`, `::test_the_other_filters_reach_the_query_untouched` | CONFIRMED |
| `clock.now()` passed to list and get | `queries/perfumes.py:35,50` | application | `::test_listing_passes_the_clock_now_to_the_query`, `::test_get_passes_the_clock_now_to_the_query` | CONFIRMED |
| Get: `PerfumeNotFound` for unknown and hidden; old slug answers the current one | `queries/perfumes.py:48-53` | application | `::test_get_answers_not_found_for_an_unknown_slug`, `::..._for_a_hidden_perfume`, `::test_an_old_slug_finds_the_perfume_and_answers_the_current_slug`, `::test_get_returns_the_visible_perfume` | CONFIRMED |
| In-memory adapter follows the same visibility and price rules (it is the port double for the layers above) | `in_memory_perfumes.py:164-260` | application | `::test_the_in_memory_list_uses_the_domain_effective_price_at_the_clock_time`, `::test_the_in_memory_list_hides_hidden_archived_and_archived_brand_perfumes` | CONFIRMED |
| Query parsing: defaults, repeatable `brand`/`family`/`gender`, `Literal` sort and gender, `size` 1..48, `page>=1`, prices `>=0`, `q` max 100; invalid is 422 `VALIDATION_ERROR` | `perfume_router.py:224-252` | http | `test_public_perfume_http.py::test_the_list_needs_no_session_*`, `::test_brand_family_and_gender_are_repeatable`, `::test_every_other_parameter_is_parsed`, `::test_every_documented_sort_is_accepted`, `::test_invalid_query_parameters_are_a_validation_error` (9), `::test_the_limits_themselves_are_valid`, `::test_a_blank_search_reaches_the_query_as_no_search` | CONFIRMED |
| Response shapes: card, detail (active presentations by ml, effective price, regular price and sale end only while on sale) | `contracts.py` Public*; `perfume_router.py:255-263` | http | `::test_the_list_answers_cards_with_refs_and_the_from_price`, `::test_the_detail_answers_active_presentations_with_the_effective_price` | CONFIRMED |
| Detail 404 `CATALOG_PERFUME_NOT_FOUND` (unknown, hidden); slug `max_length=240` (241 is 422) | `perfume_router.py:255-263` | http | `::test_an_unknown_slug_is_a_404_*`, `::test_a_hidden_perfume_is_a_404`, `::test_a_slug_longer_than_240_characters_*` | CONFIRMED |
| Public routes need no session; admin routes stay protected | `perfume_router.py:221`; `support.py` | http | every test above uses no cookie; `assert_admin_routes_are_protected` in `_client` | CONFIRMED |
| Visibility: published, not archived, brand active; archived family/concentration still listed; no active presentation is not listed (Deviation 2) | `sql_perfume_queries.py:88,139` | integration | `test_sql_public_perfumes.py::test_only_published_unarchived_perfumes_of_active_brands_are_listed`, `::test_an_archived_family_or_concentration_does_not_hide_a_perfume`, `::test_a_perfume_without_active_presentations_is_not_listed`, `::test_card_fields_come_from_the_perfume_and_its_refs` | CONFIRMED |
| Effective price and `on_sale` in SQL equal the domain at window boundaries (12 windows, microsecond precision; list card and detail) | `sql_perfume_queries.py:71-85` vs `perfume.py:211,272` | integration | `::test_sql_effective_price_and_on_sale_agree_with_the_domain_at_the_boundaries` (12 params), `::test_the_boundary_matrix_covers_both_outcomes` | CONFIRMED |
| From price = min effective among active presentations; archived presentation and its sale ignored; `now` drives the result | `sql_perfume_queries.py:139-150` | integration | `::test_the_from_price_is_the_lowest_effective_price_among_active_presentations`, `::test_a_sale_makes_its_presentation_the_cheapest`, `::test_on_sale_ignores_the_sale_of_an_archived_presentation`, `::test_the_price_depends_on_the_now_passed_to_the_query`, `::test_a_sale_ending_in_the_future_reports_the_regular_price_and_the_end` | CONFIRMED |
| Filters: brand/family/gender (multi), price range inclusive on the effective from price, AND-combination, unknown slugs, no leak of hidden | `sql_perfume_queries.py:151-160` | integration | `::test_the_brand_filter_accepts_several_slugs`, `::test_the_family_filter`, `::test_the_gender_filter_accepts_several_values`, `::test_the_price_range_filters_the_from_price_inclusively`, `::test_the_price_range_uses_the_effective_from_price_not_the_regular_one`, `::test_filters_combine_with_and`, `::test_an_unknown_slug_in_a_filter_matches_nothing`, `::test_filters_never_reveal_non_visible_perfumes` | CONFIRMED |
| Search: accent and case insensitive over name, brand, notes of all levels; substring; combines with filters; no leak of hidden | `sql_perfume_queries.py:161-176` | integration | `::test_search_finds_a_brand_*`, `::test_search_finds_a_perfume_name_*`, `::test_search_finds_notes_of_any_level`, `::test_search_matches_substrings_*`, `::test_search_with_no_match_*`, `::test_search_combines_with_the_other_filters`, `::test_search_never_reveals_non_visible_perfumes` | CONFIRMED |
| `%`, `_` and `\` in `q` are literal; SQL-looking text is inert | `sql_perfume_queries.py:100-103` | integration | `::test_search_treats_percent_and_underscore_and_backslash_literally`, `::test_search_with_sql_looking_text_is_inert` | CONFIRMED |
| Sorts: name (brand, name, case-insensitive, id tie-break), price asc/desc (+ name), newest (+ id, NULL last); pagination slices, totals, beyond-last page; one row per perfume | `sql_perfume_queries.py:177-190` | integration | `::test_the_default_order_is_brand_then_name_ignoring_case`, `::test_name_ties_break_by_id`, `::test_price_ascending_*`, `::test_price_descending_*`, `::test_price_sort_uses_the_lowest_*`, `::test_newest_orders_*`, `::test_newest_puts_a_missing_publication_date_last`, `::test_pagination_slices_*`, `::test_the_total_counts_the_filtered_set_not_the_page`, `::test_a_perfume_with_many_presentations_is_listed_once` | CONFIRMED |
| Detail: active presentations by ml, notes, refs, lead times; non-visible and unknown are None | `sql_perfume_queries.py:239-300` | integration | `::test_the_detail_lists_active_presentations_ordered_by_ml`, `::test_the_detail_is_found_for_a_family_or_concentration_archived_perfume`, `::test_the_detail_of_a_non_visible_perfume_is_not_found` (3), `::test_an_unknown_slug_is_not_found`, `::test_archiving_a_presentation_hides_it_from_the_public_detail` | CONFIRMED |
| Slug history written on rename (`retired_at = updated_at`), none when unchanged; old slug returns the current slug; chain of renames; history never exposes hidden/archived/archived-brand perfumes; latest dropper owns a reused slug and the current owner wins while it is held | `sql_perfume_repository.py:219-228`; `sql_perfume_queries.py:265` | integration | `::test_renaming_writes_the_old_slug_to_the_history`, `::test_an_update_that_keeps_the_slug_writes_no_history`, `::test_the_old_slug_finds_the_perfume_and_answers_the_current_slug`, `::test_every_retired_slug_of_successive_renames_still_resolves`, `::test_the_history_does_not_make_a_hidden_perfume_visible`, `::test_the_history_does_not_make_an_archived_brand_perfume_visible`, `::test_a_slug_is_owned_by_the_latest_perfume_that_dropped_it` | CONFIRMED |
| Migration 0008: `unaccent` installed and folds `Lancôme`; history PK and FK enforced | `0008_*.py` | integration | `::test_the_unaccent_extension_is_installed`, `::test_the_history_slug_is_the_primary_key`, `::test_a_history_slug_cannot_point_to_a_missing_perfume` | CONFIRMED |
| Migration 0008 round trip on `fragancia_test` | `0008_*.py` | tooling (manual) | `alembic -x test=true downgrade 0007`, then `upgrade head` (see below) | CONFIRMED |
| Container resolves the two new use cases | `module.py:179-180` | composition | existing `tests/unit/test_container.py` (green in both runs) | CONFIRMED |

Migration round trip (test database only, 2026-10-09): `alembic -x test=true current` showed
`0008 (head)`; `downgrade 0007` ran `0008 -> 0007` and `current` showed `0007`; `upgrade head`
ran `0007 -> 0008` and `current` showed `0008 (head)`. After the round trip
`to_regclass('catalog.perfume_slug_history')` is not null and the `unaccent` extension is
installed (the downgrade leaves the extension by design). I did not inspect the table between
downgrade and upgrade.

Runs (the two allowed):

- Baseline, before writing tests: `uv run just check` green (1060 unit passed, 3 skipped; 684
  harness tests passed); `uv run just test-integration` 168 passed, 2 skipped. Nothing failed.
- Closing: `uv run just check` green (1120 unit passed, 3 skipped, +60; 684 harness passed);
  `uv run just test-integration` 235 passed, 2 skipped (+67). One intermediate `check` run
  caught two mypy `no-any-return` errors in my own new test file (fixed, rerun green). Between
  runs I only ran the files I touched.

Not covered, on purpose: the HTTP layer against the real SQL adapter (the http tests use the
in-memory adapter; SQL behavior is covered at the integration layer), and the web's 301 (out of
scope).

## Review findings

Reviewer, 2026-10-09, diff `main...HEAD` on `feat/catalog-public` (worktree clean). No PR
exists yet, so the PR-body item is not applicable at this phase. The main session must check it
before merging.

**Checklist: 14/14 applicable items pass (PR body N/A).**

- [x] `plans-scope`: 19 changed files, all inside the plan. No hot file was touched
      (`container.py`, `modules.json`, `.importlinter`).
- [x] `uv run just check` is green, including 684 hook tests and the drift check.
- [x] `uv run just test-integration`: 235 passed, 2 skipped.
- [x] Business rules are in the domain. `Sale.is_active` and `Presentation.effective_price`
      live in `domain/perfume.py:211,272`. The SQL `CASE` is the planned mirror and is pinned to
      the domain by the 12-window test. The detail maps each row through the domain methods.
- [x] CQRS-lite holds. The reads go through `PerfumeQueries` and return response models. The
      slug history is written in `SqlPerfumeRepository.save` inside the existing savepoint.
- [x] The contracts are in `contracts.py`, and `openapi.json` was regenerated (the drift check
      is green).
- [x] A missing perfume is `Err(PerfumeNotFound)` → 404 `CATALOG_PERFUME_NOT_FOUND`.
- [x] Money is integer cents. `now` comes from `Clock` in both use cases.
- [x] Migration 0008 is new and has no drops in `upgrade`. Its FK stays inside `catalog`.
      `downgrade` drops only the index and the table, and its comment explains why the
      extension stays. The tester's round trip is recorded.
- [x] The public routes use `public_router`. Admin protection was asserted in the HTTP tests.
- [x] The wiring resolves (`module.py:179-180`; `test_container.py` is green).
- [x] There are no secrets and no real personal data.
- [x] `## Deviations` is honest. I spot-checked two claims. `concat_ws` is at
      `sql_perfume_queries.py:148`. The inner join to `prices` is at `:175/:195`, and the port
      docstring says "with at least one active presentation".
- [x] `docs/architecture.md:55` was updated as planned. README decisions 40–44 match the code.

**Ruling on the Deviations.**

1. `concat_ws` instead of `||`: **accepted.** The arrays are `NOT NULL`. An empty array
   only adds blank separators, and that does not change any `contains` match.
2. A visible perfume with no active presentation is left out of the list: **accepted.**
   It is unreachable in practice. The domain refuses to archive the last active presentation of a
   published perfume, and publishing needs one (`perfume.py` `publish`). The detail still answers
   for such a perfume with `presentations: []`, and that is harmless.
3. Tests were left to the tester: **accepted**, as planned.
4. The smoke test ran only on an empty catalog: **accepted** for this phase. The acceptance
   criteria on real data remain for verify.

**Findings.** There are no Critical, High or Medium findings. One Low finding needs a code change.

- **Low: a full-width `％` or `＿` in `q` becomes a live LIKE wildcard.**
  `sql_perfume_queries.py:100-103` (`_like_pattern`) and `:157-161`.
  - **What fails:** the code escapes `\`, `%` and `_` in Python, then applies
    `unaccent(lower(...))` to the escaped pattern in SQL. `unaccent` folds the full-width forms
    to ASCII. I checked this on `fragancia_test`:
    `unaccent(lower('％ ＿ ＼'))` = `% _ \`, and `'abc' LIKE unaccent(lower('%＿%'))` is true.
  - **Failure scenario:** `GET /api/v1/perfumes?q=％` (a phone keyboard in full-width mode, or
    a paste) lists the whole visible catalog instead of nothing. `q=＿` matches any perfume. A
    full-width `＼` turns into an escape character and changes the meaning of the next
    character. This breaks the acceptance criterion "`?q=100%` … does not match everything" for
    the full-width variant.
  - **Impact:** only visible perfumes appear, so nothing leaks and no money is affected.
  - **Possible fixes (the implementer chooses):** run `unicodedata.normalize("NFKC", q)` in
    `_like_pattern` before escaping, or escape after folding in SQL
    (`replace(...unaccent(lower(:q))...)`). Either needs an integration test with `％` and `＿`.

**Checked and found correct (no finding):**

- **Effective price, SQL against the domain.** Both sides use the same predicate:
  `sale_price IS NOT NULL AND (starts IS NULL OR starts <= now) AND (ends IS NULL OR now < ends)`.
  It never evaluates to NULL, so `bool_or` and `CASE` cannot drift. `now` is a single tz-aware
  bind compared with `timestamptz`, both at microsecond precision. Archived presentations are
  excluded before `min`/`bool_or`. The price filters compare the effective "from" price,
  inclusively.
- **Visibility.** The same `_VISIBLE` applies to the list, to the detail by current slug and to
  the detail by history. The history cannot expose a hidden, archived or archived-brand perfume.
- **Sort stability.** Every sort ends on `perfumes.id`, which is unique: `name` is
  `lower(brand), lower(name), id`, price is `price_from, lower(name), id`, and `newest` is
  `first_published_at DESC NULLS LAST, id`. LIMIT/OFFSET pages are deterministic. The count
  uses the same joins and conditions.
- **Slug history.** The upsert gives the slug to the latest perfume that dropped it.
  `retired_at = updated_at`, which `update` sets. `retired_slugs` is cleared only after a
  successful save, and a refused update appends nothing. Lookup order is the current visible
  slug, then the history. The answer always carries the current slug.

**Info (no change requested):**

- (Uncertain, by design) Suppose perfume A dropped `x`, and perfume B now holds `x` but is
  hidden. Then `GET /perfumes/x` resolves through the history to A, and the web would 301 a
  hidden product's URL to a different perfume. The plan prescribes this. The user may want it
  noted.
- The in-memory double differs from SQL in places. `_fold` (NFD) does not fold `ø`, `ß` or `Œ`
  the way `unaccent` does. Python sorts by code point, while the database sorts with the
  `en_US.utf8` collation. This only matters to unit tests that rely on those characters, and
  none do.
- The migration file name `0008_catalog_perfume_slug_history_and_.py` was cut off by Alembic's
  default `truncate_slug_length` (40). The revision id inside is correct (`0008` ← `0007`), so
  the name is cosmetic. The migration is applied, so renaming it is optional. A rename would
  only change the file name, never the revision.

**Out of scope, filed:** `plans/findings/catalog-unbounded-page-offset-overflow.md`. Every list
route accepts an unbounded `page`, and `(page-1)*size` overflows the `bigint` OFFSET, giving a
500. The defect existed before plan 004, but this plan makes one of those routes public.

**Status:** stays `review`, because the Low finding needs a code change. If the user accepts the
full-width wildcard behavior instead, the main session can move the plan to `verify`.

## Verification
