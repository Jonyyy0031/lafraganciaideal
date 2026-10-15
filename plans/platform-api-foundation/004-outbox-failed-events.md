---
status: approved
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

## Test coverage

## Review findings

## Verification
