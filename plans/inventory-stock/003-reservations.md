---
status: testing
module: inventory
min_implementer: high
depends_on: ["001", "002"]
---

# 003 — Reservations

## Context

Inventory keeps one `StockItem` per in-stock presentation, with `on_hand` and an append-only
ledger (`apps/api/src/fragancia_api/modules/inventory/domain/stock.py:142-223`).
- **Movements.** `record_movement` refuses only a negative `on_hand` (`:164-200`).
- **Low stock.** "Low" is `is_low(on_hand, override, global)` (`:124-126`).
- **The event.** `StockChanged.available` is documented as "on hand for now" (`:129-139`;
  `modules/inventory/__init__.py:6-9`). The catalog keeps a copy of it for the storefront
  (plan 002).
- **Lock order.** The commands take the catalog presentation (share), the settings row (share),
  then the stock item, with items in `presentation_id` order
  (`modules/inventory/application/commands/stock.py:44-97`, `:100-170`, `:224-262`).
  `StockRepository.get_for_update` inserts the empty item first, so the row always exists to
  lock (`modules/inventory/infrastructure/sql_stock_repository.py:31-57`).
- **The panel** shows `on_hand`, the thresholds and `is_low` (`modules/inventory/contracts.py:27-37`,
  `:56-67`; `application/queries/stock.py:19-96`; `http/router.py:75-117`).

Checkout (the orders initiative, not built yet) needs to hold units while a customer pays, so
the last bottle is never sold twice. This plan applies README decisions 3–5 and 24–29.
- A **reservation** holds units of one or more presentations until its expiry. It is all or
  nothing.
- An approved payment **commits** it: a `sale` movement per presentation lowers `on_hand`.
- A rejected payment **releases** it, and a job **expires** it at its expiry.
- The caller (orders) gives the expiry and may **extend** an active reservation.
- **Available** becomes `max(on_hand − reserved, 0)`. The storefront, the low-stock flag and
  the panel use it.

**Approach.** A `Reservation` aggregate with its lines, plus a `reserved` counter on each
stock item, changed under the item's row lock.
- **Why a counter rather than a sum of the active lines at read time:** availability is then
  one row, read under the same lock that guards `on_hand`, so a reserve and a movement on one
  presentation serialize.
- Every reservation operation keeps the existing lock order. A reservation operation that
  starts from a reservation (extend, commit, release, expire) locks the reservation row first.
  Only those operations lock reservation rows, so no wait cycle forms.
- **Other modules** call it through a facade, `StockReservations`, exported from the
  module's `__init__.py`. This is the same pattern as the catalog's `PresentationDirectory`
  (`modules/catalog/application/queries/presentation_directory.py:30-48`;
  `modules/catalog/__init__.py`).
- **The expiry job** is a `ScheduledJob`, as the catalog's import jobs are
  (`modules/catalog/module.py:343-361`; `shared/module.py:31-44`).

**Rejected:**
- **Computing `reserved` from the lines on every read.** It needs a lock on every line of the
  presentation to be safe, and an aggregate subquery in each stock read.
- **Reserving by decrementing `on_hand`.** It mixes "in the warehouse" with "promised", breaks
  physical counts (decision 5), and a release would need a fake movement.

**Files imitated:**
- the commands follow `RecordMovement` (`commands/stock.py:44-97`);
- the facade follows `PresentationDirectory`;
- the job follows `ExpireCatalogImports` (`modules/catalog/application/commands/catalog_imports.py`,
  class at `:226`).

## Out of scope

- **The orders module:** carts, checkout, order states, which payment method sets which expiry
  (decision 4), and what happens to an order whose commit is refused (decision 25). Orders
  calls the facade later.
- **Whether a presentation can be sold** (active, perfume published, brand active). The cart
  checks it; inventory only counts units.
- **Partial commits or releases** of some lines of a reservation.
- **HTTP endpoints to reserve, commit or release.** Only orders will call these, in process.
  The panel only reads.
- **The low-stock email** (plan 004). It will use the `available` this plan defines.

## Dependencies

- **001 (done):** `StockItem`, the ledger, the repositories, `PresentationCatalog` and
  `_require_stocked` (`commands/stock.py:33-41`).
- **002 (done):** `StockChanged` published on every change. The catalog's copy treats
  `available` as "can be bought", so this plan only changes the value inventory sends.
- **Platform:**
  - `ScheduledJob` and `AppModule.jobs` (`shared/module.py:31-55`);
  - `EventPublisher`;
  - `TransactionRunner`, which joins an active transaction (`shared/infrastructure/database.py:64-65`).

## Steps

1. **Domain: available and reserved on the stock item; the `sale` movement**
   - Files: `apps/api/src/fragancia_api/modules/inventory/domain/stock.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/domain/errors.py` (modify)
   - Do:
     - **`MovementKind.SALE = "sale"`.**
       - `_quantity_valid` accepts 1..`QUANTITY_MAX` for it.
       - `record_movement` refuses `SALE` with `StockQuantityInvalid`: a sale only comes from
         a commit.
       - Update the module docstring table.
     - **`StockItem` gains `reserved: int`** (keyword, default 0 in `empty`) and the property
       `available = max(on_hand − reserved, 0)`.
       - `is_low` and `changed_event` use `available`.
       - `StockChanged.available` is now `available`. Fix its docstring and the module
         docstring: "A presentation is low when `available <= effective threshold`".
     - **New methods on `StockItem`:**
       - `hold(quantity) -> Result[None, StockUnavailable]`: refuses when
         `quantity > available`; otherwise `reserved += quantity`.
       - `unhold(quantity) -> None`: `reserved -= quantity`. A result below 0 is a programming
         error: raise `ValueError`.
       - `sell(quantity, reason, actor_id, now, *, held: bool) -> Result[Movement,
         StockInsufficient | StockUnavailable]`:
         - When held, the units are this reservation's, so only `on_hand >= quantity` is
           required; otherwise `StockInsufficient`. Then `reserved -= quantity`.
         - When not held (late commit), require `available >= quantity`; otherwise
           `StockUnavailable`.
         - In both cases, append a `SALE` movement with `delta = −quantity` and lower
           `on_hand`.
     - **Errors** (`errors.py`):
       - `StockUnavailable(ConflictError)`, code `INVENTORY_STOCK_UNAVAILABLE`, message "Not
         enough units are available". Its `details` are `{"lines": [{"presentation_id",
         "requested", "available"}]}`, set by the command.
       - `ReservationNotFound(NotFoundError)`, `INVENTORY_RESERVATION_NOT_FOUND`.
       - `ReservationReferenceTaken(ConflictError)`, `INVENTORY_RESERVATION_REFERENCE_TAKEN`.
       - `ReservationInvalid(InvalidValueError)`, `INVENTORY_RESERVATION_INVALID`. It covers
         no lines, a reference outside 1..64 characters, or a quantity outside
         1..`QUANTITY_MAX`.
       - `ReservationExpiryInvalid(InvalidValueError)`,
         `INVENTORY_RESERVATION_EXPIRY_INVALID`: an expiry not in the future, or an extension
         that is not later.
       - `ReservationNotActive(ConflictError)`, `INVENTORY_RESERVATION_NOT_ACTIVE`: extending
         a reservation that is not active, or is past its expiry.
       - `ReservationCommitted(ConflictError)`, `INVENTORY_RESERVATION_COMMITTED`: releasing a
         committed one.
   - Observable result: unit tests show a loss below `reserved` is recorded and `available`
     is 0; `hold` refuses beyond `available`; a held sale needs only `on_hand`.

2. **Domain: the `Reservation` aggregate**
   - Files: `apps/api/src/fragancia_api/modules/inventory/domain/reservation.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/domain/repositories.py` (modify)
   - Do:
     - **Types.**
       - `ReservationStatus(StrEnum)`: `active`, `committed`, `released` and `expired`.
       - `ReservationLine(presentation_id: UUID, quantity: int)`, frozen.
       - `RESERVATION_REFERENCE_MAX = 64`.
       - `SALE_ACTOR = "system"` (decision 27: the system is the actor of a sale).
     - **`Reservation(AggregateRoot)`.** Fields:
       - `id`, `reference: str`;
       - `lines: tuple[ReservationLine, ...]`, sorted by `presentation_id`;
       - `status`, `expires_at`, `created_at`;
       - `closed_at: datetime | None`, set by commit, release and expire.
     - **Methods** (none touch stock; the commands pair them with `StockItem`):
       - `create(reference, quantities: Mapping[UUID, int], expires_at, now) ->
         Result[Reservation, ReservationInvalid | ReservationExpiryInvalid]`. The reference
         is stripped, 1..64 characters. At least one line; each quantity 1..`QUANTITY_MAX`.
         `expires_at > now`.
       - `holds_units(now) -> bool`: `status == active`. An active reservation past its
         expiry still holds its units until the job expires it.
       - `extend(expires_at, now) -> Result[None, ReservationNotActive |
         ReservationExpiryInvalid]`. It must be active with `now < self.expires_at`, else
         `ReservationNotActive`. The new value must be later than the current one, else
         `ReservationExpiryInvalid`.
       - `commit(now)`: from `active`, `expired` or `released` to `committed`. Calling it on
         `committed` raises `ValueError`, because the command returns early.
       - `release(now)`: from `active` to `released`.
       - `expire(now)`: from `active` to `expired`.
       - `sale_reason() -> str`: `f"Venta: {reference}"` (Spanish ledger copy, decision 27).
     - **`ReservationRepository`** in `repositories.py`:
       - `add(reservation) -> bool`: False when the reference is taken.
       - `get_for_update(id) -> Reservation | None`.
       - `claim_expired(now, limit) -> list[Reservation]`: active, `expires_at <= now`,
         locked `FOR UPDATE SKIP LOCKED`, ordered by `id`.
       - `save(reservation)`: status, `expires_at` and `closed_at`; the lines never change.
     - **`StockRepository`** gains `get_many_for_update(ids, *, now) -> dict[UUID,
       StockItem]`: the same insert-then-lock as `get_for_update`, in ascending
       `presentation_id` order.
   - Observable result: unit tests of every transition, including those refused.

3. **Persistence and migration**
   - Files: `apps/api/src/fragancia_api/modules/inventory/infrastructure/tables.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/sql_stock_repository.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/sql_reservation_repository.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/in_memory.py` (modify),
     `apps/api/migrations/versions/0014_inventory_reservations.py` (create)
   - Do:
     - **Tables** (`tables.py`):
       - `stock_items` gains `reserved` Integer, not null, server default 0, with
         `CheckConstraint("reserved >= 0", name="reserved_not_negative")`.
       - `reservations`: `id` Uuid PK; `reference` String(64), unique; `status` String(12);
         `expires_at`, `created_at` and `closed_at` (nullable), all DateTime(tz). Add a
         partial index `ix_reservations_active_expires` on `expires_at`
         `WHERE status = 'active'`.
       - `reservation_lines`: `reservation_id` Uuid; `presentation_id` Uuid, indexed;
         `quantity` Integer with `CheckConstraint("quantity > 0")`. The PK is
         `(reservation_id, presentation_id)`. The FK goes to `reservations.id`, which is the
         same schema.
     - **`SqlStockRepository`** reads and writes `reserved`, and implements
       `get_many_for_update`: one `INSERT … ON CONFLICT DO NOTHING` for all ids, then
       `SELECT … WHERE presentation_id IN … ORDER BY presentation_id FOR UPDATE`.
     - **`SqlReservationRepository`:**
       - `add` uses `INSERT … ON CONFLICT (reference) DO NOTHING RETURNING id` for the
         reservation, then inserts the lines.
       - `claim_expired` uses `with_for_update(skip_locked=True)`.
     - **In-memory fakes:** `InMemoryStock` keeps `reserved`. Add `InMemoryReservations` with
       the same contract.
     - **Migration.** Run `uv run just db-revision "inventory reservations"` and rename the
       file to `0014` (`down_revision "0013"`). The downgrade drops the two tables and the
       column.
   - Observable result: `uv run just db-migrate` and its downgrade/upgrade round trip run
     clean on the test database.

4. **Application: the commands and the facade**
   - Files: `apps/api/src/fragancia_api/modules/inventory/application/commands/reservations.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/application/stock_reservations.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/__init__.py` (modify)
   - Do: every command runs in one `transactions.run`. Each one publishes
     `item.changed_event(global_threshold, now)` for every item it changed, with `now` taken
     after the locks.
     - **`ReserveStock.execute(reference, quantities, expires_at)`**, all or nothing
       (decision 24):
       1. `Reservation.create`.
       2. For each id in ascending order, `_require_stocked` (catalog share).
       3. `settings.get_for_share()`.
       4. `stock.get_many_for_update(ids, now=…)`, then `now = clock.now()`.
       5. Try `hold` on every line. If any line refuses, return one `StockUnavailable` that
          lists every short line. Nothing is written, because the runner rolls back.
       6. `reservations.add`. If it returns False, `ReservationReferenceTaken`.
       7. Save the items and publish.

       Returns the reservation id.
     - **`ExtendReservation.execute(reservation_id, expires_at)`:** lock the reservation and
       call `extend`. Stock does not change, so no event is published.
     - **`CommitReservation.execute(reservation_id)`** (decisions 25 and 27):
       1. Lock the reservation. Unknown means `ReservationNotFound`; already `committed`
          returns `Ok` without doing anything (payment webhooks retry).
       2. `settings.get_for_share()`, then `get_many_for_update` of its lines.
       3. `held = reservation.holds_units(now)`.
       4. `sell(quantity, reservation.sale_reason(), SALE_ACTOR, now, held=held)` on every
          line. If any line refuses, return one error listing every short line, and change
          nothing:
          - a held reservation refuses with `StockInsufficient` (a loss left fewer units than
            it holds);
          - a late commit refuses with `StockUnavailable`.

          The reservation keeps its status, and the caller decides (decision 25).
       5. `reservation.commit(now)`, save everything and publish.
     - **`ReleaseReservation.execute(reservation_id)`:** lock the reservation.
       - Active: `unhold` every line under the item locks, `release`, save and publish.
       - `released` or `expired`: `Ok`, a no-op.
       - `committed`: `ReservationCommitted`.
     - **`ExpireReservations.run_once() -> int`:**
       1. `claim_expired(now, limit=100)`.
       2. `get_many_for_update` over the union of their lines.
       3. `unhold` each line, `expire` each reservation, save and publish one event per
          changed item.

       Returns the count.
     - **Lock order.** Write it as a comment at the top of `reservations.py`:
       - ReserveStock: catalog (share), settings (share), then items, all in
         `presentation_id` order. The new reservation row is invisible until commit.
       - The other commands: the reservation row(s), settings (share), then items in
         `presentation_id` order.
       - Only reservation operations lock reservation rows, and every operation takes items
         in one global order, so no wait cycle forms with `RecordMovement` or
         `UpdateInventorySettings`.
     - **The facade `StockReservations`** (`stock_reservations.py`) has `reserve`, `extend`,
       `commit` and `release`. Each one delegates to its command and returns its `Result`.
       - The docstring states the rules: decisions 24–28, the idempotent commit and release,
         and that inventory takes the expiry from the caller (decision 4).
       - Export it from `__init__.py` and update the public API paragraph.
       - Update the event's docstring there: `available` is now `on_hand − reserved`, never
         below 0.
   - Observable result: application tests of each command against the in-memory adapters.

5. **Queries and the panel**
   - Files: `apps/api/src/fragancia_api/modules/inventory/contracts.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/application/ports.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/application/queries/stock.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/sql_stock_queries.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/http/router.py` (modify),
     `apps/api/openapi.json` (modify)
   - Do (decision 29):
     - **Contracts:**
       - `StockResponse` and `StockLine` gain `reserved: int` and `available: int`, both
         `ge=0`. `is_low` is computed from `available`.
       - The `kind` Literals of `StockMovement` gain `"sale"`. `MovementRequest` does not, so
         the panel cannot record a sale (422).
       - New `ReservationHold(reservation_id, reference, quantity, expires_at, created_at)`
         and `ReservationHoldPage(Page[ReservationHold])`.
     - **Ports** (`StockQueries`):
       - `get` and `on_hand_by_id` return `(on_hand, reserved, override)`;
       - new `active_holds(presentation_id, *, page, size) -> ReservationHoldPage`: active
         reservations holding it, soonest expiry first, ties by id.
     - **Queries:**
       - `GetStock` and `ListStock` fill the new fields, with `is_low(available, …)`.
       - New `ListReservationHolds` follows `ListMovements` (404 for an unknown
         presentation).
     - **SQL:** the new column, plus a join of `reservation_lines` with `reservations` for
       `active_holds`.
     - **Route** `GET /admin/inventory/presentations/{presentation_id}/reservations`, with
       `page` and `size` as in the movements route, and 404 `INVENTORY_PRESENTATION_NOT_FOUND`.
     - Run `uv run just openapi`.
   - Observable result: the stock detail shows `on_hand 5, reserved 2, available 3`, and the
     new route lists the hold.

6. **Wiring, job and docs**
   - Files: `apps/api/src/fragancia_api/modules/inventory/module.py` (modify),
     `docs/architecture.md` (modify),
     `apps/api/tests/unit/inventory/` (modify), `apps/api/tests/integration/inventory/` (modify),
     `apps/api/tests/integration/catalog/` (modify)
   - Do:
     - **Wiring** (`module.py`):
       - build `SqlReservationRepository`;
       - register the five commands, `StockReservations` and `ListReservationHolds`;
       - add `jobs=(ScheduledJob("inventory_expire_reservations", lambda s:
         s.get(ExpireReservations).run_once(), 60, 120),)` to the `AppModule`.
     - **`docs/architecture.md`:** under "Events, outbox and jobs", add a short bullet on
       reservations:
       - the counter;
       - available = on hand − reserved;
       - the expiry job;
       - the facade orders will call.
     - **Tests:** existing tests that build `StockItem` or read `StockQueries.get` tuples keep
       working through the defaults, or get the minimal shape update. The tester writes the
       layered tests.
   - Observable result: `uv run just check` and `uv run just test-integration` are green. The
     worker lists `cron:inventory_expire_reservations`.

## Acceptance criteria

Driven through `StockReservations`, obtained from `build_container(Settings()).services`, with
the API and the worker running. There is no HTTP route to reserve (out of scope).

- [ ] **Reserve.**
  - With `on_hand 5`, reserving 2 under reference `QA-1` (expiring in 2 minutes) returns an id.
  - `GET /admin/inventory/presentations/{id}` shows `on_hand 5, reserved 2, available 3`.
  - `…/reservations` lists `QA-1`, quantity 2.
  - An `inventory.stock.changed` event with `available 3` reaches the catalog copy.
- [ ] **All or nothing.**
  - A reservation of 1 unit of A plus 10 units of B, when B has 3 available, answers
    `INVENTORY_STOCK_UNAVAILABLE`. The details list B with `requested 10` and `available 3`.
  - A's `reserved` is unchanged.
- [ ] **Reference taken.** Reusing `QA-1` answers `INVENTORY_RESERVATION_REFERENCE_TAKEN`.
- [ ] **Not stocked.** Reserving a made-to-order or unknown presentation answers
  `INVENTORY_PRESENTATION_NOT_STOCKED` or `INVENTORY_PRESENTATION_NOT_FOUND`.
- [ ] **Extend.**
  - Extending `QA-1` to 72 h moves `expires_at`.
  - Extending to an earlier time answers `INVENTORY_RESERVATION_EXPIRY_INVALID`.
  - Extending an expired one answers `INVENTORY_RESERVATION_NOT_ACTIVE`.
- [ ] **Commit.**
  - Committing an active reservation records a `sale` movement with reason `Venta: QA-1`,
    actor `system` and delta −2. It leaves `on_hand 3, reserved 0`.
  - A second commit is a no-op `Ok`.
- [ ] **Release.**
  - Releasing an active reservation returns its units to `available`, and records no
    movement.
  - Releasing again is a no-op.
  - Releasing a committed one answers `INVENTORY_RESERVATION_COMMITTED`.
- [ ] **Expiry job.** Within about a minute of its expiry, the worker marks a reservation
  `expired` and frees its units. The storefront follows.
- [ ] **Late commit.**
  - Committing an expired reservation while units are available sells them.
  - With no units available it answers `INVENTORY_STOCK_UNAVAILABLE`, and the reservation
    stays `expired`.
- [ ] **A loss below reserved.**
  - With `on_hand 3, reserved 2`, a loss of 2 is recorded: `on_hand 1`, `available 0`, and
    the storefront shows `sold_out`.
  - Committing the reservation then answers `INVENTORY_STOCK_INSUFFICIENT`, and nothing
    changes.
- [ ] **The panel cannot record a sale.** `POST …/movements` with `kind: "sale"` answers 422.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `available` floor; `hold`/`unhold`/`sell` (held and not); `record_movement` refuses `sale`; `is_low` and `changed_event` use available; every `Reservation` transition and refusal, reference and quantity bounds, expiry rules |
| application | yes     | Each command against the fakes: all-or-nothing with every short line listed, reference taken, idempotent commit and release, late commit both ways, loss below reserved then commit refused, the job's batch, events per changed item; the queries' new fields and `is_low` by available |
| http        | yes     | New fields on stock detail and list; the reservations route (404, pagination); `sale` refused on the movement route; OpenAPI |
| integration | yes     | Migration round trip and constraints; SQL repositories (reference conflict, `claim_expired` skip-locked, `get_many_for_update` order); concurrency on Postgres: two reserves racing for the last unit (exactly one wins), a reserve racing a loss, a commit racing the expiry job, a settings change racing a reserve (no deadlock); the event reaches the catalog copy |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

All cosmetic, fixed forward (no design or scope impact):

- **Step 1.** Added a pure helper `available_units(on_hand, reserved)` in `domain/stock.py`.
  `StockItem.available` and the queries (`GetStock`, `ListStock`) share it, so the
  `max(on_hand − reserved, 0)` rule exists only once. `StockItem.__init__` also defaults
  `reserved` to 0 (not just `empty`), so existing callers keep working (step 6). The common
  ledger append moved into a private `StockItem._append`, which `record_movement` and `sell`
  both use.
- **Step 4, refusal details of a held commit.** The plan says a refusal lists "every short
  line" but gives a shape only for `StockUnavailable`. `StockInsufficient` from
  `CommitReservation` uses `{"lines": [{"presentation_id", "requested", "on_hand"}]}`.
  `presentation_id` is a string in both, so the details serialize as JSON.
- **Step 4, unknown id.** `ExtendReservation` and `ReleaseReservation` also answer
  `ReservationNotFound` for an unknown id. The plan states this only for commit.
- **Step 4, `updated_at`.** Reserve, release and expire leave `stock_items.updated_at` as it
  is (the domain's `hold`/`unhold` don't stamp it, as `set_threshold` doesn't). Only the
  `sale` movement of a commit stamps it, like every movement. The plan doesn't specify this.
- **Step 4, `ExpireReservations`.** Takes the settings row (share) after claiming the
  reservations, as the lock-order comment says, to stamp the events with the effective
  threshold. It logs `inventory.reservations_expired` with the count.
- **Step 3, migration.** `uv run just db-revision` already numbered it `0014` (no rename).
  Autogenerate did not detect the new `ck_stock_items_reserved_not_negative` check on the
  existing table, so it was added by hand (`op.create_check_constraint`, dropped in the
  downgrade). A round trip on the test database (upgrade → downgrade 0013 → upgrade) ran
  clean, and `alembic check` reports no drift. Applied to the development database with
  `uv run just db-migrate`.
- **Step 6, tests.** Minimal shape updates only: `tests/unit/inventory/test_stock_http.py`
  (the new `reserved`/`available` keys) and `tests/integration/inventory/test_sql_stock.py`
  (`StockQueries.get`/`on_hand_by_id` 3-tuples). `tests/integration/catalog/` needed no
  change.

Commands run: `uv run just typecheck` ✔, `uv run just arch` ✔ (7 kept), `uv run just openapi`
✔, `uv run just check` ✔, `uv run just test-integration` ✔ (581 passed, 5 skipped),
`uv run just plans-scope plans/inventory-stock/003-reservations.md` ✔ (every change inside
the plan). Not run: the worker process itself (`cron:inventory_expire_reservations` is
registered through `AppModule.jobs` but was not seen running), and the acceptance criteria
against the running app (verify phase).

## Test coverage

## Review findings

## Verification
