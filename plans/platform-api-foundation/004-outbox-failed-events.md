---
status: review
module: platform
min_implementer: mid
depends_on: ["001"]
---

# 004 — Failed outbox events: a retry cap, a panel list and a manual retry

## Context

**The outbox relay retries a failing event forever.**
- `OutboxRelay._record_failure` increments `attempts` and sets `available_at` to now plus
  `min(2**attempts s, MAX_BACKOFF)`. `MAX_BACKOFF` is 5 minutes
  (`apps/api/src/fragancia_api/shared/infrastructure/outbox.py:38`, `:132-151`).
- The relay picks any row with `published_at IS NULL` and `available_at <= now()`
  (`:103-128`).
- There is no cap and no failed state, and nobody is told (finding
  `platform-outbox-relay-retries-forever`).
- A subscriber with partial side effects repeats them every 5 minutes. A broken event stays
  pending, unseen.

This plan applies README decision 8 (2026-10-15):
- retry for about 24 hours;
- then mark the event failed, keep it with its error, and log it;
- owners can list failed events in the panel and retry one by hand.

**What exists today:**
- The outbox table and its partial index `ix_outbox_pending` (`outbox.py:40-53`;
  `apps/api/migrations/versions/0001_platform_outbox.py:37-43`).
- Shared ports live in `shared/application/events.py:10-33`, and adapters in
  `shared/infrastructure/`.
- The composition root registers platform services itself: `services.add(HealthChecks, …)` at
  `apps/api/src/fragancia_api/container.py:73-75`, with the relay at `:86`.
- The health router is a platform router mounted by `main/http.py:94`, with its models
  defined beside it (`shared/http/health.py:14-27`).
- Admin routers need a permission dependency (`shared/http/access.py:43-49`).
- Permissions are atomic strings in `modules/identity/domain/user.py:80-87`. Owner and staff
  each have their own set.
- `Page[T]` and the paging bounds live in `shared/contracts/__init__.py`.

**Approach.**
- **A new nullable `failed_at` column.** The relay skips rows that have it.
- **The cap is a number of attempts, `MAX_ATTEMPTS = 295`.** With the current backoff (2 s,
  4 s … 256 s, then 5 minutes), that is about 24 hours of retries:
  - the first 8 waits add up to 510 s;
  - then 286 waits of 300 s bring the total to about 86,310 s, just under 24 h.
- **Why attempts and not age?** Counting attempts lets a manual retry restart the window:
  setting `attempts` back to 0 is enough. An age measured from `occurred_at` would fail a
  retried event at once.
- **The panel side.** A small platform port, `OutboxAdmin`, has `failed(page, size)` and
  `retry(id)`. It is implemented in SQL beside the relay and served by a platform admin
  router, the way the health router is.
- **A new owner-only permission, `outbox:manage`.** It follows decision 11 of inventory-stock
  (atomic permissions per area).
- **Rejected:**
  - a separate dead-letter table, which means moving rows, and retrying is then a copy back;
  - an email alert, which the user did not choose, and which fails exactly when email is the
    broken part.

## Out of scope

- **Retrying every failed event at once, or deleting failed events.**
- **Per-handler idempotency for partial side effects.** The low-stock email already skips
  permanently rejected recipients (inventory-stock decision 36).
- **Changing the backoff curve or the relay's ordering.**
- **The web panel screens** (phase 3). This plan adds the API only.

## Dependencies

- **001 (done):** the outbox, the relay, the worker cron, `admin_router` and
  `require_permission`.

## Steps

1. **Contracts and the port**
   - Files: `apps/api/src/fragancia_api/shared/contracts/outbox.py` (create),
     `apps/api/src/fragancia_api/shared/application/events.py` (modify)
   - Do:
     - **Contracts** (`contracts/outbox.py`), on Pydantic:
       - `FailedEvent`: `id`, `name`, `occurred_at`, `failed_at`, `attempts`, `last_error`;
       - `FailedEventPage(Page[FailedEvent])`.
     - **The port** (`events.py`):
       - `OutboxAdmin(Protocol)`, with `failed(*, page, size) -> FailedEventPage` (newest
         `failed_at` first, ties by id) and `retry(event_id: UUID) -> bool`;
       - `retry` returns False when no failed event has that id;
       - the error `OutboxEventNotFound(NotFoundError)`, with code `OUTBOX_EVENT_NOT_FOUND` and
         message "No failed event with this id".
   - Observable result: `uv run just typecheck` and `uv run just arch` are green.

2. **The cap in the relay, the column and the migration**
   - Files: `apps/api/src/fragancia_api/shared/infrastructure/outbox.py` (modify),
     `apps/api/migrations/versions/0016_platform_outbox_failed_at.py` (create)
   - Do:
     - **Table:**
       - `outbox` gains `failed_at` DateTime(tz), nullable;
       - `ix_outbox_pending` becomes partial on `published_at IS NULL AND failed_at IS NULL`.
     - **`MAX_ATTEMPTS = 295`**, with a comment showing the arithmetic (about 24 h with
       `MAX_BACKOFF`).
     - **`_relay_one`** also requires `failed_at IS NULL`.
     - **`_record_failure`.** When `attempts >= MAX_ATTEMPTS`:
       - set `failed_at = now`, together with `attempts` and `last_error`;
       - log at error level `outbox.delivery_failed_permanently`, with the event id, name,
         attempts and error.

       Otherwise it behaves as today.
     - **`SqlOutboxAdmin(database)`:**
       - `failed`: a select and a count of the rows with `failed_at IS NOT NULL`, through
         `database.reader()`;
       - `retry`: in a unit of work, `UPDATE … SET failed_at = NULL, attempts = 0, available_at
         = now() WHERE id = :id AND failed_at IS NOT NULL`, returning whether a row matched.
         It keeps `last_error` for context.
     - **Migration.** Run `uv run just db-revision "platform outbox failed at"`; it becomes
       `0016`, with `down_revision "0015"`.
       - It adds the column, then drops and re-creates the index with the new predicate.
       - The downgrade restores the old index and drops the column.
   - Observable result: an integration test with a handler that always fails shows:
     - after `MAX_ATTEMPTS` the row has `failed_at` set and the relay no longer picks it;
     - before that it is still retried.

3. **Permission, router and wiring**
   - Files: `apps/api/src/fragancia_api/modules/identity/domain/user.py` (modify),
     `apps/api/src/fragancia_api/shared/http/outbox.py` (create),
     `apps/api/src/fragancia_api/main/http.py` (modify),
     `apps/api/src/fragancia_api/container.py` (modify),
     `apps/api/openapi.json` (modify)
   - Do:
     - **Permission** (`user.py`): `OUTBOX_MANAGE = "outbox:manage"`, in `ROLE_PERMISSIONS`
       for `Role.OWNER` only.
     - **Router** (`shared/http/outbox.py`): `admin_router(prefix="/outbox", tags=["platform ·
       outbox"], dependencies=[Depends(require_permission("outbox:manage"))])`. Put a comment
       on the literal: permissions are identity's strings, and modules may not import each
       other.
       - `GET /failed`, with `page` and `size` bounded as in `modules/inventory/http/router.py`
         (`MAX_PAGE`, `MAX_PAGE_SIZE`, `DEFAULT_PAGE_SIZE`), returns `FailedEventPage`.
       - `POST /{event_id}/retry`, status 204, answers 404 `OUTBOX_EVENT_NOT_FOUND` when
         `retry` returns False.
       - Docstrings describe both, including that a retry restarts the about-24-hour window.
     - **`main/http.py`** includes `outbox.router` next to `health.router`.
     - **`container.py`** adds `services.add(OutboxAdmin, SqlOutboxAdmin(database))`.
     - Run `uv run just openapi`.
   - Observable result:
     - an owner gets 200 on `GET /api/v1/admin/outbox/failed`;
     - staff gets 403;
     - a retry of a failed event answers 204, and the relay delivers it again.

4. **Docs and tests**
   - Files: `docs/adr/0006-outbox-from-day-one.md` (modify), `docs/architecture.md` (modify),
     `plans/findings/platform-outbox-relay-retries-forever.md` (modify),
     `apps/api/tests/unit/` (modify), `apps/api/tests/integration/` (modify)
   - Do:
     - **ADR 0006, "Consequences":** add one line. Retries stop after about 24 h (295
       attempts), the event is marked failed and kept, and owners can list and retry it.
     - **`docs/architecture.md`:** under "Events, outbox and jobs", replace the retry sentence
       accordingly.
     - **The finding:** set `status: resolved` and `plan: platform-api-foundation/004`, and add
       a resolution line.
     - **Tests:** existing tests that pin `ROLE_PERMISSIONS` (`tests/unit/identity/test_user_domain.py`)
       get the new permission. The tester writes the layered tests.
   - Observable result: `uv run just check` and `uv run just test-integration` are green.

## Acceptance criteria

- [ ] **Cap.** A subscriber that always raises makes its event fail permanently after 295
  attempts:
  - `failed_at` is set;
  - the worker log has `outbox.delivery_failed_permanently`;
  - the relay never picks it again.

  To check it live, the verifier sets the row's `attempts` to 294 and makes it due, rather
  than waiting 24 h.
- [ ] **List.**
  - An owner's `GET /api/v1/admin/outbox/failed` lists it with its name, attempts, last error
    and `failed_at`.
  - Staff gets 403 `FORBIDDEN`.
- [ ] **Retry.**
  - `POST /api/v1/admin/outbox/{id}/retry` answers 204. The row has `failed_at` null and
    `attempts` 0, and the worker relays it again.
  - Once the cause is fixed, it is published.
  - Retrying an unknown or non-failed id answers 404 `OUTBOX_EVENT_NOT_FOUND`.
- [ ] **No regression.** Normal events relay as before. A transient failure still backs off and
  succeeds later.
- [ ] **Migration.** `db-migrate` and its round trip run clean, and existing rows keep
  `failed_at` null.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `ROLE_PERMISSIONS`: `outbox:manage` for owner only |
| application | no      | The port has no logic of its own |
| http        | yes     | Both routes: owner 200/204, staff 403, unauthenticated 401, 404 on retry, paging bounds, OpenAPI |
| integration | yes     | The relay cap (attempts 294 → failed, before it retried), failed rows skipped, `SqlOutboxAdmin.failed` order and total, `retry` resets and only matches failed rows, a retried event delivered by the relay, migration round trip and index predicate |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

- Cosmetic: adding `outbox:manage` to the owner role also broke other existing tests that pin the
  owner permission list or the admin route list (`test_auth_http.py`, `test_resolve_session_actor.py`,
  `test_sessions.py`, `test_http_platform.py`, `integration/identity/test_sql_identity.py`). Updated
  their expected values only; no new tests (the tester writes those).
- Cosmetic: the generated migration was hand-edited to also re-create the partial index, as the
  plan specifies.

### Repair round 1 (2026-10-15, main session: review → implementing)

**Why.** The tester's GAP (finding `platform-outbox-backoff-overflows-before-the-cap`):
- `_record_failure` builds `timedelta(seconds=2**attempts)` before applying `MAX_BACKOFF`
  (`shared/infrastructure/outbox.py:159`).
- From about attempt 47 that raises `OverflowError`. The failure is never recorded, and the
  relay raises on every cycle.
- So an always-failing event never reaches the 295-attempt cap, which defeats decision 8.
- This is an implementation defect, not a business rule, so no user decision is needed.

**Repair spec.**
- Files: `apps/api/src/fragancia_api/shared/infrastructure/outbox.py`,
  `plans/findings/platform-outbox-backoff-overflows-before-the-cap.md`.
- **Cap the exponent before building the delta.** Write
  `delay = min(timedelta(seconds=2 ** min(attempts, _BACKOFF_EXPONENT_CAP)), MAX_BACKOFF)`,
  with `_BACKOFF_EXPONENT_CAP = 9`, the first exponent whose value (512 s) exceeds
  `MAX_BACKOFF`. Add a one-line comment.
- The waits are unchanged for every attempt: 2, 4 … 256 s, then 300 s.
- The finding gets `status: resolved`, `plan: platform-api-foundation/004`, and a resolution
  line.
- **Tests, for the tester.** The two strict-xfail tests become XPASS failures. Remove their
  markers and keep them as normal tests.

**Scope.** The product code is that single expression. The implementer changes no tests.

## Test coverage

Baseline (before the tests): `just check` green (2455 unit passed, 3 skipped), `just
test-integration` 697 passed, 5 skipped. Closing: `just check` 2470 unit passed, 3 skipped, 685
harness passed; `just test-integration` 714 passed, 5 skipped, 2 xfailed. `fragancia_test` has 0
outbox rows afterwards and no `fragancia_mig_*` database is left.

| Behavior | Source | Layer | Test | State |
| -------- | ------ | ----- | ---- | ----- |
| `outbox:manage` for owner only | `identity/domain/user.py:83-87` | domain | `unit/identity/test_user_domain.py` (owner and staff sets, updated in the implementation commit) | CONFIRMED |
| The router literal equals identity's permission | `shared/http/outbox.py:14` | http | `unit/test_outbox_http.py::test_the_router_permission_literal_is_the_one_identity_grants_to_owners` | CONFIRMED |
| Owner lists failed events (200, shape, default paging) | `shared/http/outbox.py:25-32` | http | `test_outbox_http.py::test_an_owner_lists_failed_events` | CONFIRMED |
| Paging parameters reach the port; bounds 422 (page 0 / above `MAX_PAGE`, size 0 / above `MAX_PAGE_SIZE`, non-numeric) | `shared/http/outbox.py:28-29` | http | `test_paging_parameters_reach_the_port`, `test_out_of_bounds_paging_is_a_validation_error` | CONFIRMED |
| Owner retry answers 204; unknown or non-failed id 404 `OUTBOX_EVENT_NOT_FOUND`; malformed id 422 | `shared/http/outbox.py:35-42`, `application/events.py:37-40` | http | `test_an_owner_retries_a_failed_event`, `test_retrying_an_unknown_or_not_failed_event_is_404`, `test_a_malformed_event_id_is_a_validation_error` | CONFIRMED |
| Staff 403 `FORBIDDEN`, unauthenticated or unknown session 401, port never called | `shared/http/access.py:43-49` | http | `test_staff_is_forbidden_on_both_routes`, `test_unauthenticated_requests_are_401_on_both_routes` | CONFIRMED |
| OpenAPI: both operations, `FailedEventPage`, 204/401/403/404, cookie security, `FailedEvent` fields | `openapi.json` | http | `test_the_openapi_document_describes_both_protected_operations`, `test_the_outbox_routes_declare_admin_access`; `test_http_platform.py::test_every_admin_route_declares_admin_access` (existing, lists both routes) | CONFIRMED |
| Below the cap: failure backs off, not failed, still retried | `outbox.py:158-167` | integration | `integration/test_outbox_failed_events.py::test_an_event_still_below_the_cap_is_retried_with_backoff` (attempts 5) | CONFIRMED |
| The attempt that reaches 295 sets `failed_at`, keeps the error, logs `outbox.delivery_failed_permanently` (error level, id, name, attempts, error) | `outbox.py:149-157` | integration | `test_the_attempt_that_reaches_the_cap_marks_the_event_failed_and_logs_it` (seeded at 294) | CONFIRMED |
| A failed event is never picked again, even when due; a failed row does not block later due rows | `outbox.py:119-121` | integration | `test_a_failed_event_is_never_picked_again_even_when_due`, `test_a_failed_row_does_not_block_the_due_rows_after_it` | CONFIRMED |
| Transient failure still succeeds later | `outbox.py:158-170` | integration | `test_a_transient_failure_still_succeeds_later_and_never_fails` | CONFIRMED |
| `SqlOutboxAdmin.failed`: only failed rows, newest `failed_at` first, ties by id, fields, total | `outbox.py:177-200` | integration | `test_failed_lists_only_failed_rows_newest_failure_first_ties_by_id`, `test_failed_is_empty_when_nothing_failed` | CONFIRMED |
| `failed` paging slices and keeps the total | `outbox.py:195-196` | integration | `test_failed_pages_slice_the_list_and_keep_the_total` | CONFIRMED |
| `retry` resets `failed_at`/`attempts`, makes it due, keeps `last_error`; only matches failed rows; second retry False | `outbox.py:202-212` | integration | `test_retry_resets_the_window_and_keeps_the_last_error`, `test_retry_only_matches_failed_rows`, `test_a_second_retry_of_the_same_event_does_not_match` | CONFIRMED |
| A retried event is delivered by the relay once fixed; a retried event restarts at attempts 1 | `outbox.py:202-212` | integration | `test_a_retried_event_is_delivered_by_the_relay_once_the_cause_is_fixed`, `test_a_retried_event_gets_a_fresh_window_and_fails_again_only_at_the_cap` | CONFIRMED |
| Migration 0016: nullable timestamptz column, index predicate `published_at IS NULL AND failed_at IS NULL`, existing rows keep `failed_at` null, downgrade restores the old predicate and drops the column, round trip (scratch database) | `migrations/versions/0016_platform_outbox_failed_at.py` | integration | `integration/test_migration_0016_outbox_failed_at.py` (3 tests) | CONFIRMED |
| Retries reach the cap from attempt 0 (an always-failing subscriber fails the event at exactly 295) | plan step 2, acceptance "Cap" | integration | `test_an_always_failing_subscriber_fails_the_event_after_exactly_max_attempts` | GAP |
| A failure recorded far below the cap (attempts 60) still backs off at 5 minutes | `outbox.py:159` | integration | `test_a_failure_recorded_far_below_the_cap_still_backs_off_at_the_maximum` | GAP |
| Application layer | n/a | application | not required (the port has no logic) | n/a |

**GAP (product bug, tests do not fix it).** `_record_failure` builds
`timedelta(seconds=2**attempts)` before applying `MAX_BACKOFF`, which raises `OverflowError`
from about attempt 47. The failure is then not recorded, so a subscriber that always fails
never reaches attempt 295 by retrying; the row keeps raising in the relay. The attempt-294 live
check in the acceptance criteria would pass and hide it. Both tests are strict `xfail`
(`raises=OverflowError`) and become XPASS failures when it is fixed. Finding:
`plans/findings/platform-outbox-backoff-overflows-before-the-cap.md`.

**Repair round 1 (main session, tester role, 2026-10-15).**
- The fix capped the exponent (`_BACKOFF_EXPONENT_CAP = 9`).
- Both strict-xfail tests then reported XPASS. Their markers and `GAP_REASON` were removed, and
  they are normal regression tests now:
  - `test_a_failure_recorded_far_below_the_cap_still_backs_off_at_the_maximum`;
  - `test_an_always_failing_subscriber_fails_the_event_after_exactly_max_attempts`. Its log
    shows `outbox.delivery_failed_permanently` at attempts 295.
- `uv run just check` is green: 2470 unit tests and 685 harness tests.
- `uv run just test-integration`: 716 passed, 5 skipped. That is the 714 earlier plus these 2,
  and no xfail is left.

## Review findings

## Verification
