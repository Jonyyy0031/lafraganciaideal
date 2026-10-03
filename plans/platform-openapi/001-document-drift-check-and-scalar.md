---
status: done
min_implementer: mid
module: platform
depends_on: [platform-api-foundation/002]
---

# 001 — Accurate document, drift check and Scalar

## Context

FastAPI already generates OpenAPI 3.1 at `/api/v1/openapi.json` and serves Swagger UI at
`/api/v1/docs` outside production (`apps/api/src/fragancia_api/main/http.py`, `build_app`).
Problems found on 2026-10-02:

- `build_app` declares `responses={401, 403, 422}` globally, so public routes (`/health/*`,
  `GET /brands`) advertise 401/403.
- FastAPI documents 422 as `HTTPValidationError`, but the API answers `ErrorResponse`
  (`VALIDATION_ERROR`, `shared/http/errors.py`).
- Generic schema name `Page_AdminBrand_`; operation ids such as
  `list_brands_api_v1_brands_get` (they become method names in the phase 3 client).
- The document is not committed, so contract changes are invisible in PRs.

Approach (imitates web-rh `plans/platform-openapi/001`): a pure document builder from the
module routers (no settings, no database), a committed `apps/api/openapi.json`, a unit test
that fails when it is stale (so `just check` and CI fail), and Scalar served by
`scalar-fastapi` with the Scalar script pinned.

## Out of scope

- Generating the TypeScript client (phase 3).
- Publishing the reference in production.
- Response examples beyond what the models already declare.

## Dependencies

- platform-api-foundation/002: `build_app`, `admin_router`, catalog routers, `MODULES`.

## Steps

1. **Document builder**
   - Files: `apps/api/src/fragancia_api/shared/http/openapi.py` (create),
     `apps/api/src/fragancia_api/main/openapi.py` (create),
     `apps/api/src/fragancia_api/main/http.py` (modify)
   - Do: operation id = route function name (`generate_unique_id_function`); after FastAPI
     builds the schema, 422 responses point to `ErrorResponse` and `HTTPValidationError` /
     `ValidationError` are removed; description and version in `info`. `main/openapi.py`:
     `document()` builds the app from `MODULES` routers with an empty registry;
     `python -m fragancia_api.main.openapi [--check]` writes or verifies the file.
   - Observable result: `uv run just openapi` writes `apps/api/openapi.json`.

2. **Declared responses per router**
   - Files: `apps/api/src/fragancia_api/shared/http/access.py` (modify),
     `apps/api/src/fragancia_api/shared/http/health.py` (modify),
     `apps/api/src/fragancia_api/shared/contracts/__init__.py` (modify)
   - Do: `admin_router` adds 401/403 `ErrorResponse`; nothing global. Health functions named
     `liveness` / `readiness`. `Page[T]` is named `<T>Page`.
   - Observable result: public operations list no 401/403; admin ones do.

3. **Scalar**
   - Files: `apps/api/pyproject.toml` (modify), `uv.lock` (modify),
     `apps/api/src/fragancia_api/shared/http/reference.py` (create)
   - Do: `scalar-fastapi`; route `GET /api/v1/docs` (not in the schema) only when docs are
     enabled; script pinned to `@scalar/api-reference@1.72.4`; bearer auth preselected and
     persisted in the browser for convenience.
   - Observable result: `/api/v1/docs` shows Scalar in development; 404 in production.

4. **Drift check and recipe**
   - Files: `apps/api/openapi.json` (create), `apps/api/tests/unit/test_openapi.py` (create),
     `justfile` (modify)
   - Do: `just openapi` regenerates; a unit test compares the committed file with `document()`
     and fails with "run `uv run just openapi`". Tests for: responses per access, 422 schema,
     no FastAPI validation schemas, unique readable operation ids, schema names, Scalar served
     and pinned, hidden in production.
   - Observable result: changing a contract without regenerating fails `just check`.

5. **Docs**
   - Files: `apps/api/README.md` (modify), `AGENTS.md` (modify), `docs/recipes/new-use-case.md` (modify)
   - Do: document `just openapi`, the committed file and Scalar.

## Acceptance criteria

- [ ] `GET /api/v1/docs` (dev) renders Scalar with the five operations; Authorize with
      `dev-admin-token` makes `POST /admin/brands` succeed from the page.
- [ ] `apps/api/openapi.json` lists 401/403 only on `/api/v1/admin/*`, 422 → `ErrorResponse`,
      schema `AdminBrandPage`, operation ids `liveness`, `readiness`, `list_brands`,
      `create_brand`, `list_all_brands`.
- [ ] Editing a contract without `just openapi` makes `uv run just check` fail.
- [ ] `APP_ENV=production` → `/api/v1/docs` and `/api/v1/openapi.json` are 404.
- [ ] CI green.

## Test layers required

| Layer       | Applies | Focus                                          |
| ----------- | ------- | ---------------------------------------------- |
| http        | yes     | document shape, Scalar page, production toggle |
| integration | no      | no persistence change                          |

## Deviations

1. **Readable page schema via a concrete subclass** — Pydantic names `$defs` from the generic's
   internal reference (`Page_AdminBrand_`), ignoring `model_parametrized_name`. Contracts now
   declare `class AdminBrandPage(Page[AdminBrand])` and ports/queries/routes use it
   (`modules/catalog/contracts.py`, `application/*`, `infrastructure/*`, `http/router.py`).
   The recipe documents the pattern.
2. **The drift check is the unit test** `test_committed_document_is_up_to_date` (runs in
   `just check` and CI's quality job); `python -m fragancia_api.main.openapi --check` exists
   for scripts. No separate CI step.
3. **Scalar in a browser not driven** — the Chrome extension was not connected. Verified the
   served page instead (see below).

## Test coverage

Not applicable: completed before the harness existed (2026-10-02); tests and review evidence are recorded in Verification.

## Review findings

Not applicable: completed before the harness existed (2026-10-02); tests and review evidence are recorded in Verification.

## Verification

Run on 2026-10-02.

| Criterion | Result | Evidence |
| --- | --- | --- |
| Scalar at `/api/v1/docs` (dev) | ✔ (served) / **NOT VERIFIED** (rendered in a browser) | 200 HTML with `@scalar/api-reference@1.72.4` (CDN 200), `url: /api/v1/openapi.json`, `persistAuth: true`, `preferredSecurityScheme: HTTPBearer`; same-origin `POST /admin/brands` with the dev token → 201 |
| 401/403 only on admin, 422 → `ErrorResponse`, `AdminBrandPage`, readable ids | ✔ | `openapi.json`: `liveness [200]`, `readiness [200, 503]`, `list_brands [200]`, `create_brand [201, 401, 403, 409, 422]`, `list_all_brands [200, 401, 403, 422]`; no `HTTPValidationError` |
| Stale document fails `just check` | ✔ | temporary edit of `CreateBrandRequest` examples → test fails with "run `uv run just openapi`", `--check` exit 1; reverted → up to date |
| Hidden in production | ✔ | `test_reference_and_document_are_hidden_without_docs`, `test_production_app_hides_the_docs` |
| `just check`, `test-integration` | ✔ | 103 unit, 15 integration, mypy 81 files, 7 contracts |
| CI green | see PR | |
