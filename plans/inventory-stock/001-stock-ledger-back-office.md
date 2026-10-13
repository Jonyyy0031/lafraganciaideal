---
status: verify
module: inventory
min_implementer: mid
depends_on: ["catalog-perfumes/003"]
---

# 001 — Stock ledger in the back office

## Context

Presentations are sold either "in stock" or "made to order". The domain records that kind
and the delivery days (`apps/api/src/fragancia_api/modules/catalog/domain/perfume.py:236-260`,
table `catalog.presentations` at `modules/catalog/infrastructure/tables.py:91`), but no unit
count. `inventory` is registered as a planned module (`docs/modules.json`, "Stock per size;
reserve and release stock for orders"). No code exists yet, and the catalog exposes no public
API ("Public API for other modules: nothing yet", `modules/catalog/__init__.py:5`).

This plan creates the `inventory` module and its first slice: the ledger in the back office
(README decisions 1, 5, 8, 9 and 11). Plans 002–005 build on it.

- **Stock items.** One per in-stock presentation: `presentation_id` (the catalog's id, not a
  foreign key, since modules own their schemas) and `on_hand` units. A stock item is created
  lazily by its first movement; a presentation without one has 0 on hand.
- **Movements.** Every change is a movement, in an append-only ledger (decision 5):

  | Kind | Quantity | Effect |
  | --- | --- | --- |
  | `restock` | > 0 | adds |
  | `return` | > 0 | adds |
  | `loss` | > 0 | subtracts |
  | `adjustment` | signed, ≠ 0 | adds or subtracts |
  | `count` | ≥ 0, absolute | sets `on_hand` to this number |

  Each movement stores `delta`, `on_hand_after`, a reason (1–200 characters), the actor and
  the time. `on_hand` never goes below 0, so a movement that would make it negative is
  refused.
- **Thresholds** (decision 9):
  - a global low-stock threshold, default 3, editable, stored in `inventory.settings`;
  - an optional override per presentation.

  A presentation is **low** when `on_hand <= effective threshold`.
- **Permission.** Every route requires the new `inventory:manage` permission (decision 11).
  It is added to both roles in identity (`modules/identity/domain/user.py:80-86`), next to
  `catalog:manage`. Inventory's router keeps its own constant, as catalog does
  (`modules/catalog/http/router.py:69-70`).

**Approach: crossing into the catalog.** Inventory must know that a presentation exists, that
it is sold in stock, and how to name it in the panel. Following the recipe
(`docs/recipes/new-module.md` §5):
- **The facade.** The catalog exposes a small query, `PresentationDirectory`, as its first
  public API (`modules/catalog/__init__.py`).
- **The port and adapter.** Inventory declares a port in its own terms, `PresentationCatalog`,
  and implements it in `infrastructure/catalog_presentations.py` by calling that facade.
- **Import rules.** `.importlinter` gets exactly two `ignore_imports` lines: the adapter, and
  `inventory/module.py`, which fetches the facade from the service registry. The catalog
  registers before inventory in `MODULES`.

**Alternatives.**
- **Reading `catalog.presentations` directly.** Forbidden by the module rules.
- **Copying presentation data into inventory through events.** That needs every catalog
  command to publish events, which none does yet (`modules/catalog/application/commands/perfumes.py:3`).
  Too much for this slice.

**The stock list.** It takes every active in-stock presentation of a non-archived perfume from
the facade, joins it in memory with the stock rows, then filters (`low=true`) and paginates.
The shop's catalog is hundreds of presentations, so this is cheap, and presentations never
counted show up with 0.

**Files imitated.**
- The module layout follows `docs/recipes/new-module.md` and the catalog reference module.
- The aggregate and value objects follow `modules/catalog/domain/brand.py`.
- Locking for update follows `SqlPerfumeRepository.get_for_update`
  (`modules/catalog/infrastructure/sql_perfume_repository.py`).
- The routes follow `modules/catalog/http/perfume_router.py`.

## Out of scope

- Storefront stock status (002), reservations and available versus on-hand stock (003), the
  low-stock email (004) and the Excel "Existencias" column (005).
- **Domain events.** Nothing subscribes to stock changes yet; plan 004 adds what it needs.
- **Undoing a movement.** A correction is a new `adjustment`.
- Stock for made-to-order presentations (decision 1).
- **Multiple warehouses or locations.**
- **Changing the catalog's own availability rules or contracts.** The new facade is additive.

## Dependencies

- **catalog-perfumes/003 (done):**
  - `catalog.presentations` with `availability` and `is_active`, and their perfume, brand and
    concentration (`modules/catalog/infrastructure/tables.py:21-115`);
  - `SqlPerfumeQueries` and the reader session (`modules/catalog/infrastructure/sql_perfume_queries.py`).
- **identity (done):** `ROLE_PERMISSIONS` (`modules/identity/domain/user.py:83-86`) and
  `require_permission` (`shared/http/access.py`).

## Steps

1. **Register the module and the permission**
   - Files: `docs/modules.json` (modify),
     `apps/api/src/fragancia_api/modules/identity/domain/user.py` (modify),
     `apps/api/tests/integration/identity/test_sql_identity.py` (modify)
   - Do:
     - **`modules.json`:** `inventory` gets `"status": "active"`. The file is append-only:
       change only that field.
     - **`user.py`:** add `INVENTORY_MANAGE = "inventory:manage"` and add it to both roles.
     - **`test_sql_identity.py:367-368`:** these lines pin the exact permission lists. Update
       them to include `inventory:manage`, sorted.
   - Observable result: `GET /api/v1/auth/me` for an owner lists `catalog:manage`,
     `inventory:manage` and `users:manage`.

2. **Catalog facade: `PresentationDirectory`**
   - Files: `apps/api/src/fragancia_api/modules/catalog/application/queries/presentation_directory.py` (create),
     `apps/api/src/fragancia_api/modules/catalog/application/ports.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_presentation_directory.py` (create),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/in_memory_perfumes.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/__init__.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/module.py` (modify)
   - Do:
     - **The type.** `PresentationRef`, a frozen dataclass in the query module:
       `presentation_id`, `perfume_id`, `perfume_name`, `brand_name`,
       `concentration_abbreviation`, `ml`, `availability` (`"in_stock"` or `"made_to_order"`),
       `is_sellable`.
       - `is_sellable` is true when the presentation is active, its perfume is not archived
         and the perfume's brand is active.
     - **The port.** `PresentationDirectoryQueries` in `ports.py`:
       - `find(ids: Sequence[UUID]) -> dict[UUID, PresentationRef]`, where unknown ids are
         absent;
       - `list_in_stock() -> list[PresentationRef]`: every active in-stock presentation of a
         non-archived perfume, ordered by brand name, perfume name and ml.
     - **The facade.** `PresentationDirectory(queries)` delegates `find` and `list_in_stock`.
     - **The SQL adapter** uses `Database.reader()` and joins presentations with perfumes,
       brands and concentrations.
     - **The in-memory version** for unit tests goes in `in_memory_perfumes.py`.
     - **The public API.** `__init__.py` exports `PresentationDirectory` and `PresentationRef`
       in `__all__`, with a docstring line under "Public API".
     - **Registration.** `module.py` registers `PresentationDirectory`.
   - Observable result: `services.get(PresentationDirectory).find([id])` returns the
     presentation's names and kind.

3. **Inventory domain**
   - Files: `apps/api/src/fragancia_api/modules/inventory/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/domain/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/domain/stock.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/domain/errors.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/domain/repositories.py` (create)
   - Do:
     - **`stock.py`:**
       - `MovementKind(StrEnum)`: restock, adjustment, loss, return, count.
       - `Reason.create(raw)`: trimmed, inner whitespace collapsed, 1–200 characters.
       - `Threshold.create(value)`: 0–1000.
       - `QUANTITY_MAX = 100_000`.
       - `StockItem(AggregateRoot)` with `presentation_id`, `on_hand`, `threshold_override`
         (`Threshold | None`), `updated_at`, and `new_movements: list[Movement]`:
         - `StockItem.empty(presentation_id, now)` creates an item at 0.
         - `record(kind, quantity, reason, actor_id, now) -> Result[Movement,
           StockQuantityInvalid | StockInsufficient]` applies the table in Context. A
           quantity outside its range or above `QUANTITY_MAX` → `StockQuantityInvalid`; a
           negative result → `StockInsufficient` (details `on_hand`).
         - `set_threshold(Threshold | None)`.
         - `effective_threshold(global_threshold)` and `is_low(global_threshold)`.
       - `Movement`, a frozen dataclass: `id`, `presentation_id`, `kind`, `quantity` (as
         sent), `delta`, `on_hand_after`, `reason`, `actor_id`, `created_at`.
       - `InventorySettings` with `low_stock_threshold: Threshold`, default 3.
     - **`errors.py`.** Codes `INVENTORY_*`:
       - `INVENTORY_PRESENTATION_NOT_FOUND` (NotFound);
       - `INVENTORY_PRESENTATION_NOT_STOCKED` (BusinessRule: made to order);
       - `INVENTORY_QUANTITY_INVALID`, `INVENTORY_REASON_INVALID` and
         `INVENTORY_THRESHOLD_INVALID` (InvalidValue);
       - `INVENTORY_STOCK_INSUFFICIENT` (Conflict).
     - **`repositories.py`:**
       - `StockRepository`: `get_for_update(presentation_id) -> StockItem | None`, and
         `save(item)`, which upserts the item and inserts `new_movements`;
       - `SettingsRepository`: `get()` and `save(settings)`.
   - Observable result: domain unit tests cover every kind and boundary.

4. **Inventory application**
   - Files: `apps/api/src/fragancia_api/modules/inventory/application/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/application/ports.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/application/commands/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/application/commands/stock.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/application/queries/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/application/queries/stock.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/contracts.py` (create)
   - Do:
     - **`ports.py`:**
       - `PresentationCatalog`, in inventory's terms: `describe(ids) -> dict[UUID,
         StockedPresentation]` and `in_stock() -> list[StockedPresentation]`.
         `StockedPresentation` holds `presentation_id`, `label` (e.g. "Versace Eros EDT
         100 ml"), `perfume_id`, `stocked: bool` and `is_sellable`.
       - `StockQueries`: `get(presentation_id)`, `movements(presentation_id, page, size)` and
         `on_hand_by_id(ids) -> dict[UUID, tuple[int, int | None]]`, which gives on hand and
         the override.
     - **Commands:**
       - `RecordMovement(stock, settings, catalog, transactions, clock).execute(presentation_id,
         request, actor_id)`:
         - an unknown presentation → `NOT_FOUND`; `stocked` false →
           `PRESENTATION_NOT_STOCKED`;
         - otherwise, in a transaction: `get_for_update`, or `StockItem.empty` when there is
           none; `record`; save;
         - returns the movement id and `on_hand`.
       - `SetThreshold(...)`: the same lookup, then lock or create the item, then
         `set_threshold`.
       - `UpdateInventorySettings(...)`: sets the global threshold.
     - **Queries:**
       - `GetStock(presentation_id)`: label, `on_hand`, `threshold_override`,
         `effective_threshold` and `is_low`.
       - `ListMovements(presentation_id, page, size)`: newest first.
       - `ListStock(low: bool, page, size)`: the in-memory join described in Context, ordered
         as the facade returns it.
       - `GetInventorySettings()`.
     - **`contracts.py`.** Pydantic models:
       - `MovementRequest`: `kind`, `quantity` (int), `reason`;
       - `MovementCreated`: `id`, `on_hand`;
       - `StockResponse`, `StockMovement` and `StockMovementPage`;
       - `StockLine` and `StockPage` (each line has `presentation_id`, `perfume_id`, `label`,
         `on_hand`, `effective_threshold` and `is_low`);
       - `ThresholdRequest`: `threshold: int | None`;
       - `InventorySettingsResponse` and `InventorySettingsRequest`.
       - Use the shared `Page` (`shared/contracts`), as catalog does.
   - Observable result: unit tests with fakes cover every path.

5. **Persistence**
   - Files: `apps/api/src/fragancia_api/modules/inventory/infrastructure/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/tables.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/sql_stock_repository.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/sql_stock_queries.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/sql_settings_repository.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/catalog_presentations.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/in_memory.py` (create),
     `apps/api/migrations/versions/0012_inventory_stock.py` (create)
   - Do:
     - **Tables** in schema `inventory`:
       - `stock_items`: `presentation_id` Uuid PK, `on_hand` Integer not null with a check
         constraint `on_hand >= 0`, `threshold_override` Integer null, `updated_at`.
       - `stock_movements`: `id` PK, `presentation_id` (indexed; it may reference a stock item
         within the schema), `kind` String(12), `quantity`, `delta`, `on_hand_after`,
         `reason` String(200), `actor_id` String(64), `created_at`. Add
         `Index("ix_stock_movements_presentation_created", "presentation_id", "created_at")`.
       - `settings`: `id` Integer PK, always 1, and `low_stock_threshold` Integer not null.
     - **Migration.** Per `docs/recipes/db-change.md`: `CREATE SCHEMA inventory` by hand,
       insert the settings row (1, 3), and `DROP SCHEMA` on downgrade. Run
       `uv run just db-revision "inventory stock"`, then rename the file to `0012`.
     - **`get_for_update`** uses `SELECT … FOR UPDATE`. A new item is inserted with
       `ON CONFLICT DO NOTHING`, then locked, so two first movements on the same presentation
       serialize.
     - **`catalog_presentations.py`** implements `PresentationCatalog` with the catalog's
       `PresentationDirectory`. It builds `label` as `"{brand} {perfume} {abbr} {ml} ml"`, and
       `stocked = availability == "in_stock"`.
     - **`in_memory.py`** holds fakes for the unit tests.
   - Observable result: `alembic -x test=true check` is clean, and a downgrade and upgrade
     round trip works.

6. **HTTP and wiring**
   - Files: `apps/api/src/fragancia_api/modules/inventory/http/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/http/router.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/module.py` (create),
     `apps/api/src/fragancia_api/container.py` (modify),
     `apps/api/.importlinter` (modify),
     `apps/api/openapi.json` (modify),
     `docs/architecture.md` (modify)
   - Do:
     - **Router.** Under `admin_router(prefix="/inventory", tags=["inventory"],
       dependencies=[Depends(require_permission(INVENTORY_MANAGE))])`:

       | Route | Answer |
       | --- | --- |
       | `POST /presentations/{presentation_id}/movements` | 201 `MovementCreated`; 404, 409, 422 |
       | `GET /presentations/{presentation_id}` | 200 `StockResponse`; 404 |
       | `GET /presentations/{presentation_id}/movements?page&size` | 200 `StockMovementPage` |
       | `PUT /presentations/{presentation_id}/threshold` | 204; 404, 422 |
       | `GET /stock?low=false&page&size` | 200 `StockPage` |
       | `GET /settings` | 200 |
       | `PUT /settings` | 204; 422 |

       `actor_id` is `request.state.actor.id`. Pages are bounded by `MAX_PAGE` and
       `MAX_PAGE_SIZE`.
     - **`module.py`** registers the commands and queries. It gets the catalog facade with
       `services.get(PresentationDirectory)`.
     - **`container.py`:** `MODULES = (identity, catalog, inventory)`.
     - **`.importlinter`:**
       - add `fragancia_api.modules.inventory` to the `module-layers` containers;
       - under `modules-independent`, add `ignore_imports` with exactly these two lines:
         `fragancia_api.modules.inventory.infrastructure.catalog_presentations ->
         fragancia_api.modules.catalog` and `fragancia_api.modules.inventory.module ->
         fragancia_api.modules.catalog`.
     - Run `uv run just openapi`.
     - **`docs/architecture.md`:** list `inventory` as built, and describe the facade
       crossing.
   - Observable result: `uv run just arch` is green, and the routes appear in
     `/api/v1/docs`.

7. **Tests**
   - Files: `apps/api/tests/unit/inventory/` (create), `apps/api/tests/integration/inventory/`
     (create), `apps/api/tests/unit/catalog/` (modify),
     `apps/api/tests/integration/catalog/` (modify), `apps/api/tests/support.py` (modify)
   - Do: the tester writes the layered tests. `tests/support.py` may grant `inventory:manage`
     to the test admin actor.

## Acceptance criteria

- [ ] **Permissions.** An owner's `/auth/me` lists `inventory:manage`, and so does staff's.
- [ ] **First movement.** `POST /api/v1/admin/inventory/presentations/{id}/movements` with a
  `restock` of 10 on an in-stock presentation that was never counted gives 201 with
  `on_hand: 10`. A row exists in `inventory.stock_items` and one in `inventory.stock_movements`.
- [ ] **Each kind.**
  - A loss of 3 gives `on_hand: 7`, and a return of 1 gives `8`.
  - An adjustment of −2 gives `6`, and a count of 20 gives `20`, recorded as `delta: 14`.
  - A loss of 50 gives 409 `INVENTORY_STOCK_INSUFFICIENT`, and nothing changes.
- [ ] **Refused movements.**
  - A made-to-order presentation gives 422 `INVENTORY_PRESENTATION_NOT_STOCKED`.
  - An unknown id gives 404 `INVENTORY_PRESENTATION_NOT_FOUND`.
  - A quantity of 0 on a restock, or an empty reason, gives 422 with the matching code.
- [ ] **Stock detail.** `GET …/presentations/{id}` shows the label (brand, perfume,
  concentration and ml), `on_hand`, `effective_threshold: 3` and `is_low`. `GET …/movements`
  lists the movements newest first, with actor and reason.
- [ ] **Thresholds.**
  - `PUT …/threshold {"threshold": 25}` makes the effective threshold 25 and `is_low` true at
    20. `{"threshold": null}` falls back to the global value.
  - `PUT /settings {"low_stock_threshold": 5}` changes the global value.
- [ ] **Stock list.** `GET /stock?low=true` lists the low presentations, including in-stock
  presentations never counted (`on_hand: 0`). It excludes made-to-order presentations and
  archived perfumes.
- [ ] **Concurrency.** Two concurrent restocks of 5 on a new presentation end at `on_hand:
  10`, with two movements.
- [ ] **Access.** Without a session, every route answers 401. An admin without
  `inventory:manage` gets 403.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `StockItem.record` for every kind and its boundaries (0, `QUANTITY_MAX`, a result below 0, a count of 0); `Reason` and `Threshold` limits; `effective_threshold` and `is_low` at equality |
| application | yes     | `RecordMovement` and `SetThreshold` paths with fakes: unknown, not stocked, first movement creates the item; settings update; `ListStock` join, filter, pagination and never-counted presentations |
| http        | yes     | Every route: status codes, shapes, error codes, 401 and 403 (`assert_admin_routes_are_protected`), page bounds |
| integration | yes     | Migration round trip and the settings seed row; the SQL repository with `FOR UPDATE` and concurrent first movements; the movements order; the check constraint; the catalog `SqlPresentationDirectory` (`find`, `list_in_stock` filters); identity permissions through `/auth/me` |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

All cosmetic or forced by the plan's own change; none touches the design.

1. **Out-of-scope test edits (forced).** Adding `inventory:manage` to both roles broke
   assertions that pin the exact permission lists, beyond the one the plan names
   (`test_sql_identity.py`). Updated only the expected values in:
   `tests/integration/identity/test_sql_account_links.py`,
   `tests/unit/identity/test_auth_http.py`, `test_resolve_session_actor.py`,
   `test_sessions.py`, `test_team_http.py` and `test_user_domain.py`
   (the last also imports `INVENTORY_MANAGE`). `plans-scope` flags these six files.
2. **`StockItem.record` renamed `record_movement`.** `AggregateRoot.record(event)` already
   exists; a different signature fails mypy strict (`override`).
3. **`StockQueries.low_stock_threshold()` added** (not listed in the plan). Queries cannot use
   `SettingsRepository`, which needs an active transaction; `GetStock`, `ListStock` and
   `GetInventorySettings` read the global threshold through it.
4. **`GET /presentations/{id}` on a made-to-order presentation answers 422
   `INVENTORY_PRESENTATION_NOT_STOCKED`** (the plan lists only 404). It has no unit count to show.
5. **`get_for_update` creates the row in the SQL adapter** (`INSERT ... ON CONFLICT DO NOTHING`,
   then `SELECT ... FOR UPDATE`), so it never returns None there; `save` is an upsert. The fakes
   return None. Commands verify the presentation exists first.
6. **`RecordMovement` receives `settings` but does not use it** (kept as the plan's signature).
7. **Migration**: the autogenerated check-constraint name doubled the convention prefix; named
   it `on_hand_not_negative` in `tables.py`, so the DB name is `ck_stock_items_on_hand_not_negative`.
   Migration written by hand from the autogenerate output.
8. **`tests/support.py` not changed** (declared "may"): the test admin needed no new permission yet.

The dev and test databases were migrated to 0012; `alembic check` is clean and a test-database
downgrade/upgrade round trip worked. Not exercised yet: the SQL repository, queries, the
`SqlPresentationDirectory` and the routes against a running database (the tester's layers).

### Repair round 1 (2026-10-13, main session: verify → implementing)

**Why.** The review left three advisory Lows (L1–L3) and filed the finding
`platform-recipe-facade-wiring-ignore-missing`. The user chose to fix all four before `done`
(README decision 12). The verification of 2026-10-13 stays valid for everything this round
does not touch. The movement and threshold paths are re-verified.

**Repair spec.**

1. **L1: one low-stock rule.**
   - In `domain/stock.py`, add pure functions
     `effective_threshold(override: int | None, global_threshold: int) -> int` and
     `is_low(on_hand: int, override: int | None, global_threshold: int) -> bool`.
   - `StockItem.effective_threshold` and `StockItem.is_low` delegate to them.
   - `application/queries/stock.py` calls them at every place it recomputes the rule today
     (`:32`, `:41`, `:80`, `:88`).
2. **L2: the app clock on the placeholder row.**
   - `StockRepository.get_for_update(presentation_id, *, now: datetime)`. The SQL adapter
     inserts the placeholder with `updated_at=now` instead of `func.now()`
     (`infrastructure/sql_stock_repository.py:40`).
   - The commands pass `self._clock.now()`.
   - The in-memory fake accepts the same signature.
3. **L3: lock the catalog check inside the transaction.**
   - **The facade.** `PresentationDirectory.find_for_update(presentation_id) ->
     PresentationRef | None`. It runs `SELECT … FOR SHARE` on `catalog.presentations`,
     joined as in `find`, through `Database.session`. That joins the caller's active
     transaction, and it raises outside one, as every repository does. It goes in
     `SqlPresentationDirectory` and the port, with an in-memory version.
   - **The port.** Inventory's port gains `PresentationCatalog.describe_for_update(id)`,
     implemented in `catalog_presentations.py`.
   - **The commands.** `RecordMovement` and `SetThreshold` do the lookup *inside*
     `transactions.run(...)`, before locking the stock row.
     - Lock order: the catalog presentation (share), then the stock item.
     - Catalog commands lock perfume and presentation rows and never touch stock rows, so the
       two orders cannot form a cycle.
     - A presentation switched to made to order concurrently now waits, or is seen as
       switched.
4. **Recipe.** `docs/recipes/new-module.md` §5 gains two things:
   - the second `ignore_imports` line, for the consumer's `module.py` when it fetches the
     facade from the registry;
   - the rule that the providing module must come before the consumer in `MODULES`.

   Set the finding `platform-recipe-facade-wiring-ignore-missing` to `resolved`, with `plan:
   inventory-stock/001`.

**Files** (all within this plan's Steps, except the recipe and the finding):
- `modules/inventory/domain/stock.py`
- `modules/inventory/domain/repositories.py`
- `modules/inventory/application/ports.py`
- `modules/inventory/application/commands/stock.py`
- `modules/inventory/application/queries/stock.py`
- `modules/inventory/infrastructure/sql_stock_repository.py`
- `modules/inventory/infrastructure/catalog_presentations.py`
- `modules/inventory/infrastructure/in_memory.py`
- `modules/catalog/application/queries/presentation_directory.py`
- `modules/catalog/application/ports.py`
- `modules/catalog/infrastructure/sql_presentation_directory.py`
- `modules/catalog/infrastructure/in_memory_perfumes.py`
- `docs/recipes/new-module.md`, which is a deviation, recorded

Then set the status to `testing`. The tester adds these regressions:
- the queries and the domain agree on `is_low` at equality;
- the placeholder `updated_at` comes from the clock;
- the facade lookup is made inside the transaction, and an integration test shows a movement
  waiting on a catalog transaction that holds the presentation row.

**Implemented (2026-10-13).**

- **L1.** `domain/stock.py` has pure `effective_threshold(override, global_threshold)` and
  `is_low(on_hand, override, global_threshold)`; `StockItem` delegates to them.
  `queries/stock.py` calls them in `GetStock` and `ListStock`. `GetStock` passes the global
  threshold to `is_low`, not the already-resolved one, so the rule is applied once.
- **L2.** `get_for_update(presentation_id, *, now)` in the port, the SQL adapter (placeholder
  `updated_at=now`; the now-unused `func` import dropped) and the in-memory fake. Both commands
  pass `self._clock.now()`.
- **L3.** `PresentationDirectory.find_for_update` (facade, `PresentationDirectoryQueries` port,
  `SqlPresentationDirectory`, `InMemoryPerfumes`). The SQL form is `SELECT ... FOR SHARE OF
  presentations` through `Database.session`, so it joins the active transaction and raises
  outside one. Inventory's `PresentationCatalog.describe_for_update` is implemented in
  `catalog_presentations.py` and the in-memory catalog. `RecordMovement` and `SetThreshold` now
  do the lookup inside `transactions.run(...)`, before locking the stock row. Input validation
  stays outside the transaction.
- **Recipe.** `docs/recipes/new-module.md` §5 lists both `ignore_imports` lines and the
  `MODULES` ordering rule. The finding `platform-recipe-facade-wiring-ignore-missing` is
  `resolved` (`plan: inventory-stock/001`) with a Resolution section.
- **Existing tests adapted** (signature only): `tests/integration/inventory/test_sql_stock.py`
  nine `repo.get_for_update(...)` calls now pass `now=NOW` (lines 83, 105, 112, 132, 175, 196,
  275, 396, 400). No assertion changed.
- **Deviation:** `docs/recipes/new-module.md` and the finding file are outside the Steps' file
  list, as the spec itself says.
- **Commands:** `uv run just check` green (2002 unit, 685 tooling); `uv run just
  test-integration` 507 passed, 5 skipped. The API was not started.

### Repair round 2 (2026-10-13, main session: verify → implementing)

**Why.** The re-review found L4. A movement's timestamp is read *before* the command waits on
the stock-row lock (`application/commands/stock.py:69-74`). Two concurrent movements can
therefore be recorded with timestamps out of lock order, and the ledger (newest first) shows
`on_hand_after` out of sequence. The user's standing choice in this plan is to fix every
review Low (README decision 12), so it is fixed here. Only the movement path is re-verified.

**Repair spec.** Product code is `application/commands/stock.py` only.
1. **Movements.** In `RecordMovement`, keep `now` for the placeholder that `get_for_update`
   may insert. After `get_for_update` returns, which means the lock is held, read
   `self._clock.now()` again. Use that second value for `StockItem.empty` and
   `record_movement`.
2. **Thresholds.** Do the same in `SetThreshold` for `updated_at`.
3. **Comment.** Add one line explaining that the timestamp is taken under the lock, so the
   ledger order matches the order in which movements were applied.

The informational notes stay as they are: the facade name `find_for_update` and the catalog
`__init__` docstring.

Then set the status to `testing`. The tester adds two regressions:
- a unit test with a clock that advances on each call, showing that the movement time is read
  after the lock;
- an integration test with two concurrent movements, where `on_hand_after`, ordered by
  `created_at`, is monotonic with the application order.

**Implemented (2026-10-13).** Only `application/commands/stock.py`. In `RecordMovement` and
`SetThreshold`, the first clock read feeds the placeholder of `get_for_update`; a second read
after it returns (lock held) feeds `StockItem.empty`, `record_movement` and `updated_at`, with a
comment saying so. No test needed adapting. `uv run just check` and `uv run just
test-integration` run after the change (results below).

## Test coverage

Baseline (before tests): `just check` green (1869 unit, 685 tooling), `just test-integration`
477 passed, 5 skipped. Closing: `just check` green (2002 unit, 685 tooling), `just
test-integration` 507 passed, 5 skipped. Added 133 unit and 30 integration tests; no GAP found
and no product code touched. Test admin for the http layer: a dedicated
`InventoryActorResolver` in the http test (`inventory-manager` holds `inventory:manage`; the shared
`ADMIN_HEADERS` actor holds only `catalog:manage`, so it doubles as the 403 case);
`tests/support.py` unchanged.

| Behavior | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| Every kind applies its delta (restock, return, loss, adjustment +/-, count) | `domain/stock.py` `record_movement` | domain | `unit/inventory/test_stock_domain.py::test_each_kind_changes_on_hand_as_the_ledger_table_says` | CONFIRMED |
| Quantity ranges, 0 and `QUANTITY_MAX` boundaries, count of 0, result below 0 refused | `domain/stock.py` `_quantity_valid` | domain | `test_stock_domain.py` (quantity range, boundaries, below-zero tests) | CONFIRMED |
| `Reason` 1-200 trimmed/collapsed; `Threshold` 0-1000; `effective_threshold` / `is_low` at equality | `domain/stock.py` | domain | `test_stock_domain.py` (reason, threshold, is_low tests) | CONFIRMED |
| `RecordMovement`: unknown, not stocked, first movement creates item, invalid reason/quantity, insufficient leaves nothing | `commands/stock.py` | application | `unit/inventory/test_stock_commands.py` | CONFIRMED |
| `SetThreshold` (creates item at 0, null clears, range, unknown/not stocked); `UpdateInventorySettings` | `commands/stock.py` | application | `test_stock_commands.py` | CONFIRMED |
| `GetStock`, `ListMovements`, `ListStock` (join, never-counted at 0, low filter, override, pagination after filter, catalog order), `GetInventorySettings` | `queries/stock.py` | application | `unit/inventory/test_stock_queries.py` | CONFIRMED |
| Facade `PresentationDirectory` over the in-memory queries (find, order, filters) | `catalog/.../presentation_directory.py`, `in_memory_perfumes.py` | application | `unit/catalog/test_presentation_directory.py` | CONFIRMED |
| Anti-corruption adapter: label, `stocked`, omit unknown | `infrastructure/catalog_presentations.py` | application | `unit/inventory/test_catalog_presentations.py` | CONFIRMED |
| All 7 routes: 401 without session, 403 without `inventory:manage`, shapes, 201/204, 404/409/422 codes, page bounds, malformed bodies | `http/router.py` | http | `unit/inventory/test_stock_http.py` (+ `assert_admin_routes_are_protected`) | CONFIRMED |
| `GET /presentations/{id}` on made-to-order answers 422 (Deviation 4) | `queries/stock.py` `GetStock` | http | `test_stock_http.py::test_get_stock_errors` | CONFIRMED |
| Two concurrent first movements end at `on_hand` 10 with two movements | `sql_stock_repository.py` `get_for_update` | integration | `integration/inventory/test_sql_stock.py::test_two_concurrent_first_movements_serialize_on_the_row_lock` (forced lock overlap) and `::test_two_concurrent_restocks_through_the_use_case_end_at_ten` (real container + catalog) | CONFIRMED |
| Concurrent losses cannot go below 0 | same | integration | `::test_concurrent_losses_cannot_drive_the_stock_below_zero` | CONFIRMED |
| Repository: lazy create, field round trip, rollback leaves nothing | `sql_stock_repository.py` | integration | `test_sql_stock.py` (first lock, second load, every field, failed transaction) | CONFIRMED |
| Check constraint `ck_stock_items_on_hand_not_negative` (insert, update, via repository) | `tables.py`, migration 0012 | integration | `test_sql_stock.py::test_the_database_refuses_a_negative_on_hand` and 2 more | CONFIRMED |
| `SqlStockQueries`: movements newest first, ties by id desc, pagination, per-presentation, `get`, `on_hand_by_id` | `sql_stock_queries.py` | integration | `test_sql_stock.py` (movements and get tests) | CONFIRMED |
| Settings: seed row (1, 3), upsert, use case persists | `sql_settings_repository.py` | integration | `test_sql_stock.py` (settings tests) | CONFIRMED |
| Stock list / override / made-to-order over the real catalog and database | `queries/stock.py` + `SqlPresentationDirectory` | integration | `test_sql_stock.py::test_the_stock_list_joins_...` and 2 more | CONFIRMED |
| `SqlPresentationDirectory`: `find` (unknown omitted, made to order, `is_sellable` false for inactive presentation / archived perfume / inactive brand) | `sql_presentation_directory.py` | integration | `integration/catalog/test_sql_presentation_directory.py` | CONFIRMED |
| `list_in_stock`: only active in-stock of non-archived perfumes; ordered brand, perfume, ml ignoring case | same | integration | same file | CONFIRMED |
| `list_in_stock` includes presentations of an inactive brand, flagged `is_sellable=false` (the code filters only active + in-stock + non-archived, matching the docstring; the user's brief listed "inactive brand" as a filter) | same | integration | `::test_list_in_stock_includes_presentations_of_an_inactive_brand_flagged_unsellable` | CONFIRMED (as implemented; confirm intent in review) |
| Identity: `/auth/me` and role permission lists include `inventory:manage` | `identity/domain/user.py` | integration | updated by the implementer in `integration/identity/test_sql_identity.py` (not rewritten here) | CONFIRMED |
| Migration downgrade/upgrade round trip | `migrations/versions/0012_inventory_stock.py` | integration | none: a repeatable test would downgrade the shared test schema; only the implementer's manual run (Deviations) backs it | NOT CONFIRMED by a test |
| Routes against the real database through the real container and session | `main/`, `container.py` | integration | none: real sessions need an identity login flow; unit http + SQL layers cover the halves, `tests/unit/test_container.py` covers wiring | NOT CONFIRMED (verify phase) |

### Repair round 1

Tests only; no product code touched. Baseline for this round (commit 7248d24, implementer
numbers): `just check` 2002 unit, `just test-integration` 507 passed. Closing: `just check`
green, `just test-integration` 519 passed, 5 skipped (+12 integration, +12 unit).

| Behavior | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| L1: `GetStock`, `ListStock` and `StockItem` agree on `is_low` at equality, with and without an override; the pure `is_low` / `effective_threshold` | `domain/stock.py`, `queries/stock.py` | application | `unit/inventory/test_repair_round_1.py::test_queries_and_domain_agree_on_is_low` (8 cases), `::test_the_pure_rule_functions` | CONFIRMED |
| L1 over SQL: the same agreement through the real container's queries | same | integration | `integration/inventory/test_sql_repair_round_1.py::test_sql_backed_queries_flag_low_at_equality_like_the_domain` | CONFIRMED |
| L2: both commands pass the injected clock to `get_for_update(now=)` | `commands/stock.py` | application | `test_repair_round_1.py::test_commands_pass_the_injected_clock_to_the_stock_lock` | CONFIRMED |
| L2: the placeholder row's `updated_at` equals the given `now` in PostgreSQL; first movement and first threshold keep the fixed clock | `sql_stock_repository.py` | integration | `test_sql_repair_round_1.py::test_the_placeholder_row_is_stamped_with_the_given_clock`, `::test_a_first_movement_and_a_first_threshold_keep_the_injected_clock` | CONFIRMED |
| L3: the locked catalog lookup runs inside the transaction (and `describe` is no longer used) | `commands/stock.py` | application | `test_repair_round_1.py::test_the_catalog_lookup_is_locked_and_inside_the_transaction` (record, threshold) | CONFIRMED |
| L3: `find_for_update` raises outside a transaction, reads and returns None for unknown inside one | `sql_presentation_directory.py` | integration | `test_sql_repair_round_1.py::test_find_for_update_raises_outside_a_transaction`, `::test_find_for_update_inside_a_transaction_reads_the_presentation` | CONFIRMED |
| L3: a movement / threshold waits while another transaction holds the presentation `FOR UPDATE`; after it commits a switch to made_to_order the command answers `INVENTORY_PRESENTATION_NOT_STOCKED` and writes nothing; after a rollback it proceeds | same + `catalog_presentations.py` | integration | `::test_a_command_waits_for_a_catalog_transaction_and_sees_the_switch_to_made_to_order` (movement, threshold), `::test_a_command_proceeds_once_the_catalog_transaction_ends_unchanged` | CONFIRMED |

The wait is asserted with a 0.7 s timeout (the task must still be pending), so it depends on
the lock, not on timing for the later outcome.

### Repair round 2

Tests only; no product code touched. Closing: `just check` and `just test-integration` green
(counts in the chat report).

| Behavior | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| L4: with a clock that advances per call, the movement `created_at` and the item `updated_at` are the read taken after `get_for_update`; the first read only feeds the lock placeholder; a second movement does the same | `commands/stock.py` `RecordMovement` | application | `unit/inventory/test_repair_round_2.py::test_the_movement_time_is_read_after_the_stock_lock`, `::test_a_second_movement_on_an_existing_item_also_uses_the_post_lock_time` | CONFIRMED |
| L4: `SetThreshold` `updated_at` (first threshold creates the item) is the post-lock read | `commands/stock.py` `SetThreshold` | application | `::test_the_threshold_updated_at_is_read_after_the_stock_lock` | CONFIRMED |
| L4 on PostgreSQL: movement B reads the clock first but locks second (a gate holds it just before `get_for_update`); ordered by `created_at`, `on_hand_after` follows the application order (7 then 12) | same + `sql_stock_repository.py` | integration | `integration/inventory/test_sql_repair_round_2.py::test_ledger_order_by_created_at_follows_the_order_movements_were_applied` | CONFIRMED (by reasoning, it fails on the pre-repair code: B would get the earlier timestamp and sort first) |

## Review findings

Reviewed 2026-10-13 against `main...HEAD` (head `d606249`, clean worktree).

**Checklist: 14/15 passed, 1 N/A, 0 failed.**

- [x] `plans-scope`: the only out-of-scope files are the six identity test files named in
  Deviation 1. Each diff only changes expected permission values (plus the
  `INVENTORY_MANAGE` import in `test_user_domain.py`), so it is forced and honest.
  `tests/support.py` is declared but unchanged (Deviation 8). The hot files are append-only:
  `.importlinter` adds one container line and the two planned `ignore_imports` lines;
  `container.py` adds one import and appends `inventory` to `MODULES`; `modules.json` flips
  only `inventory.status`.
- [x] `uv run just check`: green (2002 unit, 3 skipped; 685 tooling; import-linter 7 kept,
  with "2 ignored imports" on the independence contract).
- [x] `uv run just test-integration`: green (507 passed, 5 skipped).
- [x] Business rules are in `domain/stock.py` (kinds, ranges, never negative, reason,
  threshold). Exception: the query layer recomputes the low rule (L1), but it is not in a
  router, mapper or query adapter.
- [x] CQRS-lite: `RecordMovement`, `SetThreshold` and `UpdateInventorySettings` run
  aggregate → repository inside `TransactionRunner.run` and return `Result`. Queries go
  through `StockQueries` (reader sessions) and return contracts. Repositories have no
  screen-specific methods.
- [x] Contracts: all come from `inventory/contracts.py`, and `Page` is shared.
  `python -m fragancia_api.main.openapi --check` reports the committed file is up to date.
- [x] Errors: `Err(DomainError)` with `INVENTORY_*` codes, and the router uses `unwrap`.
  Catalog lookups refuse before the transaction.
- [x] Time, money and ids: no money is involved. Ids come from `new_id()`. The aggregate's
  datetimes come from `Clock`, except for one adapter placeholder (L2).
- [x] Migration `0012` is new. It runs `CREATE SCHEMA inventory`, seeds the settings row
  (1, 3), has no foreign keys and no drops in upgrade, and its downgrade drops in reverse
  order. Its check constraint `ck_stock_items_on_hand_not_negative` matches `tables.py`
  (`on_hand_not_negative` plus the naming convention, Deviation 7). The round trip rests on
  the implementer's manual run; `alembic check` is clean.
- [x] Routes: all seven are on `admin_router(prefix="/inventory", dependencies=[require_permission("inventory:manage")])`.
  `admin_router` puts `require_admin` first, so a request with no session gets 401 before the
  permission check. `assert_admin_routes_are_protected` is in `test_stock_http.py`.
- [x] Wiring: `inventory` is registered once, after `catalog`, and `module.py` fetches
  `PresentationDirectory` from the registry. Adapters are created only in `module.py`.
  `test_container.py` is green.
- [x] No secrets or real personal data in the code, tests or plan.
- [x] `## Deviations` is honest. I spot-checked 2 (`record_movement`), 3
  (`low_stock_threshold` on `StockQueries`), 5 (`get_for_update` inserts, then locks) and 7
  (the constraint name).
- [x] Docs: `docs/architecture.md` lists inventory as built and describes the facade
  crossing; `docs/modules.json` is updated. `docs/recipes/new-module.md` §5 was already
  incomplete before this plan. It is outside this plan's file list, so I filed it as a finding
  (see below).
- [ ] N/A: no PR exists yet. The main session must write the six-section body when it opens one.

**The areas I was asked to focus on:**

- **Module boundaries.** Clean. Inventory reaches the catalog only through
  `fragancia_api.modules.catalog` (`__init__` exports `PresentationDirectory` and
  `PresentationRef`), from exactly the two allowed modules
  (`infrastructure/catalog_presentations.py` and `module.py`). It never reads `catalog.*`
  tables, and `stock_items.presentation_id` has no foreign key. The anti-corruption layer
  translates `PresentationRef` into `StockedPresentation` and builds the label in inventory's
  terms.
- **Stock arithmetic and never-negative.** Correct. The deltas are:

  | Kind | Delta |
  | --- | --- |
  | restock, return | +q |
  | loss | −q |
  | adjustment | q (signed, ≠ 0) |
  | count | q − on_hand (q ≥ 0) |

  The quantity is validated before the delta is computed. `on_hand_after < 0` →
  `StockInsufficient` without touching any state. The check constraint backs this up in the
  DB, and three integration tests cover it (raw insert, raw update, repository save).
  `SqlTransactionRunner` rolls back on `Err`, so a refused first movement leaves no
  placeholder row behind, as "nothing changes" requires.
- **Locking.** Correct under the default READ COMMITTED isolation:
  1. A concurrent `INSERT … ON CONFLICT DO NOTHING` on the same primary key waits for the
     first transaction's uncommitted insert, then does nothing.
  2. The following `SELECT … FOR UPDATE` takes a new snapshot, sees the committed row, and
     queues on its lock.

  Each transaction locks only one row, so there is no deadlock path. The integration tests
  force the lock overlap and also race the real use case; concurrent losses cannot go below 0.
- **Stock list.**
  1. The catalog's ordered list is joined with `on_hand_by_id` in a single `IN` query.
  2. Never-counted presentations default to `(0, None)`.
  3. The threshold is the override or else the global value, and the `low` filter is applied
     before slicing, so `total` counts the filtered lines.
  4. The page and size are bounded by `MAX_PAGE` and `MAX_PAGE_SIZE` at the router, and a page
     past the end returns an empty page.
- **Permission wiring.** `inventory:manage` is in both roles. Permissions are derived from the
  role on every request (`User.permissions`, used by `resolve_session_actor`), so existing
  sessions pick it up without logging in again. The router keeps its own string constant, as
  catalog does.

**The tester's question (an inactive brand in `list_in_stock`): conforms to the plan.** The
plan defines `list_in_stock` three times, always with the same two filters and none on the
brand:

- Context, "The stock list": "every active in-stock presentation of a non-archived perfume".
- Step 2: the same wording.
- Acceptance: "excludes made-to-order presentations and archived perfumes".

The brand's status appears only in `is_sellable`, and the code does exactly that
(`catalog/infrastructure/sql_presentation_directory.py:66-70`). This is also sensible for a
ledger: the bottles exist whatever the brand's storefront status. If the user wants such
presentations hidden, that is a plan change, not a defect.

**Findings by severity:**

- **Blocking (critical, high, medium): none.**
- **Low, advisory (does not block):**
  - **L1: the low rule is duplicated outside the domain.**
    - Where: `inventory/application/queries/stock.py:32,41` and `:80,88`.
    - What fails: `GetStock` and `ListStock` recompute the override-or-global threshold and
      `on_hand <= threshold` inline. They do not use `StockItem.effective_threshold` and
      `is_low` (`domain/stock.py:174-179`).
    - Failure scenario: decision 9 makes this one threshold drive the list, the email (004) and
      "¡Últimas piezas!" (002). If one copy changes (for example, to `<` or to a threshold of
      0 meaning "never low"), the panel and the alert or storefront disagree.
    - Suggestion: put a pure domain function behind both copies, before 002 or 004 adds a third.
  - **L2: the placeholder row takes its timestamp from the database, not from `Clock`.**
    - Where: `inventory/infrastructure/sql_stock_repository.py:40`.
    - What fails: the lock placeholder sets `updated_at=func.now()`. In `SetThreshold`, the
      aggregate is rebuilt from that row (`:55`), and `set_threshold` does not touch
      `updated_at`. So a presentation whose first write is a threshold is persisted with the
      DB's time. On the fake path, `SetThreshold` instead uses `self._clock.now()`
      (`commands/stock.py:117`), so the two paths diverge.
    - Failure scenario: none visible today, since the value is UTC-correct and nothing reads
      it. It breaks the "datetimes via `Clock`" convention, and it would surface under a
      frozen-clock test that asserts `updated_at`.
  - **L3: a time-of-check gap between the catalog check and the lock (uncertain whether it
    matters).**
    - Where: `inventory/application/commands/stock.py:63,110`.
    - What fails: `_require_stocked` runs before the transaction opens, so a presentation
      switched to made-to-order in between still gets its movement or threshold. There is no
      cross-module transaction, so this is inherent to the design. It only surfaces if an
      admin changes the availability and records stock at the same moment. Accept it.
- **Informational:**
  - **The router tag differs from the plan.** The plan says `tags=["inventory"]`; the code
    uses `"inventory · admin"` (`http/router.py:41`), which follows catalog's convention.
    Cosmetic and undeclared.
  - **Inconsistent handling of made-to-order presentations.** `ListMovements` answers 200
    with an empty page for them, while `GetStock` answers 422 (Deviation 4). Harmless.
  - **A redundant index.** `stock_movements` has both `ix_stock_movements_presentation_id`
    (`tables.py:25`, `index=True`) and the composite index that starts with the same column.
    The plan asked for both; the single-column one is redundant.
  - **Unused fields.** `StockedPresentation.is_sellable` is not used yet, and
    `RecordMovement._settings` is never used (Deviation 6). Both are dead until 002 or 004
    needs them.

**Out of scope:** `plans/findings/platform-recipe-facade-wiring-ignore-missing.md`. The
new-module recipe §5 lists only the adapter's `ignore_imports` line. It omits the `module.py`
line and the `MODULES` ordering that any facade crossing needs.

**Not verified by this review:**

- I did not repeat the migration round trip (it would downgrade the shared test schema).
- I did not exercise the routes against the running app; that is the verify phase.

Result: no blocking findings → `status: verify`.

### Re-review after repair round 1

Reviewed 2026-10-13 against `git diff cff1c72..HEAD` (head `be01339`: `7248d24` fix and
`be01339` regressions; clean worktree). The review above stays as the record of round 0.
Where it conflicts with this section, this section wins: L1, L2 and L3 above are superseded
by the repair.

**Checklist (the items the repair touches): all pass.**

- [x] `plans-scope`: the only new out-of-scope file is `docs/recipes/new-module.md`. The
  repair spec names it as a recorded deviation, and README decision 12 authorizes it. The
  finding file is updated as the spec asks (`resolved`, `plan: inventory-stock/001`). The hot
  files are unchanged by the repair.
- [x] `uv run just check`: green (2014 unit, 3 skipped; 685 tooling; import-linter 7 kept,
  0 broken). The `ignore_imports` set is unchanged, because the new facade method goes
  through the same two allowed imports.
- [x] `uv run just test-integration`: green (519 passed, 5 skipped).
- [x] `openapi --check`: up to date (no contract changed).
- [x] Business rules: the low-stock rule now lives only in `domain/stock.py`. The pure
  functions `effective_threshold` and `is_low` are used by both `StockItem` and the queries
  (`queries/stock.py`). L1 is resolved.
- [x] Clock: the placeholder row takes `now` from the command's `Clock`
  (`sql_stock_repository.py`, `updated_at=now`), and the `func` import is gone. L2 is resolved.
- [x] CQRS-lite and errors: the catalog check now runs inside `transactions.run(...)`, so a
  refusal (`NOT_FOUND`, `NOT_STOCKED`) is an `Err` that rolls the transaction back.
  Validating the reason and the threshold stays outside the transaction, which is cheap and
  takes no lock.

**L3: lock order and the `FOR SHARE` lookup.**

- **It joins the active transaction.** `SqlPresentationDirectory.find_for_update`
  (`catalog/infrastructure/sql_presentation_directory.py:65-73`) runs on
  `Database.session`, which returns the context-bound unit-of-work session and raises
  `RuntimeError` outside one (`shared/infrastructure/database.py:25-34`). It is called only
  from `_require_stocked`, which now runs inside `work()` in both commands
  (`inventory/application/commands/stock.py`, `RecordMovement` and `SetThreshold`). So the
  share lock lives exactly as long as the inventory transaction. The integration test
  `test_find_for_update_raises_outside_a_transaction` pins the "raises" half.
- **The lock covers only the presentation row.** It is `FOR SHARE OF presentations`. The
  joined perfume, brand and concentration rows are read through MVCC and not locked, so a
  catalog transaction holding `perfumes … FOR UPDATE` does not block the lookup.
- **No deadlock path with the catalog commands.**
  - **The catalog's lock order.** `SqlPerfumeRepository` locks the perfume row
    (`get_for_update`, `sql_perfume_repository.py:213-215`), then `save` updates the perfume
    and upserts each presentation (`:243-264`, row locks on `catalog.presentations`). New
    presentation rows also take `FOR KEY SHARE` on their own perfume through the foreign key.
    The catalog never touches `inventory.*`.
  - **Inventory's lock order.** An inventory transaction takes, in order:
    1. `FOR SHARE` on one presentation;
    2. the `stock_items` placeholder insert and `FOR UPDATE` on the same presentation id.
  - **Why no cycle can form.** The only resource both sides contend for is the presentation
    row.
    - The catalog waits on inventory only after inventory already holds that row.
    - After that, inventory asks only for `stock_items` locks, which no catalog transaction
      ever holds.
    - Before it takes that row, inventory holds nothing.

    So an inventory → catalog → inventory wait cycle cannot form.
  - **Inventory against inventory.** Every path uses the same order (presentation, then
    stock item). Share locks are compatible with each other, and `FOR SHARE` requests that
    queue behind a waiting catalog writer hold no stock lock yet. So that interleaving
    cannot cycle either.
- **Correctness after the wait.** Under READ COMMITTED, PostgreSQL re-reads the locked
  presentation row after the blocker commits. The command therefore sees a committed switch
  to made-to-order (`NOT_STOCKED`, nothing written) or a deleted row (`None` →
  `NOT_FOUND`). The joined, unlocked columns may come from the statement's snapshot. That
  does not matter, because `stocked` depends only on the presentation's `availability`.
  L3 is resolved for movements and thresholds.

**The lock regression would fail without the lock: confirmed.** I ran a mutant from the
scratchpad without editing the worktree:

- **The mutant.** A pytest plugin passed to `pytest.main(plugins=[...])` replaced
  `SqlPresentationDirectory.find_for_update` with the same query on the same session, minus
  `with_for_update`.
- **The result.** `test_sql_repair_round_1.py` failed 3 of 12:
  - both parameters of `test_a_command_waits_for_a_catalog_transaction_and_sees_the_switch_to_made_to_order`
    failed at line 191 ("the command should wait on the row lock");
  - `test_a_command_proceeds_once_the_catalog_transaction_ends_unchanged` failed at line 228.
- **The control.** The same runner without the plugin passed 12 of 12.

The 0.7 s "still pending" assertion depends on the lock alone. Without the lock the command
finishes well inside the window.

**Findings after the repair:**

- **Blocking (critical, high, medium): none.**
- **Low, advisory (missed in round 0, not introduced by the repair):**
  - **L4: a movement's `created_at` is read before the stock-row lock wait.**
    - Where: `inventory/application/commands/stock.py:69-74`. `now` is taken, then
      `get_for_update` may wait on another movement's lock, and the movement is stamped with
      the earlier `now`.
    - What fails: two concurrent movements, A taking `now = t1` and B taking `now = t2 > t1`.
      If B wins the lock, B is applied first (`on_hand_after` 5) but carries the later time,
      and A is applied second (`on_hand_after` 10) with the earlier time.
    - Failure scenario: the ledger, listed newest first by `created_at`
      (`sql_stock_queries.py`), shows 5 above 10, so its running `on_hand_after` column reads
      out of sequence. The final `on_hand` is still correct. It needs genuinely concurrent
      writes on one presentation, so it is rare in a back office.
    - Why the tests miss it: `test_two_concurrent_restocks_through_the_use_case_end_at_ten`
      checks `sorted(on_hand_after)` only.
    - Suggestion: stamp the movement with a clock reading taken after `get_for_update`
      returns. The placeholder can keep the earlier one.
- **Informational:**
  - The facade method is named `find_for_update`, but it takes a share lock (`FOR SHARE`).
    The docstrings say "share lock", so the name is only slightly misleading.
  - The docstring of `catalog/__init__.py` ("Public API") does not mention the locking
    lookup. It still lists the facade type correctly.
  - The informational notes from round 0 (the router tag, made-to-order movements, the
    redundant index, the unused fields) are unchanged.

**Not verified:** I did not exercise the repaired commands in the running app. The existing
`## Verification` below predates `7248d24`, so it does not cover the repaired code and must be
repeated for it.

Result: no blocking findings → `status: verify`. L4 is advisory; the user decides whether to
fix it before `done` or file it.

### Re-review after repair round 2

Reviewed 2026-10-13 against `git diff 65bcc63..HEAD` (`f5622f2` fix, `c504abc`
regressions; clean worktree). This round superseded L4 above.

**L4 is resolved.**

- **The fix.** In `inventory/application/commands/stock.py`, `RecordMovement` and
  `SetThreshold` now read the clock twice. The first read only stamps the `get_for_update`
  placeholder. The second read happens after the lock is held, and it feeds
  `StockItem.empty`, `record_movement` (`created_at`) and `updated_at`.
- **Why it works.** For one presentation, writers serialize on the stock row, so their
  `created_at` order now follows the order in which they were applied (as long as the
  system clock does not go backwards).
- **Scope.** Nothing else changed: lock order, the transaction boundary and the
  catalog `FOR SHARE` lookup are as reviewed in round 1. In `SetThreshold`, the post-lock
  read is unused on the SQL path, which always returns a row. That is harmless.

**The new tests fail on the pre-repair code: confirmed.** I ran a mutant from the scratchpad
without editing the worktree:

- **The mutant.** I extracted `65bcc63:.../commands/stock.py` with `git show` into the
  scratchpad. A pytest plugin, passed to `pytest.main(plugins=[...])`, put that file's
  `RecordMovement.execute` and `SetThreshold.execute` onto the live classes.
- **The result.** All 4 new tests failed:
  - the integration test
    `test_ledger_order_by_created_at_follows_the_order_movements_were_applied` got
    `[("first-to-read", 12), ("second-to-read", 7)]`;
  - the 3 unit tests in `test_repair_round_2.py` got the placeholder's timestamp instead of
    the read taken after the lock.
- **The control.** The same runner without the plugin passed 4 of 4.

This replaces the tester's "by reasoning" note in the round 2 coverage table with an
observed failure.

**Gates on `c504abc`:**

- `uv run just check`: green (2017 unit, 685 tooling; import-linter 7 kept).
- `uv run just test-integration`: 520 passed, 5 skipped.
- `plans-scope`: the out-of-scope set is the same as in round 1. It is all accounted for
  (Deviation 1 and the recipe, per decision 12).

**Findings:** none new. The informational notes from rounds 0 and 1 are unchanged.

**Not verified:** I did not run the repaired commands in the running app. The verify phase
must cover `7248d24` and `f5622f2`.

Result: no blocking findings → `status: verify`.

## Verification

**2026-10-13, verifier (main session, inline).**

**Setup.** Branch `feat/inventory-stock` at `dff0610`. The dev DB was migrated to `0012`
(`uv run just db-migrate`). `uv run just api` ran with default settings, as the owner
`verifier007@example.test`, against the QA perfumes left by the catalog plans. The reviewer
reports `uv run just check` green (2002 unit, 685 harness) and `test-integration` at 507
passed, 5 skipped.

**Acceptance criteria** (scratchpad `qa_inv/qa_inv.py`, real requests to port 8100):
- [x] **Permissions.** `GET /api/v1/admin/auth/me` for the owner lists `['catalog:manage',
  'inventory:manage', 'users:manage']`. Staff was not driven live; the identity tests pin it.
- [x] **First movement.** "QA Confirma" 50 ml had no stock row (0). A restock of 10 gave 201
  `on_hand: 10`.
- [x] **Each kind.**
  - A loss of 3 gave 7, a return of 1 gave 8, and an adjustment of −2 gave 6.
  - A count of 20 gave 20, stored with `delta 14` and `on_hand_after 20`.
  - A loss of 50 gave 409 `INVENTORY_STOCK_INSUFFICIENT`. The row stayed at 20 with 5
    movements.
- [x] **Refused movements.**
  - A made-to-order presentation (created for QA: 200 ml, 7–10 days) gave 422
    `INVENTORY_PRESENTATION_NOT_STOCKED`.
  - An unknown id gave 404 `INVENTORY_PRESENTATION_NOT_FOUND`.
  - A restock of 0 gave 422 `INVENTORY_QUANTITY_INVALID`, and a blank reason gave 422
    `INVENTORY_REASON_INVALID`.
- [x] **Stock detail.** The label is "QA Versace QA Confirma EDP 50 ml", with `on_hand 20`,
  `effective_threshold 3` and `is_low false`. The movements page has `total 5`, newest first
  (count, adjustment, return), each with its reason and the actor id.
- [x] **Thresholds.**
  - An override of 25 gave 204, effective 25 and `is_low true`. `null` gave 204 and back to 3,
    `is_low false`.
  - Setting the global value to 5 gave 204, and the settings read `{'low_stock_threshold': 5}`.
    It was restored to 3 (204).
- [x] **Stock list.**
  - `low=true` gives `total 2`: "QA Confirma EDP 100 ml" and "QA Existente EDT 100 ml", both
    never counted, `on_hand 0`.
  - The full list has 3 lines. It holds neither the made-to-order presentation nor the
    archived perfume's.
- [x] **Concurrency.** Two concurrent first restocks of 5 on "QA Confirma" 100 ml, which had no
  row before, both gave 201 and ended at `on_hand 10` with 2 movements.
- [x] **Access.** All 7 routes return 401 without a session. The 403 case (an admin without
  `inventory:manage`) is **NOT VERIFIED** live, because both roles have the permission. It is
  covered by `tests/unit/inventory/test_stock_http.py`.

**Leftover QA data (dev DB):** stock rows for "QA Confirma" 50 ml (20) and 100 ml (10), and a
made-to-order 200 ml presentation `01a13650-dc1a-72ad-a151-09990a99d962` on "QA Confirma".
Nothing is deleted in the catalog. The API is stopped and port 8100 is free.

**Result:** every acceptance criterion that can be exercised locally passes. This run is
superseded for the movement and threshold paths by the re-verification below.

**2026-10-13, re-verification after repair rounds 1 and 2 (main session, inline).**

Branch at `7cf5e01`, which holds `7248d24` and `f5622f2`. The reviewer reports `uv run just
check` (2017 unit, 685 harness) and `test-integration` (520 passed, 5 skipped). `uv run just
api` ran with default settings. I created fresh in-stock presentations for this run: 30 ml
and 75 ml on "QA Confirma" (scratchpad `qa_inv/qa_inv2.py`).
- **Movements on a never-counted presentation:**
  - restock 10 gave 201 and 10; loss 3 gave 7; return 1 gave 8; adjustment −2 gave 6;
    count 20 gave 20;
  - a loss of 50 gave 409 `INVENTORY_STOCK_INSUFFICIENT`.
  - The ledger ordered by `created_at` is restock(10), loss(7), return(8), adjustment(6),
    count(Δ14 → 20), in the order the movements were applied.
- **L1 at equality.** With an override of 20 and `on_hand` 20, the detail and the stock list
  both say `is_low true` with an effective threshold of 20.
- **L2/L4: concurrent first restocks** of 5 and 7 on a new presentation both gave 201, with
  final `on_hand` 12.
  - Ordered by `created_at`, the ledger is `[(5, 5), (7, 12)]`, so `on_hand_after` is
    monotonic.
  - The stock row's `updated_at` equals the last movement's `created_at`, which the app clock
    took under the lock.
- **L3.** The lock wait on a catalog transaction is not reproducible by hand through HTTP. It
  is covered by the integration tests, which the reviewer confirmed fail without the lock.
- The earlier checks still hold: the error codes, the refusal of made-to-order and unknown
  presentations, the list filters, and 401 on every route.
- The API is stopped and port 8100 is free. More QA data is left in the dev DB: the two fresh
  presentations above, with their stock.

**Result:** every acceptance criterion passes on the repaired code. Ready for the user to mark
`done`.
