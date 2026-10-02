# platform-openapi — Committed OpenAPI document and Scalar reference

## Goal

Make the HTTP contract visible and reviewable: an accurate OpenAPI document committed to the
repo (every contract change shows up in PR diffs and feeds the Angular client in phase 3) and
an interactive Scalar reference to try every route, including admin routes, while developing.

## Plans

| Plan | Title                                        | Depends on                   | Purpose                                                          |
| ---- | -------------------------------------------- | ---------------------------- | ---------------------------------------------------------------- |
| 001  | Accurate document, drift check and Scalar    | platform-api-foundation/002  | Fix responses/names/ids, commit `openapi.json`, serve Scalar (dev) |

## Dependency notes

None.

## Decisions with the user

1. (2026-10-02) Follow web-rh's `platform-openapi`: committed document, `check` fails when it
   is stale, interactive reference outside production.
2. (2026-10-02) UI: **Scalar** at `/api/v1/docs` (replaces FastAPI's Swagger UI).
3. (2026-10-02) Fix the document: 401/403 only on admin routes, 422 with our error shape,
   readable schema names (`AdminBrandPage`) and operation ids (`list_brands`) for the
   generated client.

## Delivered

- **001** (2026-10-02): committed `apps/api/openapi.json` + drift test, `just openapi`,
  Scalar at `/api/v1/docs` (dev), per-router responses, readable ids and schema names.

## Considered and discarded

- **Swagger UI (FastAPI default)**: the user prefers Scalar, as in web-rh.
- **Generating the document only in CI**: changes would not be visible in PR diffs.
