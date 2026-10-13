---
status: approved
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

## Test coverage

## Review findings

## Verification
