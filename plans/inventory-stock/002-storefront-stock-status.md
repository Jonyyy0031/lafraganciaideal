---
status: testing
module: inventory
min_implementer: mid
depends_on: ["001"]
---

# 002 — Stock status on the storefront

## Context

Plan 001 (done) keeps the stock of each in-stock presentation in `inventory.stock_items`.
- **Commands.** `RecordMovement`, `SetThreshold` and `UpdateInventorySettings` are in
  `apps/api/src/fragancia_api/modules/inventory/application/commands/stock.py:36`, `:86` and
  `:129`.
- **The rule.** One domain rule decides "low" (`is_low` and `effective_threshold` in
  `modules/inventory/domain/stock.py`).
- **The storefront doesn't know any of it yet.**
  - `PublicPresentation` has no stock field (`modules/catalog/contracts.py:305-315`).
  - `PublicPerfumeCard` shows `price_from_cents` (`:287-298`).
  - The public list computes that price as the minimum effective price over **all active**
    presentations (`modules/catalog/infrastructure/sql_perfume_queries.py:162-171`). It
    filters and sorts on it (`:180-183`, `:195-201`).

This plan exposes the stock status on the storefront. It applies README decisions 2, 6, 9 and
13–16:
- **Status per presentation.**
  - A presentation sold in stock gets `stock_status`, one of `available`, `last_units`
    ("¡Últimas piezas!") or `sold_out` ("Agotado").
  - A made-to-order presentation gets `null`.
  - The exact count is never exposed.
- **Buyable.** A presentation is buyable when it is made to order, or in stock with available
  units above 0.
  - The card gains `sold_out: bool`: true when the perfume has nothing buyable.
  - The "from" price, the price filters and the price sort use only buyable presentations. A
    perfume with nothing buyable falls back to its lowest price overall.
- **Filter.** `GET /api/v1/perfumes?in_stock=true` hides perfumes with nothing buyable.

**Approach (decision 15): an event, plus a copy in the catalog.**
- **The event.** Inventory publishes `inventory.stock.changed` through the outbox
  (`shared/infrastructure/outbox.py`). It does so on every movement, every threshold change,
  and, for every stock item, on a global settings change.
  - The payload is `presentation_id`, `available` (`on_hand` for now; plan 003 subtracts
    reservations), `threshold` (effective) and `as_of`, the time of the change.
- **The handler.** The catalog subscribes in its `register()`, as identity does
  (`modules/identity/module.py:194-211`). It stores the values on `catalog.presentations`:
  `stock_available`, `stock_threshold` and `stock_as_of`.
  - The update applies only when `stock_as_of` is null or older. Delivery is at least once,
    and the relay can deliver events in any order, so the handler is idempotent and keeps
    the newest value.
- **The status** is computed from those columns in SQL and in Python with the same rule:
  - made to order → `null`;
  - `available <= 0` → `sold_out`;
  - `available <= threshold` → `last_units`;
  - otherwise → `available`.

  The Python copy lives in the catalog domain. The inventory rule for "low" is the same
  comparison (`<=`); this plan's tests pin both against each other.
- **No import between the modules.** The catalog subscribes by the event name string. The
  inventory module exports the name in its `__init__` docstring, and the catalog uses the
  literal.
- **A presentation that switches** between made to order and in stock keeps its copied
  values. Inventory refuses movements on made-to-order presentations, so the copy is never
  stale.
- **Existing data.** The migration backfills the copy from `inventory.stock_items` and
  `inventory.settings`, a one-time SQL step in a platform migration. Presentations never
  counted keep `stock_available` 0, so they are `sold_out` (decision 1: 0 units).

**Alternatives.**
- **A query per page through an inventory facade** (synchronous). It cannot filter or sort
  on stock in SQL. Rejected by decision 15.
- **A cross-schema SQL join.** Forbidden by the module rules.

**Imitated files.**
- Event classes follow `modules/catalog/domain/brand.py:40-47` (`BrandCreated`).
- Publishing follows `modules/catalog/application/commands/create_brand.py:41`
  (`self._events.publish(...)`).
- The subscriber follows the identity handlers (`modules/identity/application/handlers/account_emails.py`).

## Out of scope

- **Blocking a purchase:** there is no cart or checkout yet. The orders initiative checks
  buyability, and reservations are plan 003.
- **Admin views:** the stock detail from plan 001 already shows the counts.
- **Changes to `catalog.presentations` other than the three stock columns.**
- **Backfilling the copy from an admin endpoint.** The migration does it once, and events keep
  it current.
- **The low-stock email** (plan 004).

## Dependencies

- **001 (done):**
  - `StockItem`, `effective_threshold` and `is_low` (`modules/inventory/domain/stock.py`);
  - the commands in `modules/inventory/application/commands/stock.py`;
  - the stock tables and `inventory.settings`.
- **Platform:**
  - `EventPublisher` and `EventSubscriptions` (`shared/application/events.py:10-33`);
  - `DomainEvent` (`shared/kernel/events.py:17-37`);
  - the outbox relay in the worker.

## Steps

1. **Inventory: the event**
   - Files: `apps/api/src/fragancia_api/modules/inventory/domain/stock.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/__init__.py` (modify)
   - Do:
     - **`StockChanged(DomainEvent)`.** Its name is `"inventory.stock.changed"`, and its
       fields are `presentation_id: UUID`, `available: int`, `threshold: int` and
       `as_of: datetime`.
     - **`StockItem.changed_event(global_threshold, as_of) -> StockChanged`**, where
       `available = on_hand` and `threshold = effective_threshold(...)`.
     - **Docstring.** Document the event name and its payload in the `__init__` docstring,
       under "Events".
   - Observable result: unit tests build the event with the right values.

2. **Inventory: publish it**
   - Files: `apps/api/src/fragancia_api/modules/inventory/application/commands/stock.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/domain/repositories.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/sql_stock_repository.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/in_memory.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/module.py` (modify)
   - Do:
     - **Wiring.** `RecordMovement`, `SetThreshold` and `UpdateInventorySettings` take
       `events: EventPublisher`, wired with `platform.events` in `module.py`.
     - **Per item, inside the same transaction, after the save:** the first two publish
       `item.changed_event(global, as_of=<the timestamp taken under the lock>)`.
     - **Global settings change:** `UpdateInventorySettings` loads every stock item through a
       new `StockRepository.list_all()`. It publishes one event per item with
       `as_of = clock.now()`. The new global threshold changes the effective threshold of
       items without an override.
     - **Global threshold source.** `RecordMovement` and `SetThreshold` read it with
       `settings.get()` inside the transaction. `RecordMovement` already receives
       `settings`.
   - Observable result: after a movement, `platform.outbox` holds an
     `inventory.stock.changed` row with the new values.

3. **Catalog: copy columns and migration**
   - Files: `apps/api/src/fragancia_api/modules/catalog/infrastructure/tables.py` (modify),
     `apps/api/migrations/versions/0013_catalog_presentation_stock_copy.py` (create)
   - Do:
     - **Columns** on `catalog.presentations`:
       - `stock_available` Integer, not null, server default 0;
       - `stock_threshold` Integer, not null, server default 3;
       - `stock_as_of` DateTime(tz), nullable.
     - **Migration.** `uv run just db-revision "catalog presentation stock copy"`, renamed to
       `0013`. After adding the columns, run one SQL `UPDATE … FROM inventory.stock_items
       si, inventory.settings s`:
       - `stock_available = si.on_hand`;
       - `stock_threshold = coalesce(si.threshold_override, s.low_stock_threshold)`;
       - `stock_as_of = si.updated_at`.

       A comment must explain that this is a one-time backfill, and that module code never
       reads across schemas. The downgrade drops the three columns.
   - Observable result: after `db-migrate`, the QA presentations of plan 001 show their counts
     in `catalog.presentations`.

4. **Catalog: the handler**
   - Files: `apps/api/src/fragancia_api/modules/catalog/application/handlers/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/catalog/application/handlers/stock_changed.py` (create),
     `apps/api/src/fragancia_api/modules/catalog/application/ports.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_stock_copy.py` (create),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/in_memory_perfumes.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/module.py` (modify)
   - Do:
     - **The port.** `StockCopy` in `ports.py`: `apply(presentation_id, available, threshold,
       as_of) -> None`.
     - **The SQL adapter.** A single `UPDATE presentations SET … WHERE id = :id AND
       (stock_as_of IS NULL OR stock_as_of < :as_of)`. An unknown presentation is a no-op.
     - **The handler.** `ApplyStockChanged(copy, transactions)` parses the payload: UUID, ints,
       ISO datetime. It calls `apply` inside `transactions.run`, which joins the relay's
       transaction, and returns `Ok`. A malformed payload raises, so the relay records the
       failure and retries.
     - **Registration.** In `register()`:
       `platform.subscriptions.subscribe("inventory.stock.changed", ApplyStockChanged(...))`.
   - Observable result: relaying the outbox updates the presentation's copy, and an older
     event changes nothing.

5. **Catalog: the status in the public reads**
   - Files: `apps/api/src/fragancia_api/modules/catalog/domain/perfume.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/contracts.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/application/ports.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_perfume_queries.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/http/perfume_router.py` (modify),
     `apps/api/openapi.json` (modify)
   - Do:
     - **Domain** (`perfume.py`): `stock_status(availability, available, threshold) ->
       Literal["available", "last_units", "sold_out"] | None`, with the rule from Context.
       `is_buyable(availability, available)` is made to order, or `available > 0`.
     - **Contracts:**
       - `PublicPresentation` gains `stock_status: Literal[...] | None`.
       - `PublicPerfumeCard` gains `sold_out: bool`.
     - **`PublicPerfumeFilters`** gains `in_stock: bool`.
     - **SQL** (`sql_perfume_queries.py`):
       - Add a `_buyable` expression: `availability = 'made_to_order' OR stock_available > 0`.
       - In the `prices` subquery (`:162-171`), compute two values per perfume:
         `price_from_buyable` (min effective price where `_buyable`) and `price_all` (min over
         all active presentations).
       - `price_from` is `coalesce(price_from_buyable, price_all)`. It feeds the card, the
         price filters and the sort, as today.
       - `sold_out` is `bool_or(_buyable)` negated.
       - `on_sale` keeps its current meaning (`bool_or(_sale_active)`).
       - `in_stock=true` adds `price_from_buyable IS NOT NULL`.
       - The detail's presentation rows (`:297-299`) already select every column. Map
         `stock_status` in `_public_presentation` (`:115-142`).
     - **Route.** `list_public_perfumes` takes `in_stock: bool = False` and documents it.
     - Run `uv run just openapi`.
   - Observable result: a counted presentation shows `available` or `last_units`, a
     never-counted one `sold_out`, and made to order `null`. The card's `price_from_cents`
     skips sold-out sizes.

6. **Docs and tests**
   - Files: `docs/architecture.md` (modify), `apps/api/tests/unit/catalog/` (modify),
     `apps/api/tests/unit/inventory/` (modify), `apps/api/tests/integration/catalog/` (modify),
     `apps/api/tests/integration/inventory/` (modify)
   - Do:
     - **`docs/architecture.md`:** describe the `inventory.stock.changed` → catalog copy flow
       under "Events, outbox and jobs".
     - **Tests:** the tester writes the layered tests.

## Acceptance criteria

- [ ] **Events in the outbox.**
  - A restock of 10 on a presentation (with the global threshold 3) puts one
    `inventory.stock.changed` row in `platform.outbox`: `available 10`, `threshold 3`.
  - Once the worker relays it, `catalog.presentations` shows `stock_available 10`.
  - `GET /api/v1/perfumes/{slug}` shows that presentation with `stock_status: "available"`.
- [ ] **Each status on the detail page.**
  - A loss down to 3 shows `last_units`, and down to 0 shows `sold_out`.
  - A made-to-order presentation shows `null`.
  - A never-counted in-stock presentation shows `sold_out`.
- [ ] **Thresholds.**
  - An override of 15 at `on_hand` 10 shows `last_units`.
  - A global threshold change to 12 re-publishes events for every stock item, and the
    statuses follow.
- [ ] **The public list.**
  - **Sold-out sizes don't count.** A perfume with a cheap sold-out 50 ml and an available
    100 ml shows `price_from_cents` = the 100 ml price.
  - **Filters and sort** use the same price.
  - **Nothing buyable.** A perfume with every size sold out stays in the list with
    `sold_out: true` and its lowest price.
- [ ] **`in_stock=true`** hides that perfume and keeps the others.
- [ ] **Idempotent copy.** Replaying an older event, with an earlier `as_of`, does not
  overwrite a newer copy.
- [ ] **Backfill.** After `db-migrate`, the presentations counted in plan 001's QA show their
  counts without any new movement.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `StockChanged` payload; `changed_event`; catalog `stock_status` and `is_buyable` at every boundary (0, equal to threshold, made to order); the two "low" rules agree |
| application | yes     | The three inventory commands publish the right events (settings: one per item); `ApplyStockChanged` parsing, newer-wins call, malformed payload raises |
| http        | yes     | The public list and detail: new fields, the `in_stock` query parameter, OpenAPI |
| integration | yes     | Migration round trip and backfill; the outbox row content; relay → handler → copy, including an older event ignored; the public SQL: buyable-only `price_from`, filters and sort on it, `sold_out`, `in_stock=true` |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

- **Cosmetic, step 2:** `SetThreshold` and `UpdateInventorySettings` also gained the
  dependencies they need to publish (`SetThreshold` takes `settings`; `UpdateInventorySettings`
  takes `stock` and `clock`), beyond the plan's "take `events`". Existing tests that build the
  commands directly (`tests/unit/inventory/support.py`, `test_repair_round_1.py`,
  `test_repair_round_2.py`, `tests/integration/inventory/test_sql_repair_round_1.py`,
  `test_sql_repair_round_2.py`) got the new constructor arguments, and the existing public list
  HTTP test (`tests/unit/catalog/test_public_perfume_http.py`) got `sold_out` in its expected
  card, only to keep the build green. No new tests were written; that is the tester's phase.
- **Cosmetic, step 3:** the migration backfill takes the global threshold from a scalar
  subquery on `inventory.settings` with a fallback of 3, instead of a join, so it still works
  when the settings row was never stored.
- **Cosmetic, step 5:** `PublicPerfumeFilters.in_stock` defaults to `False`, so existing
  constructors keep working. The status rule is computed in Python only (detail); the list uses
  the SQL `_buyable` expression, so there is no SQL copy of the status itself.

## Test coverage

## Review findings

## Verification
