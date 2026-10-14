---
status: review
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

Baseline (before any test): `just check` green (2107 passed, 3 skipped), `just test-integration`
green (564 passed, 5 skipped). Closing: `just check` green (2187 passed, 3 skipped, 1 xfailed),
`just test-integration` green (580 passed, 5 skipped). Added: 80 unit tests, 16 integration
tests, 1 strict xfail (GAP). Paths are under `apps/api/tests/`; `unit/catalog/` is shortened to
`u/c/`, `unit/inventory/` to `u/i/`, `integration/` to `int/`.

| Behavior (from plan / code) | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| `stock_count` accepts 5, 5.0, "5", 0 and `STOCK_COUNT_MAX` | `import_cells.py:118` | domain | `u/c/test_import_cells_domain.py::test_stock_count_accepts_whole_numbers_from_zero_to_the_maximum` | CONFIRMED |
| Refuses -1, 100001, "100001" with details `{min: 0, max: 100000}` | `import_cells.py:118-126` | domain | `::test_stock_count_out_of_range_is_a_value_error_carrying_the_bounds` | CONFIRMED |
| Refuses 2.5, "cinco", True/False, None, "", NaN | `import_cells.py:118` (via `whole_number`) | domain | `::test_stock_count_rejects_non_integral_text_and_booleans` | CONFIRMED |
| `Existencias` is after `Entrega máx. (días)` and not required | `import_cells.py` `COLUMNS` | domain | `::test_existencias_is_a_template_column_right_after_the_max_lead_time_and_not_required` | CONFIRMED |
| `STOCK_COUNT_MAX == QUANTITY_MAX` and the event names agree | `import_cells.py:61`, `stock.py:52` | application | `u/i/test_imported_counts.py::test_the_catalog_limit_mirrors_the_inventory_limit`, `::test_inventory_subscribes_by_the_name_the_catalog_publishes` | CONFIRMED |
| `apply` records `CatalogImportApplied` with counts, actor and import id; copies the mapping | `catalog_import.py:258-272` | domain | `u/c/test_catalog_import_confirm_domain.py::test_apply_records_one_import_applied_event_with_the_counts_and_the_actor`, `::test_the_event_copies_the_counts_so_a_later_change_of_the_mapping_does_not_leak`, `::test_an_import_without_counts_still_records_the_event_with_an_empty_map` | CONFIRMED |
| Event name `catalog.import.applied`; payload has string keys | `catalog_import.py:40-47` | domain | `::test_the_event_name_is_a_string_for_inventory_and_its_payload_has_string_keys` | CONFIRMED |
| `apply` from a non-ready status raises and records no event | `catalog_import.py:263` | domain | `::test_apply_from_a_status_but_ready_records_no_event` (7 statuses) | CONFIRMED |
| `ImportStockMadeToOrder` code and kind | `errors.py` | domain | `::test_a_count_on_a_made_to_order_row_is_an_invalid_value_with_its_own_code` | CONFIRMED |
| Counts reach the plan; summary counts the rows with one (0 counts, empty does not) | `import_validation.py:629` | application | `u/c/test_import_validation.py::test_counts_land_in_the_plan_and_the_summary_counts_the_rows_with_one` | CONFIRMED |
| Count as text or integral float; maximum accepted | `import_validation.py:402-406` | application | `::test_a_count_may_be_text_or_an_integral_float_like_any_whole_number_cell`, `::test_the_maximum_count_is_accepted` | CONFIRMED |
| No column / empty cell: no counts, `stock_counts: 0` | `import_validation.py:402-406` | application | `::test_a_file_without_the_column_validates_as_before_with_no_counts`, `::test_an_empty_count_cell_means_no_count_not_zero` | CONFIRMED |
| -1, 100001, 2.5, "abc" give `CATALOG_IMPORT_VALUE_INVALID` on `Existencias` | `import_validation.py:402-406` | application | `::test_an_invalid_count_is_a_value_error_on_the_existencias_column` | CONFIRMED |
| A count (even 0) on a made-to-order row gives `CATALOG_IMPORT_STOCK_MADE_TO_ORDER`; empty is valid | `import_validation.py:401-402` | application | `::test_a_count_on_a_made_to_order_row_is_its_own_issue_on_existencias`, `::test_a_count_of_zero_on_a_made_to_order_row_is_still_refused`, `::test_a_made_to_order_row_with_an_empty_count_is_valid_and_counts_nothing` | CONFIRMED |
| An existing presentation's count is planned with its stored id | `import_validation.py:629` | application | `::test_the_count_of_an_existing_presentation_is_planned_with_its_stored_id` | CONFIRMED |
| Confirm publishes one event with the final ids of new, updated and new-size-of-stored presentations | `catalog_imports.py:411-427` | application | `u/c/test_confirm_catalog_import.py::test_confirming_publishes_one_event_with_the_final_ids_of_new_and_updated_counts`, `::test_the_ids_in_the_event_are_the_ids_the_presentations_were_saved_with` | CONFIRMED |
| No counts: event with an empty map | `catalog_imports.py:425-427` | application | `::test_an_import_without_counts_publishes_the_event_with_an_empty_map` | CONFIRMED |
| Stale, not-ready and unknown-import confirms publish nothing | `catalog_imports.py:404-424` | application | `::test_a_stale_confirm_publishes_nothing`, `::test_confirming_an_import_that_is_not_ready_publishes_nothing`, `::test_confirming_an_unknown_import_publishes_nothing` | CONFIRMED |
| Command records one `count` movement per presentation (reason, actor, clock), returns how many | `stock.py:100-170` | application | `u/i/test_imported_counts.py::test_each_count_sets_on_hand_with_a_count_movement_by_the_actor` | CONFIRMED |
| Overwrite (7 to 4); same number recorded with delta 0; 0 creates the item | `stock.py:100-170` | application | `::test_a_count_overwrites_the_existing_stock`, `::test_a_count_equal_to_on_hand_is_still_recorded_with_delta_zero`, `::test_a_count_of_zero_creates_the_stock_item_for_a_never_counted_presentation` | CONFIRMED |
| Ascending presentation-id order, whatever the map order | `stock.py:130` | application | `::test_counts_are_recorded_in_ascending_presentation_id_order` | CONFIRMED |
| One `StockChanged` per count, with the global threshold or the item override | `stock.py:160-166` | application | `::test_each_count_publishes_one_stock_changed_with_the_new_count_and_the_threshold`, `::test_the_stock_changed_event_uses_the_presentations_own_threshold_override` | CONFIRMED |
| Unknown, made-to-order or refused-quantity presentation is skipped with `inventory.import_count_skipped`; others recorded | `stock.py:141`, `:158` | application | `::test_an_unknown_presentation_is_skipped_with_a_log_and_the_others_are_recorded`, `::test_a_presentation_switched_to_made_to_order_is_skipped_with_a_log`, `::test_a_count_the_aggregate_refuses_is_skipped_and_the_others_are_recorded`, `::test_when_every_count_is_skipped_nothing_is_written_or_published` | CONFIRMED |
| Empty map records nothing | `import_applied.py` | application | `::test_an_empty_map_records_nothing`, `::test_the_handler_treats_an_empty_map_as_a_no_op` | CONFIRMED |
| Handler parses the payload (also the one a real event produces) | `import_applied.py` | application | `::test_the_handler_parses_the_payload_and_records_the_counts_for_the_actor`, `::test_the_handler_accepts_the_payload_a_real_event_produces` | CONFIRMED |
| Malformed or incomplete payload raises (relay retries) and records nothing | `import_applied.py` | application | `::test_a_malformed_payload_raises_so_the_relay_retries_and_records_nothing`, `::test_a_payload_with_a_missing_field_raises`, `::test_a_bad_entry_after_a_good_one_records_nothing_because_parsing_comes_first` | CONFIRMED |
| Command `Err` path of the handler (`RuntimeError`) | `import_applied.py` | application | no test: the command returns `Err` only if `Reason.create` fails, and the constant is valid | NOT CONFIRMED (unreachable with the real constant; not tested) |
| 403 `FORBIDDEN` confirming a file with counts without `inventory:manage`; nothing applied or published | `import_router.py:137-142` | http | `u/c/test_catalog_import_confirm_http.py::test_confirming_a_file_with_counts_is_403_without_inventory_manage_and_applies_nothing` | CONFIRMED (through a test-local query store, see GAP) |
| 200 without counts; 200 with counts and `inventory:manage` (event carries actor and ids) | `import_router.py:137-142` | http | `::test_confirming_a_file_without_counts_is_200_without_inventory_manage`, `::test_confirming_a_file_with_counts_is_200_with_inventory_manage_and_publishes_them` | CONFIRMED |
| Unknown id still 404; 403 comes before 409 for a non-ready file with counts; discard needs no inventory permission | `import_router.py:137-142` | http | `::test_the_permission_check_for_an_unknown_import_is_still_404`, `::test_a_file_with_counts_that_is_not_ready_still_answers_403_before_the_409`, `::test_discarding_a_file_with_counts_needs_no_inventory_manage` | CONFIRMED |
| Summary field in the response; OpenAPI documents 403 and an optional `stock_counts` | `contracts.py`, `import_router.py` | http | `::test_the_import_status_reports_how_many_counts_the_confirm_will_record`, `::test_the_openapi_documents_the_403_of_the_confirm_and_the_summary_field` | CONFIRMED |
| The in-memory query reports `summary.stock_counts` | `in_memory_imports.py:146-153` | http | `::test_the_in_memory_query_reports_the_stock_counts_of_the_summary` | GAP (strict xfail; finding `plans/findings/catalog-in-memory-import-query-drops-stock-counts.md`) |
| Template has `Existencias` after `Entrega máx. (días)`, bold, wide, no dropdown; dropdowns unmoved; a count reads back | `xlsx_sheets.py:149-189` | integration | `int/catalog/test_xlsx_sheets.py::test_the_template_has_existencias_after_the_max_lead_time_with_no_dropdown`, `::test_a_count_written_under_existencias_is_read_back_by_the_reader`, existing `::test_the_template_puts_a_list_dropdown_on_the_five_columns_for_rows_2_to_2001` | CONFIRMED |
| New plan fields round trip; stored as plain JSON (`null` for an empty cell) | `sql_catalog_import_repository.py:50`, `:87` | integration | `int/catalog/test_sql_catalog_imports.py::test_the_stock_counts_round_trip_in_the_summary_and_every_planned_presentation`, `::test_the_counts_are_stored_as_plain_json_and_an_empty_cell_as_null` | CONFIRMED |
| Imports stored before the plan read with `stock_counts: 0` and no counts (store and query) | `sql_catalog_import_repository.py:61`, `:103`; `sql_catalog_import_queries.py:44` | integration | `::test_an_import_stored_before_plan_005_reads_with_no_counts`, `::test_the_query_reads_an_import_stored_before_plan_005_with_stock_counts_zero`, `::test_the_query_reports_stock_counts` | CONFIRMED |
| Confirm writes one unpublished `catalog.import.applied` row with the final ids (new and updated) | `catalog_imports.py:427` | integration | `int/inventory/test_import_counts_flow.py::test_confirming_counts_puts_one_event_in_the_outbox_with_the_final_presentation_ids` | CONFIRMED |
| Relay: count movements (reason, actor), `on_hand`, one `stock.changed` per count, catalog copy, empty cell untouched, no repeat on a second pass | `import_applied.py`, `stock.py:100-170` | integration | `::test_the_relay_turns_the_counts_into_count_movements_stock_events_and_the_copy` | CONFIRMED |
| Same number gives a count movement with delta 0 (real DB) | `stock.py` | integration | `::test_a_count_equal_to_the_stock_is_recorded_with_delta_zero` | CONFIRMED |
| No counts: nothing in inventory; stale confirm: no outbox row, import stays `ready` | `catalog_imports.py:422-427` | integration | `::test_an_import_without_counts_records_nothing_in_inventory`, `::test_a_stale_confirm_writes_no_event_and_no_count` | CONFIRMED |
| Presentation switched to made-to-order between confirm and relay is skipped with a log; others recorded; delivery marked published | `stock.py:141` | integration | `::test_a_presentation_switched_to_made_to_order_before_the_relay_is_skipped_with_a_log` | CONFIRMED |
| Lock order: counts wait for a movement in flight and end the ledger; a movement on a later item runs while counts hold an earlier one, no deadlock; counts wait for a settings change and carry the new threshold | `stock.py:122-130` | integration | `::test_the_counts_wait_for_a_movement_in_flight_and_the_count_ends_the_ledger`, `::test_a_movement_on_a_later_item_does_not_wait_for_counts_holding_an_earlier_one`, `::test_the_counts_wait_for_a_settings_change_and_carry_the_new_threshold` | CONFIRMED |
| Storefront status follows the copy after a count | `stock_changed.py` (plan 002) | integration | the copy is asserted here (`(4, 3)`, `(12, 3)`, `(0, 3)`); the copy-to-status mapping is covered by plan 002's `test_stock_status_flow.py` | CONFIRMED to the copy; the status itself not re-asserted for imported perfumes |
| Acceptance "with the worker running, within a few seconds" (real worker process, 2 s relay cycle) | `shared/infrastructure/outbox.py` | e2e / running app | the tests call `relay_all` directly | NOT CONFIRMED here (a verify-phase check) |

Out-of-plan edits kept minimal and test-only: `tests/unit/inventory/support.py` (`record_counts`
on `Inventory`), the `events` field on the `Rig` of `test_confirm_catalog_import.py` and
`test_catalog_import_confirm_http.py`, and the `_StoreReportingCounts` workaround in the latter.

## Review findings

## Verification
