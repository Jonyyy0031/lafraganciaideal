---
status: testing
module: inventory
min_implementer: mid
depends_on: ["001", "catalog-perfumes/008"]
---

# 005 — Initial stock from the Excel import

## Context

The catalog Excel import (catalog-perfumes plans 007 and 008, both done) creates and updates
perfumes and presentations from an .xlsx file. It cannot load stock yet, and plan 002 made every
never-counted in-stock presentation show "Agotado" on the storefront. The shop needs a way to
load its counts in bulk before going live. This plan applies README decisions 1, 5, 7 and
18–22.

**What exists today:**
- **Columns.** The template's columns are `COLUMNS`
  (`apps/api/src/fragancia_api/modules/catalog/domain/import_cells.py:22-44`). Headers are
  matched by slug, and unknown headers are ignored. `whole_number` parses an int, an integral
  float or a digit string (`:100-112`).
- **Validation.** Each row is parsed in `_parse_row` and `_parse_presentation`
  (`modules/catalog/application/import_validation.py:289-401`). The raw (not text) columns are
  `_RAW_COLUMNS` (`:102-112`). `_plan` turns valid groups into the summary and the plan
  (`:562-620`).
- **The plan types.** `ImportSummary` and `PlannedPresentation` live in
  `modules/catalog/domain/catalog_import.py:64-86`. `CatalogImport.apply(now, actor_id)` marks
  the import applied (`:245-251`).
- **Storage.** The summary and the plan are stored as JSON through explicit mappers
  (`modules/catalog/infrastructure/sql_catalog_import_repository.py:43-100`). The read side
  validates the stored summary into `CatalogImportSummary`
  (`modules/catalog/infrastructure/sql_catalog_import_queries.py:36-44`;
  `modules/catalog/contracts.py:351-358`).
- **Confirm.** `ConfirmCatalogImport.execute` applies the plan in one transaction
  (`modules/catalog/application/commands/catalog_imports.py:387-419`).
  - New presentations come from `perfume.add_presentation`, which returns the new id
    (`_create` `:459-473`; `_upsert_presentation` `:582-588`; `Perfume.add_presentation`
    `modules/catalog/domain/perfume.py:481-505`).
  - Existing presentations keep `planned.presentation_id` (`:589-604`).
  - The command has no `EventPublisher` today. It is wired at `modules/catalog/module.py:313-324`.
- **The confirm route** requires only `catalog:manage`
  (`modules/catalog/http/import_router.py:32-36`, `:112-129`). The actor's permission names
  are on `request.state.actor.permissions` (`shared/application/actor.py:18`). The 403
  exception is `Forbidden` (`shared/http/errors.py:48-49`). `require_permission` raises it
  (`shared/http/access.py:43-49`).
- **The template.** It is built from `COLUMNS` by `OpenpyxlTemplateWriter._build`
  (`modules/catalog/infrastructure/xlsx_sheets.py:149-189`).
- **Inventory.** `RecordMovement` records one movement (`modules/inventory/application/commands/stock.py:37-90`).
  - **Lock order:** the catalog presentation (share), the settings row (share), then the stock
    item.
  - The timestamp is taken under the stock lock.
  - It publishes `inventory.stock.changed`.
  - A count sets `on_hand` to the number, and 0..`QUANTITY_MAX` (100,000) is valid
    (`modules/inventory/domain/stock.py:52`, `:109-116`, `:164-200`).
- **Events.** Subscribers run inside the relay's transaction: the relay opens a unit of work,
  calls every handler, then marks the row published and commits
  (`shared/infrastructure/outbox.py:103-128`). `TransactionRunner.run` joins an active
  transaction (`shared/infrastructure/database.py:64-65`). The catalog already subscribes to an
  inventory event by name (`modules/catalog/module.py:262-265`;
  `modules/catalog/application/handlers/stock_changed.py:1-37`).

**Approach: an event from the catalog, applied by inventory.**
- Confirming publishes `catalog.import.applied` through the outbox, in the confirm's
  transaction. Its payload carries the import id, the actor and a map of presentation id → count.
- Inventory subscribes by name and records one `count` movement per presentation, with the
  lock order of `RecordMovement`.
- **Why not call inventory from the confirm?** Inventory already depends on the catalog facade
  (`apps/api/.importlinter`, `modules-independent` ignore lines; `modules/inventory/module.py:3`,
  `:34-35`). Calling inventory from the catalog would make the two modules depend on each other,
  and no `MODULES` order could register both facades first.
- **The trade-off: the counts apply about 2 s after the confirm** (one relay cycle).
  - Delivery cannot be lost, because the event commits with the import.
  - The handler's writes commit together with the row's `published_at`, so a committed delivery
    is never repeated.

**Files imitated:**
- the handler follows `modules/catalog/application/handlers/stock_changed.py:19-37`;
- the subscription follows `modules/catalog/module.py:262-265`;
- the new command follows `RecordMovement` (`modules/inventory/application/commands/stock.py:37-90`);
- the event follows `StockChanged` (`modules/inventory/domain/stock.py:129-139`), recorded on the
  aggregate the way `Brand` does it (`modules/catalog/domain/brand.py:43`, `:65`).

## Out of scope

- **Exporting the current catalog or stock to Excel.** The template stays empty.
- **Other movement kinds from Excel** (restock, loss, adjustment, return). The column only sets
  counts.
- **Showing the counts in the import report row by row.** The summary gives only how many
  counts the confirm will record.
- **Thresholds from Excel.** Thresholds stay panel-only.
- **Reservations** (plan 003) and **the low-stock email** (plan 004).
- **Any change to the catalog's stock copy.** It follows from the `inventory.stock.changed`
  events the new command publishes.

## Dependencies

- **001 (done):** `StockItem.record_movement`, `MovementKind.COUNT`, `Reason`, the repositories
  and `PresentationCatalog.describe_for_update`
  (`modules/inventory/application/ports.py:20-34`).
- **002 (done):** every movement publishes `inventory.stock.changed`, and `StockItem.changed_event`
  exists (`modules/inventory/domain/stock.py:217-223`).
- **catalog-perfumes/008 (done):** `ConfirmCatalogImport` and the confirm route.
- **Platform:** `EventPublisher`, `EventMessage` and `EventSubscriptions`
  (`shared/application/events.py:10-33`); `DomainEvent.payload` turns a `dict` with UUID keys into
  string keys (`shared/kernel/events.py:31-51`).

## Steps

1. **Catalog: the "Existencias" cell**
   - Files: `apps/api/src/fragancia_api/modules/catalog/domain/import_cells.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/domain/errors.py` (modify)
   - Do:
     - **The column.** Insert `"Existencias"` in `COLUMNS` right after `"Entrega máx. (días)"`.
       It is not in `REQUIRED_COLUMNS`.
     - **The bound.** Add `STOCK_COUNT_MAX = 100_000`, with a comment saying it mirrors
       inventory's `QUANTITY_MAX` (a test pins the two).
     - **The parser.** Add `stock_count(cell) -> Result[int, ImportValueInvalid]`:
       `whole_number`, then `0 <= n <= STOCK_COUNT_MAX`. Out of range is
       `ImportValueInvalid(details={"min": 0, "max": STOCK_COUNT_MAX})`.
     - **The error.** Add `ImportStockMadeToOrder(InvalidValueError)` to `errors.py`, after
       `ImportValueInvalid`:
       - code `CATALOG_IMPORT_STOCK_MADE_TO_ORDER`;
       - message "Made-to-order presentations carry no stock; leave Existencias empty".
   - Observable result: unit tests parse `5`, `5.0`, `"5"` and `0`, and refuse `-1`, `100001`,
     `2.5`, `"cinco"` and `True`.

2. **Catalog: the count in the plan and the summary**
   - Files: `apps/api/src/fragancia_api/modules/catalog/domain/catalog_import.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/application/import_validation.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_catalog_import_repository.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/contracts.py` (modify),
     `apps/api/openapi.json` (modify)
   - Do:
     - **Domain** (`catalog_import.py`):
       - `PlannedPresentation` gains `stock_count: int | None = None`, as its last field;
       - `ImportSummary` gains `stock_counts: int = 0`, as its last field. It is the number of
         rows with a count.
     - **Validation** (`import_validation.py`):
       - Add `"Existencias"` to `_RAW_COLUMNS`.
       - `_Row` gains `stock_count: int | None = None`.
       - In `_parse_presentation`, parse it with
         `_optional(raw["Existencias"], cells.stock_count, row, "Existencias")`.
       - When the row's availability kind is `"made_to_order"` and the cell is not empty,
         add `ImportStockMadeToOrder()` on `"Existencias"` and leave `stock_count` None.
       - In `_plan`, copy `row.stock_count` into `PlannedPresentation` and count the non-None
         ones into `ImportSummary.stock_counts`.
     - **Storage** (`sql_catalog_import_repository.py`):
       - `summary_to_json` and `_presentation_to_json` write the new fields.
       - **Reading.** The `*_from_json` mappers read them with `data.get(...)`, defaulting to 0
         and None. Imports stored before this plan have no such keys.
     - **Contract** (`contracts.py`):
       - `CatalogImportSummary` gains `stock_counts: int = 0`. The default keeps summaries
         stored before this plan valid.
       - Its docstring says that `stock_counts` presentations will get a physical count.
     - Run `uv run just openapi`.
   - Observable result: a ready import of a file with three counts reports
     `summary.stock_counts: 3`. A count on a "Sobre pedido" row makes the import `invalid`,
     with `CATALOG_IMPORT_STOCK_MADE_TO_ORDER` at that row and column `Existencias`.

3. **Catalog: the template**
   - Files: `apps/api/src/fragancia_api/modules/catalog/infrastructure/xlsx_sheets.py` (modify)
   - Do: nothing beyond confirming that `_build` writes every entry of `COLUMNS`
     (`:158-161`) and positions the dropdowns with `COLUMNS.index` (`:184`). The new column
     appears with no dropdown. Change the file only if a check proves otherwise.
   - Observable result: `GET /api/v1/admin/catalog-imports/template` has an `Existencias` header
     after `Entrega máx. (días)`, and the dropdowns still sit on their columns.

4. **Catalog: the event on confirm**
   - Files: `apps/api/src/fragancia_api/modules/catalog/domain/catalog_import.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/application/commands/catalog_imports.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/module.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/__init__.py` (modify)
   - Do:
     - **The event** (`catalog_import.py`): `CatalogImportApplied(DomainEvent)`, frozen and
       `kw_only` like `StockChanged`:
       - `name: ClassVar[str] = "catalog.import.applied"`;
       - fields `import_id: UUID`, `actor_id: str` and `stock_counts: dict[UUID, int]`.
     - **`apply(now, actor_id, stock_counts: Mapping[UUID, int])`** records the event, with the
       counts copied into a `dict`, before dropping the plan. The docstring of `apply` and the
       module docstring's lifecycle line mention it.
     - **The command** (`catalog_imports.py`):
       - `ConfirmCatalogImport.__init__` takes `events: EventPublisher`.
       - `_create`, `_update` and `_upsert_presentation` also return, through `Ok`, the
         `(presentation_id, stock_count)` pairs of the rows with a count:
         - a new presentation uses the id `add_presentation` returns;
         - an updated one uses `planned.presentation_id`.
       - Keep the existing shape: `_apply` returns `Ok((old_keys, counts))`.
       - `execute` gathers them into one dict and calls `catalog_import.apply(now, actor_id,
         counts)`. After `self._imports.save(...)`, it calls
         `await self._events.publish(catalog_import.pull_events())`, inside `work`.
       - Update the class docstring: confirming publishes `catalog.import.applied`, and
         inventory turns its counts into count movements.
     - **Wiring** (`module.py`): pass `events=platform.events` to `ConfirmCatalogImport`.
     - **The facade docstring** (`catalog/__init__.py`): add an "Events" list, in the style of
       `modules/inventory/__init__.py:4-9`:
       - the event `catalog.import.applied`;
       - its payload `import_id` (UUID string), `actor_id` and `stock_counts` (an object from
         presentation id string to count);
       - published once per applied import, in the confirm's transaction.
   - Observable result: confirming a ready import with counts writes one
     `catalog.import.applied` row to `platform.outbox`, holding the final presentation ids,
     including those of new presentations. A stale confirm writes none.

5. **Catalog: the permission on confirm**
   - Files: `apps/api/src/fragancia_api/modules/catalog/http/import_router.py` (modify)
   - Do:
     - Add `INVENTORY_MANAGE = "inventory:manage"`, with a comment saying it is the identity
       permission name (like `CATALOG_MANAGE`, `modules/catalog/http/router.py:70`).
     - In `confirm_catalog_import`, before running the command, apply decision 21: when
       `INVENTORY_MANAGE` is not in `request.state.actor.permissions`,
       1. load the import with the `GetCatalogImport` query the route already receives, and
          `unwrap` it, so an unknown id still answers 404;
       2. raise `Forbidden` (from `fragancia_api.shared.http.errors`) if its
          `summary.stock_counts > 0`.

       The plan of a ready import never changes, so this check cannot race the confirm.
     - Add 403 to the documented responses, and to the docstring: "403 `FORBIDDEN` when the
       file has Existencias and the actor lacks `inventory:manage`".
     - Run `uv run just openapi`.
   - Observable result: an actor with only `catalog:manage` gets 403 `FORBIDDEN` confirming a
     file with counts, and 200 with a file without counts.

6. **Inventory: record the counts**
   - Files: `apps/api/src/fragancia_api/modules/inventory/application/commands/stock.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/application/handlers/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/application/handlers/import_applied.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/module.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/__init__.py` (modify)
   - Do:
     - **The command** `RecordImportedCounts` (`stock.py`), with the same constructor as
       `RecordMovement`. `execute(counts: Mapping[UUID, int], actor_id: str) -> Result[int,
       DomainError]` returns how many counts it recorded.
       - Build the reason once: `Reason.create(IMPORT_COUNT_REASON)`, where
         `IMPORT_COUNT_REASON = "Conteo desde la importación de Excel"` is a module
         constant.
       - Inside one `transactions.run`, for each presentation id in ascending order:
         1. `_require_stocked`. On a refusal (the presentation is gone, or is no longer in
            stock), log `inventory.import_count_skipped` with the presentation id and the
            error code, then skip it.
         2. `settings.get_for_share()`, the first time only.
         3. `stock.get_for_update(...)`; then `now = clock.now()`; then `StockItem.empty` if
            it is None.
         4. `record_movement(MovementKind.COUNT, quantity, reason, actor_id, now)`. On `Err`,
            log and skip, as above.
         5. Save, and publish `item.changed_event(global_threshold, now)`.
       - Write the lock order as a comment, as in `RecordMovement`: catalog (share), settings
         (share), item. Items go in `presentation_id` order.
       - **A count equal to `on_hand` is still recorded** (decision 20).
     - **The handler** `ApplyImportedCounts(command)`, in `handlers/import_applied.py`, follows
       `stock_changed.py`:
       - `IMPORT_APPLIED_EVENT = "catalog.import.applied"`;
       - parse `actor_id` as a str and `stock_counts` as `{UUID(key): int(value)}`;
       - an empty map is a no-op;
       - otherwise call the command, which joins the relay's transaction;
       - a malformed payload raises, so the relay retries it;
       - the docstring explains that delivery commits with the handler's writes
         (`shared/infrastructure/outbox.py:121-126`), so a committed delivery is never
         repeated.
     - **Wiring** (`module.py`): register `RecordImportedCounts` with the same arguments as
       `RecordMovement`, then
       `platform.subscriptions.subscribe(IMPORT_APPLIED_EVENT, ApplyImportedCounts(...))`.
     - **The facade docstring** (`inventory/__init__.py`): say that inventory subscribes to
       `catalog.import.applied` and records one count movement per entry.
   - Observable result: after the relay delivers the event:
     - each counted presentation has a `count` movement with that reason and the confirming
       actor;
     - `on_hand` equals the file's number;
     - one `inventory.stock.changed` row per presentation is in the outbox.

7. **Docs and tests**
   - Files: `docs/architecture.md` (modify),
     `apps/api/tests/unit/catalog/` (modify), `apps/api/tests/unit/inventory/` (modify),
     `apps/api/tests/integration/catalog/` (modify), `apps/api/tests/integration/inventory/` (modify)
   - Do:
     - **`docs/architecture.md`:** under "Events, outbox and jobs" (`:152-167`), add a bullet
       for `catalog.import.applied` → inventory count movements. Say why it is an event (the
       two modules would otherwise depend on each other), and that it lags the confirm by one
       relay cycle.
     - **Test plumbing.** The three tests that build `ConfirmCatalogImport` pass
       `events=RecordingEventPublisher()` (`shared/infrastructure/in_memory.py:17-22`):
       - `apps/api/tests/unit/catalog/test_confirm_catalog_import.py:116`;
       - `apps/api/tests/unit/catalog/test_catalog_import_confirm_http.py:76`;
       - `apps/api/tests/integration/catalog/test_sql_catalog_import_confirm.py:256`.
     - **Tests:** the tester writes the layered tests.
   - Observable result: `uv run just check` and `uv run just test-integration` are green.

## Acceptance criteria

- [ ] **Template.** The downloaded template has an `Existencias` column after
  `Entrega máx. (días)`.
- [ ] **Validation.**
  - A file whose "En existencia" rows have `Existencias` 10, 0 and empty validates `ready`, with
    `summary.stock_counts: 2`.
  - `-1`, `100001`, `2.5` and `abc` give `CATALOG_IMPORT_VALUE_INVALID` on `Existencias`.
  - A count on a "Sobre pedido" row gives `CATALOG_IMPORT_STOCK_MADE_TO_ORDER`.
  - A file without the column validates as before, with `stock_counts: 0`.
- [ ] **Confirm.**
  - Confirming writes one `catalog.import.applied` outbox row. It maps the final presentation
    ids (both the new and the updated ones) to their counts.
  - Within a few seconds, with the worker running:
    - each counted presentation has a `count` movement with the reason "Conteo desde la
      importación de Excel" and the confirming user as actor;
    - `GET /api/v1/admin/inventory/presentations/{id}` shows the file's number;
    - the storefront follows (`available`, `last_units` or `sold_out`).
- [ ] **Overwrite.** A presentation with stock 7 and `Existencias` 4 ends with `on_hand` 4.
- [ ] **Same number.** A presentation with stock 4 and `Existencias` 4 gets a count movement
  with delta 0.
- [ ] **Empty cell.** A row with an empty `Existencias` records no movement for that
  presentation.
- [ ] **Permission.**
  - An admin without `inventory:manage` gets 403 `FORBIDDEN` confirming a file with counts, and
    nothing is applied.
  - The same admin confirms a file without counts (200).
- [ ] **Skipped.** A counted presentation switched to "Sobre pedido" between the confirm and the
  relay is skipped with a log line, and the other counts are recorded.
- [ ] **Old imports.** An import stored before this plan still reads, with `stock_counts: 0`.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `stock_count` boundaries (0, `STOCK_COUNT_MAX`, `STOCK_COUNT_MAX`+1, negative, non-integral, bool); `STOCK_COUNT_MAX == QUANTITY_MAX`; `apply` records `CatalogImportApplied` with the counts and its payload has string keys |
| application | yes     | Validation: counts in the plan and the summary, the made-to-order error, no column at all. Confirm: the event carries the new and updated ids, and a stale confirm publishes nothing. `RecordImportedCounts`: ascending order, equal count recorded, unknown or made-to-order presentation skipped, one `StockChanged` per count. `ApplyImportedCounts`: parsing, empty map, malformed payload raises |
| http        | yes     | Confirm 403 with counts and without `inventory:manage`, 200 without counts, 404 unknown id unchanged; template header; summary field in the response and OpenAPI |
| integration | yes     | JSON round trip of the new plan fields, and reading a pre-plan stored summary and plan; confirm → outbox row → relay → movements, `on_hand` and the catalog copy; the lock order under a concurrent `RecordMovement` on the same presentation |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

None to the design. Cosmetic: existing tests that pin the old shapes were updated minimally to keep the suites green (`apply(..., {})` calls, the 22 headers, `stock_counts: 0` in two summary assertions in unit and integration tests, `events=RecordingEventPublisher()` as the plan listed). Step 3 needed no change to `xlsx_sheets.py` (confirmed by reading `_build`).

## Test coverage

## Review findings

## Verification
