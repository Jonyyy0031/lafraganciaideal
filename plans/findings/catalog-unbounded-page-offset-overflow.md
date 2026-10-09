---
status: open
module: catalog
found: 2026-10-09
---

# A huge `page` overflows the SQL OFFSET and answers 500 instead of 422

## Found while

Reviewing catalog-perfumes/004 (public catalog).

## What

Every paginated list route declares `page: Annotated[int, Query(ge=1)] = 1` with no upper
bound: `apps/api/src/fragancia_api/modules/catalog/http/router.py:117,173,247` and
`apps/api/src/fragancia_api/modules/catalog/http/perfume_router.py:69,234`. The adapters bind
`(page - 1) * size` as the OFFSET, a PostgreSQL `bigint`. On the local test database,
`select 1 offset 23999999999999999976` fails with `ERROR: bigint out of range`; through asyncpg
an out-of-int64 parameter fails before reaching the server. No exception handler in
`apps/api/src/fragancia_api/shared/http/errors.py:114-118` maps it, so the answer would be an
unexpected 500. Not reproduced over HTTP: the API was not running during the review (uncertain
only in the exact exception type, not in the failure).

## Why it matters

Since plan 004 one of these routes is public and unauthenticated (`GET /api/v1/perfumes`), so
anyone can trigger 500s and error-log noise with `?page=1000000000000000000`. No data is
exposed.

## Suggested next step

A fast-lane fix: an upper bound on `page` (for example a `le=` that keeps
`page * MAX_PAGE_SIZE` far inside int64), shared by every list route, plus an HTTP test for
422 `VALIDATION_ERROR`.
