---
status: implementing
module: platform
min_implementer: mid
depends_on: ["001"]
---

# 003 — Request body size limit

## Context

Today nothing limits the size of a request body.
- `build_app` adds only `RequestContextMiddleware` and CORS
  (`apps/api/src/fragancia_api/main/http.py:70-79`).
- FastAPI reads a JSON or form body *before* it resolves dependencies, so it reads it before
  `require_admin` runs. An anonymous client can therefore make the API hold a multi-GB body
  in memory (finding `platform-unbounded-request-body`).
- The two binary upload routes stream their body only after the admin checks, and they stop
  at their own limits:
  - the photo upload (`modules/catalog/http/perfume_router.py:259-275`, 10 MiB);
  - the catalog import (`modules/catalog/http/import_router.py:79-96`, 5 MiB).

**Approach** (README decision 7): a pure ASGI middleware, `BodySizeLimitMiddleware`, in
`shared/http/middleware.py`. It wraps `receive`, so it covers chunked bodies too.
- **The limit depends on the media type.**
  - JSON, `application/x-www-form-urlencoded`, `multipart/form-data`, `text/*`, or no
    `Content-Type` → `max_body_bytes` (1 MiB).
  - Any other media type is a binary upload → `max_upload_bytes`, the largest upload the API
    accepts. That is `max(photo_max_bytes, catalog_import_max_bytes)`, 10 MiB today.
- **It knows no route paths**, so modules stay decoupled. The upload routes keep their own
  smaller limits, which are checked after the session.
- **When the limit is exceeded:**
  - A declared `Content-Length` over the limit is refused before the body is read at all.
  - A streamed body is cut off once it passes the limit.
  - Either way the answer is 413 `REQUEST_TOO_LARGE` with the usual error shape
    (`shared/http/errors.py:80-82`).

**Alternatives.**
- **A path → limit table.** Rejected: the platform would have to know the module routes.
- **Starlette's `BaseHTTPMiddleware`.** Rejected: it does not see a chunked body before the
  endpoint reads it, so it cannot cut one off.
- **Relying on the reverse proxy alone.** Rejected: there is no proxy config in the repo yet,
  and local runs would stay unprotected.

## Out of scope

- The production reverse proxy's `client_max_body_size`: there is no proxy config in the repo
  yet. It is noted under "Risks" in the PR.
- The per-route limits of the photo and import routes, which stay unchanged.
- Rate limiting and request timeouts.

## Dependencies

- **001 (done):** `build_app` and `create_app` (`main/http.py`), `error_response`
  (`shared/http/errors.py:80`) and `Settings` (`config.py`).

## Steps

1. **Settings**
   - Files: `apps/api/src/fragancia_api/config.py` (modify), `apps/api/.env.example` (modify)
   - Do:
     - Add `request_max_bytes: int = Field(default=1_048_576, ge=1)` to `Settings`.
     - Add a property, `upload_max_bytes`, returning
       `max(self.photo_max_bytes, self.catalog_import_max_bytes)`.
     - Document `REQUEST_MAX_BYTES`, commented out, in `.env.example`.
   - Observable result: `Settings().request_max_bytes == 1_048_576`, and
     `Settings().upload_max_bytes == 10_485_760`.

2. **Middleware**
   - Files: `apps/api/src/fragancia_api/shared/http/middleware.py` (modify)
   - Do: add `BodySizeLimitMiddleware(app, *, max_body_bytes: int, max_upload_bytes: int)`, a
     plain ASGI class with `async __call__(scope, receive, send)`:
     - **Scope:** anything other than an `http` scope passes through untouched.
     - **Limit:** read `content-type` from `scope["headers"]` and lowercase the media type, up
       to the `;`.
       - `max_body_bytes` when it is `application/json` or ends in `+json`, is
         `application/x-www-form-urlencoded` or `multipart/form-data`, starts with `text/`, or
         is empty.
       - `max_upload_bytes` otherwise.
     - **Declared size:** a `content-length` that is all digits and above the limit → send
       413 at once, without calling the app.
     - **Streamed size:** otherwise wrap `receive` to count the `body` bytes of each
       `http.request` message. Past the limit, raise a private `_BodyTooLarge`. Catch it
       around the inner app call:
       - if the response has not started (track it by wrapping `send` and watching
         `http.response.start`), send the 413;
       - otherwise re-raise.
     - **The 413 body:** `error_response(413, "REQUEST_TOO_LARGE", "The request body is too
       large", {"max_bytes": limit})`, sent through its ASGI `__call__`.
   - Observable result: unit tests drive both paths with a tiny inner app.

3. **Wiring**
   - Files: `apps/api/src/fragancia_api/main/http.py` (modify)
   - Do:
     - `build_app` takes `max_body_bytes: int = 1_048_576` and
       `max_upload_bytes: int = 10_485_760`, keyword-only.
     - It adds the middleware **after** `RequestContextMiddleware`
       (`app.add_middleware(BodySizeLimitMiddleware, ...)`), which makes it the outer layer.
       That way the 413 is sent before any route code runs. Note that Starlette runs the
       last-added middleware first.
     - `create_app` passes `settings.request_max_bytes` and `settings.upload_max_bytes`.
     - Add a line to the `DESCRIPTION` string: bodies over the limit get 413
       `REQUEST_TOO_LARGE`.
     - Run `uv run just openapi`, since the description is part of `openapi.json`.
   - Files: `apps/api/openapi.json` (modify)
   - Observable result: the app answers 413 for a 2 MiB JSON body to any route, including an
     anonymous one, and a 6 MiB `image/jpeg` body to the photo route still gets the route's own
     answer.

4. **Docs**
   - Files: `docs/architecture.md` (modify)
   - Do: one bullet in the HTTP or security section about the body limits and the 413.
   - Observable result: the docs describe the limit.

5. **Tests**
   - Files: `apps/api/tests/unit/` (modify)
   - Do: the tester writes the tests; see "Test layers required".

## Acceptance criteria

- [ ] A JSON body of 2 MiB to `POST /api/v1/auth/login` (no session) gets 413
  `REQUEST_TOO_LARGE` with `details.max_bytes = 1048576`, both with `Content-Length` and
  chunked.
- [ ] A JSON body of 2 MiB to an admin route **without a session** gets 413. Before this plan
  it was read whole and then got 401.
- [ ] A normal login and normal admin JSON calls behave as before.
- [ ] A 12 MiB `image/jpeg` body to the photo route gets 413 `REQUEST_TOO_LARGE`. A 6 MiB one
  with a session reaches the route; a valid photo is accepted, as plan 006 verified.
- [ ] A 4 MiB `.xlsx` upload is still accepted with 202. A 6 MiB one still gets 422
  `CATALOG_IMPORT_TOO_LARGE` from the route, since 6 MiB is below the 10 MiB binary limit.
- [ ] Every 413 response carries `X-Request-ID`.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | no      | |
| application | no      | |
| http        | yes     | The middleware alone: limit by media type (json, `+json`, form, multipart, text, none, binary); declared length over and at the limit; chunked over the limit; the response already started → re-raise; non-http scopes. Through `build_app`: 413 shape, `X-Request-ID`, anonymous admin route, and settings wiring |
| integration | no      | |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

- Step 2/3, `X-Request-ID`: the middleware is the outer layer, so `RequestContextMiddleware`
  never sees the 413. The middleware therefore adds `X-Request-ID` itself (the request's own
  header or a new uuid4 hex), to meet the acceptance criterion. Cosmetic.
- Step 2, streamed bodies: FastAPI catches the `_BodyTooLarge` raised in `receive` while
  parsing the body and answers 400 "error parsing the body" (found by a scratch run). The
  middleware now also sets an `exceeded` flag and, when the app starts any response after the
  limit was passed, replaces it with the 413 and drops the app's remaining messages. The
  re-raise path for a started response is kept as planned. Same behavior as the plan, extra
  mechanism.
- `plans-scope` reports 4 out-of-scope files (`.claude/hooks/format_file.py`,
  `catalog_imports.py`, `test_sql_catalog_import_confirm.py`, `scripts/harness/test_hooks.py`).
  They come from earlier commits on this branch versus `main`, not from this plan's
  working-tree changes.
- Step 5 tests are left to the tester phase.

### Repair round 1 (2026-10-12, main session: verify → implementing)

**Why.** The user asked for the open findings to be fixed ("fixeamos todo eso"). The review
found one Low (L1) and one Nit (N1), and both are fixed here. I1 and I2 are by design
(README decision 7).

**Repair spec.**

Product code: `shared/http/middleware.py` only. Docs: `docs/architecture.md`.

1. **L1: one request id, and no error log for a cut-off body.**
   - Rename `_BodyTooLarge` to a public `BodyTooLarge` (same module).
   - In `RequestContextMiddleware.dispatch`, add `except BodyTooLarge: raise` before
     `except Exception`, so the cut-off is not logged as `request.unhandled_error`. The
     outer `BodySizeLimitMiddleware` turns it into the 413.
   - In `BodySizeLimitMiddleware.__call__`, when the `http` scope has no `X-Request-ID` header,
     generate one, append it to a copy of `scope["headers"]` and pass that scope to the inner
     app. `_refuse` then reads the same id, so the 413 header matches the request id bound
     to the logs.
2. **N1: the wording in `docs/architecture.md`.**
   - The middleware is outside `RequestContextMiddleware` and inside CORS. It is not the
     outermost layer.
   - The upload routes have limits *at or below* the binary limit.
   - Mention the middleware in the end-to-end request flow (`:130-133`).

Then set the status to `testing`. The tester adds three regressions:
- a chunked oversized body to a raw route produces no `request.unhandled_error` log;
- the 413's `X-Request-ID` equals the id the app saw when the client sent none;
- a client-sent `X-Request-ID` is kept.

**Done (implementer, 2026-10-12).**
- L1: `_BodyTooLarge` renamed to `BodyTooLarge`; `RequestContextMiddleware.dispatch` re-raises it before `except Exception` (no `request.unhandled_error` log); `BodySizeLimitMiddleware.__call__` appends a generated `x-request-id` header to a copy of the scope when the client sent none, so the 413 header and the logged id match.
- N1: `docs/architecture.md` now says the middleware is outside `RequestContextMiddleware` and inside CORS, upload limits are "at or below" the binary limit, and the middleware is in the end-to-end flow.
- Existing test adapted: `test_a_response_already_started_when_the_limit_is_passed_reraises` asserted the exception name `"_BodyTooLarge"`; changed to `"BodyTooLarge"` (rename only). Header injection broke no test.
- `uv run just check`: green. Status set to `testing`.

## Test coverage

All tests are in `apps/api/tests/unit/test_body_size_limit_http.py` (http layer, 31 test
functions, 52 cases with parametrization; one is an xfail). Domain, application and integration
do not apply (plan table). One GAP (repair round 1, L1, raw-stream routes), see the matrix.

| Behavior | Source | Layer | Test | State |
| -------- | ------ | ----- | ---- | ----- |
| JSON, `+json`, form, multipart, text, no Content-Type use `max_body_bytes` | `middleware.py:26-33,57` | http | `test_text_like_media_types_get_the_body_limit` | CONFIRMED |
| Other media types (`image/jpeg`, octet-stream, xlsx) use `max_upload_bytes` | `middleware.py:57` | http | `test_binary_media_types_get_the_larger_upload_limit`, `test_the_xlsx_media_type_is_binary` | CONFIRMED |
| A body exactly at the limit passes (stream and declared) | `middleware.py:60,70` | http | `test_a_body_exactly_at_the_limit_passes`, `test_a_declared_length_at_the_limit_passes` | CONFIRMED |
| Declared `Content-Length` over the limit: 413 without calling the app or reading the body | `middleware.py:60-62` | http | `test_a_declared_length_over_the_limit_is_refused_without_reading_the_body` | CONFIRMED |
| Malformed `Content-Length` falls back to counting the stream | `middleware.py:60` | http | `test_a_malformed_content_length_falls_back_to_counting_the_stream` | CONFIRMED |
| Chunked body cut off once past the limit; under the limit passes whole | `middleware.py:67-75` | http | `test_a_chunked_body_is_cut_off_once_it_passes_the_limit`, `test_a_chunked_body_under_the_limit_passes_in_full` | CONFIRMED |
| 413 body is `REQUEST_TOO_LARGE` with `details.max_bytes` | `middleware.py:98-105` | http | `test_a_declared_length_over...`, `test_an_oversized_json_body_gets_413_with_the_error_shape` | CONFIRMED |
| 413 carries `X-Request-ID` (echoed or generated) (deviation 1) | `middleware.py:100-104` | http | `test_the_refusal_carries_a_request_id...`, `test_the_refusal_generates_a_request_id...`, `test_the_413_echoes_the_request_id` | CONFIRMED |
| App answer after the limit was passed is replaced by the 413, rest dropped (deviation 2) | `middleware.py:77-90` | http | `test_exceeding_the_limit_replaces_an_error_the_app_answers_itself`, `test_the_app_messages_after_the_replacement_are_dropped`, `test_a_chunked_json_body_gets_413_not_the_frameworks_400` | CONFIRMED |
| Response already started when the limit is passed: re-raise | `middleware.py:91-95` | http | `test_a_response_already_started_when_the_limit_is_passed_reraises` | CONFIRMED |
| App that never reads the body is not refused by the stream count | `middleware.py:67-75` | http | `test_an_app_that_never_reads_the_body_is_not_refused_by_the_stream_count` | CONFIRMED |
| Non-http scopes pass through untouched | `middleware.py:50-52` | http | `test_non_http_scopes_pass_through_untouched` | CONFIRMED |
| Oversized body to an anonymous admin route gets 413, not 401 (criterion 2) | `main/http.py:73-76` | http | `test_an_oversized_body_to_an_admin_route_without_a_session_is_413_not_401` | CONFIRMED |
| Normal bodies behave as before (criterion 3) | `main/http.py` | http | `test_a_normal_body_behaves_as_before`, `test_get_requests_without_a_body_are_unaffected` | CONFIRMED |
| Binary body between the two limits reaches the app; over the upload limit gets 413 | `middleware.py:57` | http | `test_a_binary_body_over_the_json_limit_but_under...`, `test_a_binary_body_over_the_upload_limit_gets_413` | CONFIRMED |
| `build_app` defaults are 1 MiB / 10 MiB | `main/http.py:55-56` | http | `test_build_app_defaults_are_1_mib_and_10_mib` | CONFIRMED |
| `create_app` wires `request_max_bytes` and `upload_max_bytes` from settings; 413 on the real `/auth/login` route | `main/http.py:46-47` | http | `test_create_app_wires_the_limits_from_settings` | CONFIRMED |
| Settings defaults, `upload_max_bytes = max(photo, import)`, `ge=1` | `config.py:87,106-108` | http (settings) | `test_settings_defaults_and_derived_upload_limit`, `test_settings_reject_a_non_positive_request_limit` | CONFIRMED |
| Repair round 1, L1: chunked oversized body to a raw-stream route logs no `request.unhandled_error` | `middleware.py:112-119` | http | `test_a_chunked_oversized_body_to_a_raw_route_logs_no_unhandled_error` | **GAP** (xfail strict): `BaseHTTPMiddleware` delivers `BodyTooLarge` wrapped in an `ExceptionGroup`, so `except BodyTooLarge` in `RequestContextMiddleware.dispatch` never matches and the error is still logged (client still gets 413). Probe confirmed. Needs a product fix (e.g. `except* BodyTooLarge` or unwrap the group) |
| Repair round 1: the 413's `X-Request-ID` equals the id the app saw when the client sent none | `middleware.py:60-69,97-99` | http | `test_the_413_request_id_is_the_one_the_app_saw_when_the_client_sent_none` | CONFIRMED |
| Repair round 1: a client-sent `X-Request-ID` is kept (app sees it, 413 echoes it) | `middleware.py:60-69` | http | `test_a_client_sent_request_id_is_kept_through_the_cut_off` | CONFIRMED |
| 2 MiB JSON to real login both ways; 12 MiB photo 413; 6 MiB photo and 4/6 MiB xlsx reach the real routes (criteria 1, 4, 5) | photo and import routers | e2e-ish | not unit-testable without real routes and multipart bodies at that size | NOT CONFIRMED here: left to the verify phase against the running app |

## Review findings

Reviewed 2026-10-12 on `fix/open-findings` against `main` (commits 6396b30..6bf0244 for this
plan, plus the two fast-lane commits 9462e59 and 1331836 noted at the end).

**Checklist: 15/15 passed** (items that do not apply to this diff count as passed).

- [x] `plans-scope`. The command exits 1 and lists 4 files outside the plan. All 4 come from the
  fast-lane commits 9462e59 and 1331836 (`git diff --stat 1331836 HEAD` shows only this plan's
  commits). This plan's own diff touches exactly the 7 declared files plus allowed plan files
  (its finding, the initiative README, this plan). No hot files changed.
- [x] `uv run just check` is green: 1865 unit tests and 685 harness tests pass, and the exit
  status is 0.
- [x] `test-integration`: not required, since this plan changes no infrastructure. It was run
  anyway for the fast-lane commit 1331836: 477 passed, 5 skipped.
- [x] Business rules: none live in routers. The limit is platform HTTP code in `shared/http`.
- [x] CQRS-lite: not applicable.
- [x] Contracts: no contract models changed. `openapi.json` differs only in the description and
  was regenerated, which matches `main/http.py:21-26`.
- [x] Errors: `REQUEST_TOO_LARGE` uses the shared `error_response` shape. It does not follow the
  `<MODULE>_` code pattern, which is correct for a platform-level code (like
  `VALIDATION_ERROR`).
- [x] Money, time and ids: not applicable. The uuid4 hex request id copies
  `RequestContextMiddleware`.
- [x] Migrations: none.
- [x] Routes: none added.
- [x] Wiring: `create_app` passes both settings (`main/http.py:46-47`). The settings test is
  green.
- [x] No secrets: `.env.example` gains only a commented default.
- [x] Deviations: spot-checked deviation 2 with a scratch probe through `build_app`. A chunked
  `application/json` body over the limit gets 413, not FastAPI's 400. The claim holds.
- [x] Docs: `docs/architecture.md` was updated (see nit N1).
- [x] PR body: no PR exists yet, so nothing to check. The PR must carry the reverse-proxy
  `client_max_body_size` risk that "Out of scope" promises.

**Bug hunt.** I traced the middleware against the Starlette stack. Order: `ServerErrorMiddleware`
→ CORS (when configured) → `BodySizeLimitMiddleware` → `RequestContextMiddleware` →
`ExceptionMiddleware` → router. Results:

- **Media types.** The classification matches the plan. It is case-insensitive and ignores
  parameters, and only the first `content-type` header counts, as in Starlette and FastAPI.
- **Declared path.** It refuses without calling the app. A malformed `Content-Length` falls back
  to counting the stream, and the ASGI server enforces the length framing.
- **Chunked path.** It counts only `http.request` messages, so a body cannot get through
  unbounded.
- **Replacement path.** When the app answers after the limit was passed, the middleware
  replaces the answer with the 413 and drops the app's remaining messages. The re-raise path for
  an already-started response is correct.
- **Raw-body routes.** Photo (`perfume_router.py:259-275`) and import (`import_router.py:79-96`)
  are unaffected: their media types are binary, so the 10 MiB limit applies, which is at or above
  their own limits.
- **CORS.** CORS stays the outer layer, so a browser can read the 413.

Scratch probe (scratchpad, `build_app`, limits 100 and 1000 bytes, 30 chunks of 50 bytes):

| Request | Result |
| --- | --- |
| raw `image/jpeg` route | 413, `max_bytes` 1000 |
| JSON route, `application/json` | 413, `max_bytes` 100 |
| JSON route, `application/octet-stream` | 413, `max_bytes` 1000 |

### High / Medium

None.

### Low (non-blocking; no code change needed to accept the plan)

- **L1. A chunked body over the limit on a raw-stream route is logged as an unhandled error, and
  its request id does not match the log.**
  - Where: `shared/http/middleware.py:67-75` together with `:113-120`.
  - What happens:
    - `_BodyTooLarge` is raised inside `request.stream()` in the photo or import route.
    - No exception handler matches it, so `RequestContextMiddleware` catches it. It calls
      `log.exception("request.unhandled_error")`, with a traceback at error level, and builds a
      500.
    - The 500 is then replaced with the 413. The client gets the right answer, but the logs
      show a 500-class error.
    - When the client sent no `X-Request-ID`, the 413's id comes from `_refuse` (a new uuid4).
      It differs from the `request_id` bound in that log line, so the two cannot be correlated.
  - Confirmed by the probe: `unhandled_logs=1` for the raw route, and 0 for the JSON routes,
    where FastAPI swallows the error into a 400.
  - Failure scenario: an admin client uploads a 12 MiB photo with `Transfer-Encoding: chunked`.
    The client correctly gets 413, but an error-level traceback is logged. It would page if
    error logs alert.
  - Reach: only after the admin checks. A browser `fetch` with a `File` declares
    `Content-Length` and takes the declared path, so this case is rare.
  - Possible fix, for a later plan or a fast lane: have `RequestContextMiddleware` re-raise or
    ignore `_BodyTooLarge` instead of logging it, and reuse the request id it bound.

### Info

- **I1. The client chooses which limit applies.**
  - An anonymous client can label a body `application/octet-stream` and send it to a JSON route
    such as `/auth/login`. It then gets the 10 MiB budget, and FastAPI reads the whole body
    (`request.body()`) before answering 422 (probe: a 750 B octet body to the JSON route gave
    422).
  - The body is bounded, so the finding's "unbounded" risk is closed. This is the documented
    trade-off of README decision 7, not a defect.
- **I2. Future multipart uploads.** `multipart/form-data` falls under the 1 MiB limit. No route
  uses multipart today. A future multipart upload route would be refused above 1 MiB unless the
  classification changes.

### Nits

- **N1. Wording in `docs/architecture.md:105-109`.**
  - "Outermost layer" is not exact when `CORS_ORIGINS` is set, because CORS sits outside it.
  - "The upload routes keep their own smaller limits": the photo limit equals the upload limit
    (10 MiB), so it is not smaller.
  - The "A request, end to end" flow (`:130-133`) still starts at `RequestContextMiddleware`.
  - None of these changes behavior.

### Fast-lane commits (sanity look, not part of this plan)

- **9462e59: the format hook sorts imports only.** It is correct.
  - `ruff check --fix --select I` only sorts imports, and `ruff format` runs after it.
  - `just check` still enforces every rule.
  - The regression test `test_format_file_keeps_imports_not_used_yet_and_only_sorts_them`
    proves that unused imports survive and are sorted. The older formatting test was adjusted
    because it relied on the removed autofix (dropping `import os`).
  - The harness suite is green.
- **1331836: import confirm in identity order.** It is correct.
  - The sort key `(str(brand_id), slugify(name), str(concentration_id))` matches the unique
    constraint `uq_perfumes_identity` (`tables.py:84`), whose `name_slug` is `Perfume.name.slug`.
  - It also matches how existing perfumes are found (`sql_import_references.py:75`). So the same
    perfume gets the same key in every import, whether the import creates it or updates it, and
    the lock order is global.
  - The unit test was updated, and the integration regression test passes; the full integration
    suite passed as well.

**Result:** no finding requires a code change, so the status is set to `verify`. The verify
phase still has to cover the acceptance criteria marked NOT CONFIRMED in "Test coverage":

- the real login, chunked and declared;
- 12 MiB and 6 MiB photos;
- 4 MiB and 6 MiB `.xlsx` uploads.

## Verification
