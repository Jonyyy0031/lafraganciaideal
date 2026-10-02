---
status: done
module: catalog
depends_on: [platform-api-foundation/001]
---

# 002 — Catalog brands as the reference module

## Context

Plan 001 delivers the platform: kernel (`Result`, errors, `AggregateRoot`), `TransactionRunner`,
`OutboxEventPublisher`, `require_admin` / `public_router()` / `admin_router()`, error mapping,
`AppModule` wiring and import-linter contracts. No business module exists.

This plan builds the first vertical slice — perfume brands — through every layer of a new
`catalog` module. It is deliberately small and complete: it becomes the pattern every later
module copies by name (the role `employees` plays in web-rh, `AGENTS.md` there → "the
reference module").

Rules: a brand has a name (trimmed, internal whitespace collapsed, 2–80 characters) unique
case- and accent-insensitively, a URL slug derived from the name (unique), and an active flag
(new brands are active). Customers see active brands only; the admin sees all.

## Out of scope

- Perfumes, sizes, prices, photos, stock (later catalog/inventory plans).
- Updating, deactivating or deleting brands (next catalog plan).
- Real authentication (identity initiative).

## Dependencies

- platform-api-foundation/001: everything listed in Context.

## Steps

1. **Register the module**
   - Files: `docs/modules.json` (modify)
   - Do: `catalog` → `active`, marked as the reference module.
   - Observable result: `catalog` remains a valid commit scope.

2. **Domain**
   - Files: `apps/api/src/fragancia_api/modules/catalog/__init__.py` (create),
     `.../catalog/domain/__init__.py` (create), `.../catalog/domain/brand.py` (create),
     `.../catalog/domain/errors.py` (create), `.../catalog/domain/repositories.py` (create)
   - Do: value objects `BrandName` (normalization + length rule, `key` for uniqueness:
     casefolded, accents removed) and `Slug` (ASCII, lowercase, hyphenated); aggregate `Brand`
     with `Brand.create(name)` recording `BrandCreated` (`catalog.brand.created`). Errors with
     stable codes: `CATALOG_BRAND_NAME_INVALID` (invalid value),
     `CATALOG_BRAND_ALREADY_EXISTS` (conflict). Port `BrandRepository`:
     `exists_with_key(key)`, `add(brand) -> Result[None, BrandAlreadyExists]`.
   - Observable result: domain imports only the kernel.

3. **Contracts**
   - Files: `.../catalog/contracts.py` (create)
   - Do: Pydantic models: `CreateBrandRequest`, `CreatedResponse{id}`, `PublicBrand{id, name,
     slug}`, `AdminBrand{id, name, slug, is_active, created_at}`, `Page[T]{items, total, page,
     size}` (generic in `shared/http` if reusable).
   - Observable result: OpenAPI shows these schemas.

4. **Application**
   - Files: `.../catalog/application/__init__.py` (create),
     `.../catalog/application/commands/create_brand.py` (create),
     `.../catalog/application/queries/list_brands.py` (create),
     `.../catalog/application/ports.py` (create)
   - Do: `CreateBrand.execute(name) -> Result[UUID, DomainError]`: validate → check key →
     `Brand.create` → `add` → publish pulled events, all inside one transaction. Port
     `BrandQueries`: `list_active() -> list[PublicBrand]`, `list_all(page, size) ->
     Page[AdminBrand]`; thin use cases `ListPublicBrands`, `ListAdminBrands`.
   - Observable result: no SQLAlchemy/FastAPI imports in application.

5. **Infrastructure**
   - Files: `.../catalog/infrastructure/__init__.py` (create),
     `.../catalog/infrastructure/tables.py` (create),
     `.../catalog/infrastructure/sql_brand_repository.py` (create),
     `.../catalog/infrastructure/sql_brand_queries.py` (create),
     `.../catalog/infrastructure/in_memory.py` (create),
     `apps/api/migrations/versions/0002_catalog_brands.py` (create)
   - Do: schema `catalog`, table `brands` (id uuid pk, name, name_key unique, slug unique,
     is_active, created_at timestamptz). Repository maps rows ↔ `Brand` explicitly and turns a
     unique violation into `Err(BrandAlreadyExists)` (race-safe). Queries select only needed
     columns, ordered by name. In-memory fakes for both ports.
   - Observable result: migration creates `catalog.brands`; downgrade drops it.

6. **HTTP and wiring**
   - Files: `.../catalog/http/__init__.py` (create), `.../catalog/http/router.py` (create),
     `.../catalog/module.py` (create), `apps/api/src/fragancia_api/container.py` (modify),
     `apps/api/.importlinter` (modify)
   - Do: `GET /api/v1/brands` (public), `POST /api/v1/admin/brands` (admin, 201 `{id}`),
     `GET /api/v1/admin/brands?page&size` (admin). Routers contain no logic. Module registered
     in the container; import-linter layer contract for `catalog`: `http | infrastructure` →
     `application` → `contracts | domain`.
   - Observable result: endpoints visible in `/api/v1/docs` with their access.

7. **Tests**
   - Files: `apps/api/tests/unit/catalog/` (create), `apps/api/tests/integration/catalog/` (create)
   - Do: domain (normalization, length limits, slug and key with accents/case, event recorded),
     application with fakes (created, duplicate → conflict, invalid → error, event published,
     nothing published on failure), http with fakes (201/401/403/409/422, public lists only
     active, pagination), integration (repository round trip, unique race → conflict, queries,
     `catalog.brand.created` row in the outbox in the same transaction).
   - Observable result: all green.

8. **Recipes for the next modules**
   - Files: `docs/recipes/new-module.md` (create), `docs/recipes/new-use-case.md` (create),
     `docs/recipes/db-change.md` (create), `AGENTS.md` (modify), `docs/architecture.md` (modify)
   - Do: step-by-step "copy catalog/brands" instructions with file names; how to add a
     migration; AGENTS.md names `catalog` as the reference module.
   - Observable result: the recipes reference real files.

## Acceptance criteria

- [ ] `POST /api/v1/admin/brands {"name":"  Maison   Margiela "}` with the dev token → 201
      `{id}`; the row has name `Maison Margiela`, slug `maison-margiela`, `is_active` true.
- [ ] Same request without token → 401 `AUTHENTICATION_REQUIRED`; wrong token → 401.
- [ ] `{"name":"maison margiéla"}` afterwards → 409 `CATALOG_BRAND_ALREADY_EXISTS`.
- [ ] `{"name":"x"}` → 422 `CATALOG_BRAND_NAME_INVALID`; `{}` → 422 `VALIDATION_ERROR`.
- [ ] `GET /api/v1/brands` (no token) lists active brands `{id, name, slug}` ordered by name.
- [ ] `GET /api/v1/admin/brands?page=1&size=2` returns `{items, total, page, size}`.
- [ ] Creating a brand writes a `catalog.brand.created` outbox row that the worker marks published.
- [ ] `uv run just check`, `uv run just test-integration` and CI are green.

## Test layers required

| Layer       | Applies | Focus                                                     |
| ----------- | ------- | --------------------------------------------------------- |
| domain      | yes     | BrandName, Slug, Brand.create, event                       |
| application | yes     | CreateBrand and list use cases with in-memory fakes        |
| http        | yes     | status codes, error codes, access, response shapes         |
| integration | yes     | SQL repository/queries, unique race, outbox in transaction |
| e2e         | no      | no web app yet                                             |

## Deviations

1. **Uniqueness by slug only** — the plan had a `name_key` unique column plus a unique slug;
   the slug already folds case, accents and punctuation ("Maison Margiela" = "maison
   margiéla"), so one unique constraint (`uq_brands_slug`) is the rule. No `name_key` column.
2. **Event field `brand_name`** — `name` is the event's class-level identifier
   (`catalog.brand.created`), so the brand's name travels as `brand_name`.
3. **`Page[T]` lives in `fragancia_api.shared.contracts`** (plan suggested `shared/http`) so
   application ports can return it without importing the HTTP layer.
4. **One in-memory adapter** (`InMemoryBrands`) implements both `BrandRepository` and
   `BrandQueries` over a shared store.
5. **Extra integration test** forcing the unique violation inside a savepoint (the concurrent
   test can resolve through the `exists` check alone).
6. **Migration** generated with `just db-revision "catalog brands"`, then edited by hand to
   create/drop the `catalog` schema (autogenerate does not), as `docs/recipes/db-change.md`
   now documents.
7. **`Clock` port** (added to the platform, see 001 deviation 3) gives `created_at`.

## Verification

Run on 2026-10-02 with `just api` + `just worker` against the development database.

| Criterion | Result | Evidence |
| --- | --- | --- |
| Create `"  Maison   Margiela "` → 201, normalized row | ✔ | 201 `{"id":"01a0fe48-…"}`; row `Maison Margiela \| maison-margiela \| t` |
| No token / wrong token → 401 | ✔ | both `{"code":"AUTHENTICATION_REQUIRED",...}` 401 |
| `"maison margiéla"` → 409 | ✔ | `CATALOG_BRAND_ALREADY_EXISTS` 409 |
| `"x"` → 422 domain code; `{}` → 422 `VALIDATION_ERROR` | ✔ | `CATALOG_BRAND_NAME_INVALID` with `{"min":2,"max":80}`; `VALIDATION_ERROR` |
| Public list: active, `{id,name,slug}`, by name | ✔ | `Armani, Chanel, Maison Margiela` with only those fields |
| Admin list paginated | ✔ | `page=1&size=2` → 2 items, `total: 3` |
| Outbox row published by the worker | ✔ | 3 × `catalog.brand.created \| published = t` |
| Unique race and savepoint | ✔ | 5 concurrent creates → 1 Ok + 4 conflicts; forced violation keeps the transaction usable |
| `just check`, `just test-integration` | ✔ | 95 unit, 15 integration, 7 import-linter contracts (module contracts proven with a temporary violation) |
| CI green on push | ✔ | PR #1, run [37066830411](https://github.com/Jonyyy0031/lafraganciaideal/actions/runs/37066830411): Quality, Commit messages, API integration, Docker (runtime), Docker (migrator), Local environment — all ✓ |

The three brands created during verification (Armani, Chanel, Maison Margiela) were left in
the development database as sample data.
