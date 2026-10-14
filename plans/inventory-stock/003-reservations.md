---
status: review
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
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_perfume_repository.py` (modify, repair round 1),
     `apps/api/tests/unit/catalog/` (modify, repair round 1),
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

### Repair round 1 (2026-10-14, main session: review → implementing)

**Why.** Review finding M1 (`plans/findings/catalog-perfume-save-presentation-lock-order.md`):
- `ReserveStock` share-locks catalog presentations in `presentation_id` order.
- The catalog's perfume save upserts them in `ml` order: the aggregate loads them ordered by
  `ml` (`modules/catalog/infrastructure/sql_perfume_repository.py:219-224`), and `save`
  iterates them as loaded (`:254-262`).
- So a two-size checkout racing an admin save of that perfume can deadlock, and PostgreSQL
  aborts one side with a 500.

The user chose to fix M1, L1 and L2 in this plan (README decision 30). M1 reaches outside the
plan's declared files, into the catalog's SQL perfume repository, and is recorded here as a
deviation.

**Repair spec.**
1. **M1: the catalog writes presentations in id order.**
   - Files: `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_perfume_repository.py`,
     `plans/findings/catalog-perfume-save-presentation-lock-order.md`.
   - In `SqlPerfumeRepository.save`, iterate `sorted(perfume.presentations, key=lambda p:
     p.id)` for the presentation upserts.
     - Add a comment: other modules (inventory reservations) lock presentation rows in id
       order, and every writer of several presentation rows must use the same order.
     - Only the write order changes. The aggregate's `ml` ordering (reads and responses) stays.
   - `add` inserts rows nobody else can see yet, so it does not need the order. Leave it as is.
   - No other catalog writer locks or updates several presentation rows:
     - `sql_stock_copy.py:21` updates one row;
     - `sql_presentation_directory.py:70` share-locks one row.
   - Extend the lock-order comment at the top of
     `modules/inventory/application/commands/reservations.py:3-13`: the catalog presentations
     are also written in id order by the catalog's perfume save.
   - The finding gets `status: resolved`, `plan: inventory-stock/003`, and a resolution line.
2. **L2: the refusal comment.**
   - Files: `apps/api/src/fragancia_api/modules/inventory/application/commands/reservations.py`.
   - Rewrite the comment at the `short` refusal (around `:111`): "A standalone call rolls back
     everything. Inside a caller's transaction, the empty stock rows the locks inserted
     remain. They read exactly like a presentation never counted (0 on hand), so they are
     harmless."
   - Keep the behaviour. The tester adjusts the test that claims no rows are left, so it states
     the standalone case.
3. **L1:** test-only, for the tester. A race test pins that a two-line reserve locks the lower
   `presentation_id` before waiting on the higher one. A catalog race test shows that a
   two-size reserve and a perfume save of those sizes both finish, with no deadlock, when
   the presentations were created in reverse `ml` order.

**Scope.** The product code is `sql_perfume_repository.py` (`save` only) and comments in
`reservations.py`. The implementer changes no tests.

## Test coverage

Files (all new unless noted): `apps/api/tests/unit/inventory/test_reservation_domain.py`,
`test_reservation_commands.py`, `test_reservation_queries.py`, `test_reservation_http.py`,
`test_reservation_wiring.py`, `support.py` (modified: the reservation commands and the facade on
`Inventory`); `apps/api/tests/integration/inventory/test_sql_reservations.py`,
`test_reservation_races.py`, `reservation_support.py`, `conftest.py` (modified: the cleanup
fixture also truncates the reservation tables and the outbox after each test, so no row stays in
`fragancia_test`).

Race tests (Postgres) pause one transaction on a gate while the other starts, assert the second
is blocked (or, for `SKIP LOCKED`, returns without waiting), then open the gate. `Scenario`
opens every gate and finishes or cancels every task on exit, and every wait is bounded by 5 s, so
a lock-order regression fails fast instead of hanging. Three consecutive runs of the race file
were green (about 0.5 s per race).

| Behavior (plan / code) | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| `available = max(on_hand - reserved, 0)`; `is_low` and `changed_event` use it | `stock.py:131`, `:183` | domain | `test_reservation_domain.py::test_available_is_on_hand_minus_reserved_never_below_zero`, `::test_is_low_uses_available_...`, `::test_the_changed_event_carries_available_not_on_hand` | CONFIRMED |
| `hold` refuses beyond available; `unhold` below 0 is a `ValueError` | `stock.py:211`, `:218` | domain | `::test_hold_*`, `::test_unhold_*` | CONFIRMED |
| `sell` held needs only `on_hand`; late needs `available`; refusals change nothing | `stock.py:224` | domain | `::test_a_held_sale_*`, `::test_a_late_sale_*` | CONFIRMED |
| `record_movement` refuses `sale` | `stock.py:196` | domain | `::test_record_movement_refuses_a_sale_whatever_the_quantity` | CONFIRMED |
| `Reservation.create` bounds (reference 1..64 stripped, lines, quantity 1..max, expiry in future), lines sorted | `reservation.py:74` | domain | `::test_create_*` | CONFIRMED |
| `extend`, `commit`, `release`, `expire`, `holds_units`, `sale_reason`, every refused transition | `reservation.py:107-141` | domain | `::test_extend_*`, `::test_an_active_reservation_can_be_closed_three_ways`, `::test_*_only_applies_to_an_active_reservation`, `::test_a_released_or_expired_reservation_can_still_be_committed` | CONFIRMED |
| Reserve is all or nothing, lists every short line, nothing written | `reservations.py:96-112` | application | `test_reservation_commands.py::test_reserve_is_all_or_nothing_...`, `::test_reserve_lists_every_short_line_not_just_the_first` | CONFIRMED |
| Reserve: reference taken, not stocked / unknown, invalid input, event per changed item with `available`, threshold from settings | `reservations.py:76-122` | application | `::test_reserve_*` | CONFIRMED |
| Extend: later expiry, earlier refused, expired / closed not active, unknown, no event | `reservations.py:125` | application | `::test_extend_*` | CONFIRMED |
| Commit: `sale` movement (reason `Venta: QA-1`, actor `system`, delta -2), idempotent, one sale per presentation, event | `reservations.py:158-241` | application | `::test_commit_*` | CONFIRMED |
| Late commit both ways (units available / not), released late commit, other holds untouched | `reservations.py:158` | application | `::test_a_late_commit_*` | CONFIRMED |
| Loss below reserved is recorded, commit then refused (`INSUFFICIENT`, `on_hand` in details), nothing changes, other lines untouched | `reservations.py:203-230` | application | `::test_a_loss_below_reserved_*`, `::test_committing_a_reservation_a_loss_left_short_*`, `::test_a_refused_commit_sells_no_line_*` | CONFIRMED |
| Release: units back, no movement, idempotent (released / expired), committed refused, unknown | `reservations.py:244` | application | `::test_release_*` | CONFIRMED |
| Expiry job: due only (exact instant counts), ignores closed ones, batch limit, one event per changed item, count | `reservations.py:296` | application | `::test_the_job_*`, `::test_an_expiry_frees_units_for_a_new_reservation` | CONFIRMED |
| Facade delegates and is exported from the module | `stock_reservations.py`, `__init__.py` | application | `::test_the_facade_*` | CONFIRMED |
| `GetStock`, `ListStock` fill `reserved` / `available`; low flag by available; holds query (soonest first, active only, pages, 404) | `queries/stock.py` | application | `test_reservation_queries.py` | CONFIRMED |
| Stock detail and list carry the new fields; reservations route (401, 403, 200, paging, 404, 422); `sale` on the movement route is 422; a sale shows in the ledger | `router.py:99`, `contracts.py:18`, `:47` | http | `test_reservation_http.py` | CONFIRMED |
| OpenAPI: the route, required `reserved`/`available`, `sale` in the ledger kind but not in the request, no reserve/commit/release route | `openapi.json` | http | `test_reservation_http.py::test_openapi_*` | CONFIRMED |
| Container resolves the commands, the facade and the holds query; the job is declared (60 s, timeout 120 s) and runs the command | `module.py` | application | `test_reservation_wiring.py` | CONFIRMED |
| Schema: `reserved` default 0 and `>= 0` check, `reference` unique, `quantity > 0`, FK to reservations, line PK, no FK to catalog, partial index on active expiry | `tables.py` | integration | `test_sql_reservations.py::test_the_database_*`, `::test_a_line_*`, `::test_the_expiry_scan_has_a_partial_index_*` | CONFIRMED |
| `SqlReservationRepository`: add / reload with lines, reference conflict writes nothing, rollback, `save` fields, `claim_expired` (due, ordered by id, limit, exact instant, `SKIP LOCKED`) | `sql_reservation_repository.py` | integration | `::test_add_*`, `::test_save_*`, `::test_claim_expired_*` | CONFIRMED |
| `get_many_for_update`: inserts missing items empty, ascending and deduplicated, keeps stored values, locks all rows | `sql_stock_repository.py:56-80` | integration | `::test_get_many_for_update_*` | CONFIRMED |
| `SqlStockQueries`: `reserved` in `get` / `on_hand_by_id`; `active_holds` order, ties, only active, pages | `sql_stock_queries.py` | integration | `::test_queries_*`, `::test_active_holds_*` | CONFIRMED |
| Flows on Postgres: reserve shows in stock, holds and catalog copy; all or nothing leaves no rows (a standalone refusal also rolls back the empty item its lock inserted); reference taken; not stocked; extend; commit (sale row, idempotent); release; late commit both ways; loss below reserved then commit refused; list low by available; facade | `reservations.py` | integration | `::test_reserve_*`, `::test_extend_*`, `::test_commit_*`, `::test_release_*`, `::test_a_late_commit_*`, `::test_a_loss_below_reserved_*` | CONFIRMED |
| Expiry job on Postgres, storefront follows | `reservations.py:296` | integration | `::test_the_expiry_job_*` | CONFIRMED |
| Race: two reserves for the last unit, exactly one wins (gated; and 8 concurrent for 3 units); same reference once; opposite key order does not deadlock | locks in `reservations.py` | integration | `test_reservation_races.py::test_the_second_reserve_waits_*`, `::test_many_concurrent_reserves_*`, `::test_two_reserves_with_one_reference_*`, `::test_reserves_over_the_same_presentations_in_opposite_order_*` | CONFIRMED |
| Race: reserve vs loss, both orders (loss may leave `on_hand` below `reserved`; reserve sees the units a loss left) | | integration | `::test_a_loss_waits_for_a_reserve_*`, `::test_a_reserve_waits_for_a_loss_*` | CONFIRMED |
| Race: commit vs expiry job, both orders (job skips; commit after job is a late commit; one sale; copy ends at the newest event) | | integration | `::test_the_job_skips_a_reservation_a_commit_*`, `::test_a_commit_waits_for_the_job_*` | CONFIRMED |
| Race: release vs job, two job runs, two commits, two releases, commit vs release (each hold consumed once, never below 0) | | integration | `::test_the_job_skips_a_reservation_a_release_*`, `::test_a_release_waiting_for_the_job_*`, `::test_two_job_runs_*`, `::test_two_commits_*`, `::test_two_releases_*`, `::test_a_commit_and_a_release_*` | CONFIRMED |
| Race: settings change vs reserve, both orders; vs commit / release / job; no deadlock | | integration | `::test_a_reserve_waits_for_a_settings_change_*`, `::test_a_settings_change_waits_for_a_reserve_*`, `::test_a_reservation_command_waits_for_a_settings_change_*`, `::test_a_settings_change_waits_for_a_commit_*` | CONFIRMED |
| Storm: reserves, commits, releases, loss, settings change and job runs at once keep `reserved` equal to what active reservations hold (3 rounds) | | integration | `::test_a_mixed_storm_*` | CONFIRMED |
| The event reaches the catalog copy (`available` 3 after reserve; restored after expiry; storefront `sold_out` / `last_units`) | | integration | `test_sql_reservations.py::test_reserve_shows_in_the_stock_*`, `::test_a_reservation_that_empties_the_shelf_*`, `::test_the_expiry_job_expires_*` | CONFIRMED |
| Migration downgrade/upgrade round trip | `0014_inventory_reservations.py` | integration | not tested: running a downgrade on the shared test database would race other suites; the implementer recorded one round trip and `alembic check` (Deviations, step 3) | NOT CONFIRMED (by a test) |
| The worker process runs `cron:inventory_expire_reservations` | `module.py` | none | the job declaration and its command are tested; the worker process was not started | NOT CONFIRMED (verify phase) |

No GAP found: every behavior the plan promises is implemented as written. Deviations recorded by
the implementer (details shape of a held-commit refusal, unknown id on extend/release, `updated_at`
untouched by hold/unhold) are asserted as implemented.

Commands run: baseline `uv run just check` green (2201 unit + 685 harness tests) before any test.
Closing: `uv run just check` green (2358 unit passed, 3 skipped; 685 harness) and
`uv run just test-integration` green (652 passed, 5 skipped; the plan's own run before the tests
was 581 passed). New tests: 157 unit (domain 67, commands 53, queries 13, http 21, wiring 3) and 71
integration (SQL 48, races 23). `fragancia_test` was checked after
the run: no inventory, reservation or catalog rows left; the one `platform.outbox` row
(`tests.something.happened`) comes from a platform suite, not from these tests.

### Test coverage, repair round 1 (2026-10-14, L1; supersedes nothing above)

Files: `apps/api/tests/integration/inventory/test_reservation_lock_order.py` (new),
`reservation_support.py` (modified: `Commands` takes an optional presentation catalog, so a test
can pause a reserve between two presentation locks), `test_sql_reservations.py` (modified: the
refused-reserve test is renamed and documented as the standalone case). Product code unchanged.

| Behavior (repair spec) | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| A two-line reserve locks the lower `presentation_id` stock item before waiting on the higher one, whatever order the caller lists them | `sql_stock_repository.py` (`get_many_for_update`), `reservations.py:89-95` | integration | `test_reservation_lock_order.py::test_a_two_line_reserve_locks_the_lower_id_before_waiting_on_the_higher` | CONFIRMED |
| A two-size reserve and a perfume save of those sizes (created in reverse `ml` order) both finish with no deadlock | `sql_perfume_repository.py` `save` (id order), `reservations.py` | integration | `::test_a_two_size_reserve_and_a_perfume_save_of_those_sizes_do_not_deadlock` | CONFIRMED |
| A refused reserve leaves no empty stock row when it runs standalone (inside a caller's transaction the row remains, harmless) | `reservations.py:111-115` | integration | `test_sql_reservations.py::test_a_standalone_refused_reserve_does_not_leave_the_empty_item_it_locked` | CONFIRMED (standalone case only; the in-caller case is not tested: no caller exists yet) |

How the tests work: the lock-order test holds the higher stock row in a paused transaction, runs
a reserve listing the higher id first, and polls (5 s bound) with a `FOR UPDATE SKIP LOCKED`
probe until the lower row is held by the reserve while the reserve is still unfinished. The
deadlock test pauses the reserve (a catalog wrapper) after it share-locks the lower presentation,
starts the save, asserts the save is blocked, then opens the gate. Both use `Scenario`, so gates
open and tasks finish or are cancelled when the test ends, and every wait is bounded.

Mutation checks (product edited in place, restored by hand from a scratchpad copy; `git diff`
on `apps/api/src` was empty afterwards):
- Save iterating `perfume.presentations` in `ml` order (the old behaviour): the deadlock test
  fails with `psycopg.errors.DeadlockDetected` in about 1 s. The lock-order test is unaffected.
- `get_many_for_update` ordering by `presentation_id DESC`: the lock-order test fails (the lower
  row is never locked while the reserve waits). The deadlock test is unaffected.

Commands run: baseline `uv run just check` green (2358 unit, 685 harness) and
`uv run just test-integration` green (652 passed, 5 skipped). Closing: `uv run just check` green
(2358 unit passed, 3 skipped; 685 harness; plans-lint and harness-check OK) and
`uv run just test-integration` green (654 passed, 5 skipped). New tests: 2 integration; no unit
test added (L1 is a Postgres lock behaviour). `fragancia_test` afterwards: 0 rows in
`stock_items`, `reservations`, `reservation_lines`, `stock_movements`, `catalog.presentations`,
`catalog.perfumes`; the single `platform.outbox` row is the same platform-suite row as before.

## Review findings

Reviewed 2026-10-14 against `main...HEAD` (df9a2e1 implementation, d08d504 tests). The tree was
clean, with no uncommitted changes.

**Checklist: 14/15 pass, 1 not applicable.**

- [x] `plans-scope`: every change is inside the plan (23 declared, 34 changed). No hot file
  was touched (`container.py`, `docs/modules.json` and `.importlinter` are unchanged).
- [x] `uv run just check` is green: 2358 unit passed, 3 skipped; 685 harness.
- [x] `uv run just test-integration` is green: 652 passed, 5 skipped. Afterwards
  `fragancia_test` had 0 rows in `stock_items`, `reservations`, `reservation_lines`,
  `stock_movements`, `platform.outbox` and `catalog.presentations`.
- [x] Business rules live in `domain/` (`hold`, `unhold`, `sell`, the `Reservation`
  transitions). The commands only orchestrate, and the queries reuse `available_units` and
  `is_low`.
- [x] CQRS-lite: every command runs in `transactions.run` and returns a `Result`.
  `ListReservationHolds` goes through `StockQueries.active_holds`.
- [x] The contracts are in `contracts.py`. `openapi.json` was regenerated (the drift check in
  `check` passes).
- [x] Errors are `Err(DomainError)` with stable codes (`INVENTORY_STOCK_UNAVAILABLE`,
  `INVENTORY_RESERVATION_*`). A `ValueError` is raised only for programming errors.
- [x] Datetimes come from `Clock` and ids from `new_id()`. No money is involved.
- [x] Migration 0014 is new and adds nothing unexpected. It drops nothing, has no cross-schema
  FK (the only FK is lines → `inventory.reservations`) and has a full downgrade. The check
  constraint was added by hand, as Deviations says.
- [x] The new route is on `admin` and the existing protection test covers it.
- [x] Wiring resolves (`test_reservation_wiring.py`). The job is declared on `AppModule.jobs`
  and the adapters are created in `module.py`.
- [x] No secrets, `.env` contents or personal data.
- [x] Deviations are honest. Spot-checks: `hold`/`unhold` do not stamp `updated_at`
  (`domain/stock.py:211-222`), and extend/release answer `ReservationNotFound`
  (`reservations.py:145`, `:269`).
- [x] Docs were updated: `docs/architecture.md`, the event docstring in `inventory/__init__.py`
  and the module docstring of `domain/stock.py`.
- [ ] (n/a) PR body: no PR is open for `feat/inventory-reservations` yet. Check it when the PR
  is opened.

**Mutants** (each edited in place and restored by hand; `git status` was clean after each):

| Mutant | Result |
| --- | --- |
| Reservation `get_for_update` without `FOR UPDATE` | Killed by `test_a_commit_waits_for_the_job_then_sells_as_a_late_commit` (`ValueError` from `unhold`). |
| A late `sell` checks `on_hand` instead of `available` | Killed in the domain, application and integration layers. |
| `get_many_for_update` locks items `ORDER BY presentation_id DESC` | Killed only by the dict-order assertion of `test_sql_reservations.py::test_get_many_for_update_inserts_the_missing_items_empty_and_returns_all_ascending`. The race file passed in full, the storm included. See L1. |

**Bug hunt.** Traced reserve, extend, commit, release and expire end to end (domain →
repositories → outbox → catalog copy).

What holds:

- The counter is changed only under the item row lock. Locks are taken in `ORDER BY` order
  (LockRows sits above Sort), so concurrent first inserts of the same keys queue on the unique
  index in one order.
- Reservation rows are locked only by reservation operations. The job uses `SKIP LOCKED`.
- All or nothing holds: every short line is listed and nothing is saved before the decision.
- On a commit, `held` follows the locked status:
  - an active reservation needs `on_hand` (`STOCK_INSUFFICIENT`);
  - a late commit needs `available` (`STOCK_UNAVAILABLE`) and leaves the status as it is.

  This matches decisions 25 and 28.
- Each event's `as_of` is taken after the locks, so a later writer always stamps a later time.
- Within the inventory module I found no correctness bug.

### Medium

- **M1. The lock-order claim ignores catalog writers: a two-line reserve can deadlock with a
  perfume save.** Out of scope; recorded as
  `plans/findings/catalog-perfume-save-presentation-lock-order.md`. Not reproduced: this is
  reasoned from PostgreSQL row-lock semantics.
  - How the locks cross:
    - `ReserveStock` share-locks the catalog presentations one by one, by `presentation_id`
      (`application/commands/reservations.py:89`).
    - The catalog's perfume save upserts every presentation of the perfume in `ml` order
      (`catalog/infrastructure/sql_perfume_repository.py:224`, `:255`), which row-locks each
      one.
  - Failure scenario: a perfume's 100 ml presentation was created before its 50 ml one, so
    `id(100) < id(50)`. A checkout reserves both while an admin edits the perfume. The two
    transactions take the two rows in opposite orders. PostgreSQL aborts one of them after
    `deadlock_timeout`, so the checkout or the admin save fails with a 500.
  - What is wrong in this plan: the comment at `reservations.py:3-13` and the plan's Approach
    both say "no wait cycle forms", but they consider only inventory's own commands.
  - Why it is not repaired here: the fix (upsert in `id` order) is in catalog, outside this
    plan's file list. `RecordImportedCounts` (plan 005) already has the same pattern, but this
    plan puts it on the checkout path.
  - The user decides between:
    - fixing it in catalog before orders lands (fast lane, plus a race test);
    - accepting it;
    - widening this plan with a deviation.

### Low

- **L1. The race tests do not pin the item lock order.** Test-only; it goes to the tester.
  - The DESC-order mutant above passed the whole race file, including
    `test_a_mixed_storm_*` and `test_reserves_over_the_same_presentations_in_opposite_order_*`.
    - Every reserve sorts its lines in the aggregate, so reserves always agree on the order.
    - The storm did not interleave a settings change (ascending `list_all_for_update`) between
      the locks of a two-line reserve.
  - The only guard is the order of the returned dict (`test_sql_reservations.py:488-500`). If
    an implementation locked in DESC order and then re-sorted the dict in Python, it would
    still pass.
  - Suggested test: pause a `RecordMovement` on the higher id. Start a two-line reserve and
    assert that the lower id is locked and not blocked. For example, a second `RecordMovement`
    on the lower id should block behind the reserve. Then open the gate.
- **L2 (uncertain). A refusal inside a joined transaction is not rolled back.** Uncertain
  because orders is not built yet.
  - The facade's docstring says a call "joins the caller's [transaction] when one is active"
    (`stock_reservations.py:39`). `SqlTransactionRunner.run` then returns the inner `Err`
    without a rollback (`shared/infrastructure/database.py:64-65`).
  - Failure scenario: a refused reserve leaves the empty `stock_items` rows that
    `get_many_for_update` inserted. If orders commits its outer transaction, those rows are
    committed too.
  - Those rows are harmless (0 on hand, 0 reserved). However, the comment "the runner rolls
    back whatever the locks inserted" (`reservations.py:111`) and the test claim "leaves no
    rows (not even the empty item)" hold only for a standalone call.
  - When orders is planned, it should rely on `Err` meaning "no stock was held", not on "no
    rows were written".

No High findings. Status stays `review`:

- M1 needs a user decision (a catalog-side fix outside this plan's scope).
- L1 is a test-only correction for the tester.
- If the user accepts M1 as the separate finding and accepts or defers L1 and L2, the main
  session can move this plan to `verify`.

## Verification
