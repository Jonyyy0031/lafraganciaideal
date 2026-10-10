---
status: testing
module: catalog
min_implementer: small
depends_on: ["004"]
---

# 005 — Bound the page number of every list

## Context

Finding `plans/findings/catalog-unbounded-page-offset-overflow.md` (open): every paginated
list route declares `page: Annotated[int, Query(ge=1)] = 1` with no upper bound —
`apps/api/src/fragancia_api/modules/catalog/http/router.py:117,173,247` (brands, families,
concentrations admin lists) and `apps/api/src/fragancia_api/modules/catalog/http/perfume_router.py:69`
(admin perfumes) and `:234` (public perfumes, unauthenticated since plan 004). The adapters
bind `(page - 1) * size` as the SQL OFFSET (a `bigint`), so `?page=10**18` overflows and the
API answers 500. The page size is already bounded by `MAX_PAGE_SIZE = 100` in
`apps/api/src/fragancia_api/shared/contracts/__init__.py:5-6`, which every module imports.

Approach: a shared `MAX_PAGE = 10_000` next to `MAX_PAGE_SIZE` (10,000 × 100 rows is far
beyond any real catalog and far inside int64), used as `le=MAX_PAGE` on the five `page`
parameters, so a huge page is a 422 `VALIDATION_ERROR` before any query runs. README decision
45. Discarded: catching the database error and mapping it (the request would still reach the
database, and the error type differs between drivers).

## Out of scope

Paginated lists in other modules (none exist yet besides catalog), cursor pagination, the
`Page` response model, any other query parameter.

## Dependencies

- **004** (`done`): the public perfume list at `perfume_router.py:234`.

## Steps

1. **Shared bound and the five routes**
   - Files: `apps/api/src/fragancia_api/shared/contracts/__init__.py` (modify), `apps/api/src/fragancia_api/modules/catalog/http/router.py` (modify), `apps/api/src/fragancia_api/modules/catalog/http/perfume_router.py` (modify), `apps/api/openapi.json` (modify)
   - Do: add `MAX_PAGE = 10_000` after `MAX_PAGE_SIZE` with a one-line comment ("keeps
     (page - 1) × size far inside the SQL OFFSET range"). Change the five `page` parameters to
     `Query(ge=1, le=MAX_PAGE)` (import `MAX_PAGE` where `MAX_PAGE_SIZE` is imported). Run
     `uv run just openapi`.
   - Observable result: `uv run just check` green; `openapi.json` shows `maximum: 10000` on
     the five `page` parameters.

2. **Close the finding**
   - Files: `plans/findings/catalog-unbounded-page-offset-overflow.md` (modify)
   - Do: `status: resolved`, `plan: catalog-perfumes/005`, and a short Resolution section.
   - Observable result: `uv run just plans-lint` OK.

3. **Tests (tester phase)**
   - Files: `apps/api/tests/unit/catalog/` (create)
   - Do: http tests: `page=10000` accepted, `page=10001` and `page=10**18` → 422
     `VALIDATION_ERROR` on the public perfume list and on each admin list.
   - Observable result: `uv run just check` green.

## Acceptance criteria

- [ ] `GET /api/v1/perfumes?page=1000000000000000000` → 422 `VALIDATION_ERROR` (was a 500).
- [ ] `GET /api/v1/perfumes?page=10000` → 200 with empty `items` and the real `total`.
- [ ] The four admin lists (`/admin/brands`, `/admin/olfactory-families`,
      `/admin/concentrations`, `/admin/perfumes`) answer 422 for `page=10001`.
- [ ] `openapi.json` regenerated; the drift check passes.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | no      |       |
| application | no      |       |
| http        | yes     | Bound on all five routes: 10000 ok, 10001 and huge → 422 |
| integration | no      | The query never runs for an invalid page |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

None.

## Test coverage

## Review findings

## Verification
