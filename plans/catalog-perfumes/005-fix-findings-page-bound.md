---
status: done
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

- [x] `GET /api/v1/perfumes?page=1000000000000000000` → 422 `VALIDATION_ERROR` (was a 500).
- [x] `GET /api/v1/perfumes?page=10000` → 200 with empty `items` and the real `total`.
- [x] The four admin lists (`/admin/brands`, `/admin/olfactory-families`,
      `/admin/concentrations`, `/admin/perfumes`) answer 422 for `page=10001`.
- [x] `openapi.json` regenerated; the drift check passes.

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

Baseline `uv run just check`: 684 passed. All tests are http layer, in the existing files of
`apps/api/tests/unit/catalog/`; the over-bound case is parametrized on `page` in {10_001, 10**18}.

| Behavior | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| Public list accepts `page=10000` and passes it to the query | `perfume_router.py` (`le=MAX_PAGE`) | http | `test_public_perfume_http.py::test_the_last_allowed_page_is_accepted_and_reaches_the_query` | CONFIRMED |
| Public list: 10001 / 10**18 -> 422 `VALIDATION_ERROR`, query never runs | `perfume_router.py` | http | `test_public_perfume_http.py::test_a_page_past_the_bound_is_a_422_and_the_query_never_runs` | CONFIRMED |
| Admin perfumes: 10000 -> 200 empty; 10001 / 10**18 -> 422 | `perfume_router.py` | http | `test_perfume_http.py::test_the_last_allowed_page_is_empty_not_an_error`, `::test_a_page_past_the_bound_is_a_422_before_any_query` | CONFIRMED |
| Admin brands: same | `router.py` | http | `test_brand_http.py` (same two names) | CONFIRMED |
| Admin olfactory families: same | `router.py` | http | `test_catalog_maintenance_http.py` (same two names) | CONFIRMED |
| Admin concentrations: same | `router.py` | http | `test_concentration_http.py` (same two names) | CONFIRMED |

15 tests added (5 routes x 3 cases). No GAP, no NOT CONFIRMED. Integration not required by the
plan. Closing `uv run just check`: green (see the tester report).

## Review findings

Reviewed 2026-10-10, diff `git diff 87678b6..HEAD` (plan 004 commits excluded).

**Checklist: 15/15 applicable items pass** (N/A counted as pass; nothing failed).

- plans-scope (`--base 87678b6`): pass, "Every change is inside the plan" (6 declared, 11
  changed; the tests fall under the declared `apps/api/tests/unit/catalog/`). No hot files touched.
- `uv run just check`: pass (exit 0; ruff, mypy 207 files, import-linter 7 kept, plans OK,
  1135 api tests passed / 3 skipped, 684 hook tests passed, harness-check up to date,
  `tests/unit/test_openapi.py` drift test green).
- test-integration: N/A (no `infrastructure/`, tables or migrations changed).
- Business rules / CQRS-lite / Result errors / Money-Clock-ids / migrations / wiring: N/A or
  unchanged; the change is HTTP-boundary validation only.
- Contracts: `MAX_PAGE` lives next to `MAX_PAGE_SIZE` in `shared/contracts/__init__.py:7`;
  `openapi.json` regenerated (five `maximum: 10000` additions only, matching the five routes).
- Routes: still declared via `admin_router()` / `public_router()`; no new routes.
- No secrets or personal data.
- Deviations "None": spot-checked, accurate (all five parameters at `router.py:117,173,247`
  and `perfume_router.py:69,234` carry `le=MAX_PAGE`; `grep "page: Annotated"` finds no others).
- Docs: README decision 45 present; no doc describes the page parameter bound, nothing stale.
  Finding `catalog-unbounded-page-offset-overflow.md` resolved with `plan:` and Resolution.
- PR body: N/A, no PR exists yet for this branch.

**Bug hunt:** no findings requiring changes. `(MAX_PAGE - 1) × MAX_PAGE_SIZE` = 999,900 rows,
far inside int64; FastAPI parses `10**18` as an int and the `le` check rejects it with the
standard 422 `VALIDATION_ERROR` before the use case is resolved (the public test asserts the
stub query never ran). Public route keeps its own `size le=48`. No authorization change.

**Low (no change required):** the four admin tests named
`test_a_page_past_the_bound_is_a_422_before_any_query` (`test_brand_http.py:151`,
`test_catalog_maintenance_http.py:431`, `test_concentration_http.py:383`,
`test_perfume_http.py:864`) assert only the 422, not that the query did not run; the name
overclaims slightly. The behavior is guaranteed by FastAPI validation and is asserted
explicitly on the public route, so this is cosmetic.

All passed.

## Verification

Verified on 2026-10-10 by the main session, inline, on `feat/catalog-public` at 798f8fe.

**Suite.** `uv run just check` (run once):
- ruff: "All checks passed!".
- mypy: "no issues found in 207 source files".
- Unit tests: `1135 passed, 3 skipped`.
- Harness tests: `684 passed`.

No infrastructure changed, so the integration tests were not run.

**Running app** (`uv run just api` on port 8100, started and stopped by me):

| Request | Session | Result |
| ------- | ------- | ------ |
| `GET /perfumes?page=1000000000000000000` | none | 422 `VALIDATION_ERROR` (was the OFFSET overflow 500) |
| `GET /perfumes?page=10000` | none | 200 `{"items":[],"total":0,"page":10000,"size":24}` |
| `GET /perfumes?page=10001` | none | 422 |
| `GET /admin/brands?page=10001` | `verifier@example.test` | 422 |
| `GET /admin/olfactory-families?page=10001` | `verifier@example.test` | 422 |
| `GET /admin/concentrations?page=10001` | `verifier@example.test` | 422 |
| `GET /admin/perfumes?page=10001` | `verifier@example.test` | 422 |
| `GET /admin/concentrations?page=10000` | `verifier@example.test` | 200 with empty `items` and the real `total: 5` |

The server log has no 500 and no traceback. No rows were created.

**NOT VERIFIED:** nothing in this plan's scope.

Every acceptance criterion passes. The plan is ready for the user to set `done`.
