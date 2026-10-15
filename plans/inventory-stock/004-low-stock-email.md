---
status: review
module: inventory
min_implementer: mid
depends_on: ["001", "002", "003", "005"]
---

# 004 — Low-stock email to owners

## Context

**The panel already knows "low".**
- `is_low(available, override, global)` (`apps/api/src/fragancia_api/modules/inventory/domain/stock.py:136`)
  decides it. The stock list filters on it (`application/queries/stock.py`, `ListStock`).
- Nobody is told when a presentation becomes low.

**Decision 10** asks for one email to the active owners when a presentation's stock drops to or
below its threshold, not repeated until it goes back above. This plan applies decisions 8–10
and 32–34.

**What exists today:**
- **Stock commands.** Every stock change goes through these commands, and each publishes
  `item.changed_event(...)` after saving:
  - `RecordMovement`, `RecordImportedCounts`, `SetThreshold` and `UpdateInventorySettings`
    (`modules/inventory/application/commands/stock.py:94`, `:166`, `:218`, `:259`);
  - `CommitReservation`, the only reservation command that changes `on_hand`
    (`modules/inventory/application/commands/reservations.py:242`).
  - Reserve, release and expire change only `reserved` (`:123`, `:294`, `:340`).
- **Emails.** They are sent from outbox subscribers in the worker, through
  `platform.email: EmailSender` (`shared/application/email.py`; for example
  `modules/identity/application/handlers/account_emails.py:43-89`, wired at
  `modules/identity/module.py:194-215`).
- **Owners.** They live in identity:
  - `identity.users` has `email`, `name`, `role` and `is_active`
    (`modules/identity/infrastructure/tables.py:8-20`);
  - `Role.OWNER = "owner"` (`modules/identity/domain/user.py:75-77`);
  - identity exposes no public API yet (`modules/identity/__init__.py`).
- **Module order.** Identity registers before inventory
  (`apps/api/src/fragancia_api/container.py:31`), so inventory can consume an identity
  facade, following `docs/recipes/new-module.md` §5 (`:43-65`).

**Approach.**
- **The alert state.** Each stock item gets a `low_alerted` flag that re-arms (decision 10).
  Every command that changes `on_hand` or a threshold calls the new domain method
  `item.track_low(global_threshold)`, which returns True when the item just went low by
  **units on hand** (decision 32). Reservations cannot flap it.
- **One event per operation** (decision 33). The command collects the items that went low,
  then publishes one `inventory.stock.went_low` event listing them, in the same transaction.
- **The email.** An inventory subscriber turns the event into one email per active owner. It
  reads the recipients through a new identity facade, `OwnerDirectory`, and the labels through
  the existing `PresentationCatalog` port.
- **Rejected:**
  - **A scheduled scan comparing the current stock with a "last emailed" table.** It is
    simpler to wire, but adds a delay and needs its own state.
  - **Emailing from the HTTP request.** Forbidden by `EmailSender`'s contract.

**Files imitated:**
- the facade follows `modules/catalog/application/queries/presentation_directory.py:30-48`,
  with its queries port at `modules/catalog/application/ports.py:116-130`;
- the adapter and wiring follow `modules/inventory/infrastructure/catalog_presentations.py`
  and `modules/inventory/module.py`;
- the email handler follows `account_emails.py:43-89`;
- the migration backfill follows `apps/api/migrations/versions/0013_catalog_presentation_stock_copy.py`.

## Out of scope

- **A panel URL in the email.** There is no web app yet. The email names the presentations and
  tells the reader to check the inventory panel.
- **Digest emails, per-owner preferences or unsubscribe.**
- **Emailing staff, or a fixed address** (decision 10: owners only).
- **Changing the panel's or storefront's "low"/"Últimas piezas".** They keep using
  `available` (decision 29). Only the email uses units on hand.
- **WhatsApp or other channels** (the planned `notifications` module).

## Dependencies

- **001 (done):** stock items, thresholds, the commands and `PresentationCatalog`.
- **002 (done):** `StockChanged` publishing in every command.
- **003 (done):** `CommitReservation` (a sale lowers `on_hand`), and `available` vs `on_hand`.
- **005 (done):** `RecordImportedCounts`. Its counts can make items go low.
- **Platform:**
  - `EventPublisher` and `EventSubscriptions`;
  - `EmailSender` (`shared/application/email.py`);
  - `DomainEvent.payload`, which turns lists of dicts into JSON (`shared/kernel/events.py:31-51`).

## Steps

1. **Identity: the `OwnerDirectory` facade**
   - Files: `apps/api/src/fragancia_api/modules/identity/application/queries/owner_directory.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/ports.py` (modify),
     `apps/api/src/fragancia_api/modules/identity/infrastructure/sql_owner_directory.py` (create),
     `apps/api/src/fragancia_api/modules/identity/infrastructure/in_memory.py` (modify),
     `apps/api/src/fragancia_api/modules/identity/module.py` (modify),
     `apps/api/src/fragancia_api/modules/identity/__init__.py` (modify)
   - Do:
     - **`owner_directory.py`:**
       - `OwnerContact(name: str, email: str)`, frozen;
       - `OwnerDirectory(queries)` with `active_owners() -> list[OwnerContact]`: active users
         with role `owner`, ordered by name. The docstring says it is the only way other modules
         read owners.
     - **Port** `OwnerDirectoryQueries` in `ports.py`, with `active_owners()`.
     - **SQL:** `select(users.c.name, users.c.email).where(users.c.role == "owner",
       users.c.is_active.is_(True)).order_by(users.c.name)`, through `database.reader()`.
     - **In-memory:** add `active_owners` to the in-memory user store in `in_memory.py`.
     - **Wiring:** register `services.add(OwnerDirectory, OwnerDirectory(SqlOwnerDirectory(platform.database)))`
       in `module.py`.
     - **`__init__.py`:** export `OwnerDirectory` and `OwnerContact`, and rewrite the docstring's
       "Public API" line.
   - Observable result: unit tests list only active owners. A deactivated owner and a staff user
     are absent.

2. **Inventory: the alert state and the event**
   - Files: `apps/api/src/fragancia_api/modules/inventory/domain/stock.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/tables.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/sql_stock_repository.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/in_memory.py` (modify),
     `apps/api/migrations/versions/0015_inventory_low_stock_alert.py` (create)
   - Do:
     - **Domain** (`stock.py`):
       - `StockItem` gains `low_alerted: bool`, keyword, default False in `__init__` and
         `empty`.
       - New `track_low(global_threshold: Threshold) -> bool`. Let
         `low = on_hand <= effective threshold` (units on hand, decision 32):
         - low and not alerted: set `low_alerted = True` and return True;
         - not low: set `low_alerted = False` and return False;
         - otherwise return False.

         The docstring says why it uses `on_hand`, not `available`.
       - New event `StockWentLow(DomainEvent)`:
         - `name = "inventory.stock.went_low"`;
         - field `lines: tuple[dict[str, object], ...]`, each
           `{"presentation_id": str, "on_hand": int, "threshold": int}`.

         Add a helper `low_stock_event(items, global_threshold) -> StockWentLow | None`. It
         builds the event for the given items, or returns None for an empty list.
     - **Table and repository:**
       - `stock_items` gains `low_alerted` Boolean, not null, server default false.
       - `SqlStockRepository` reads it in `get_for_update`, `get_many_for_update` and
         `list_all_for_update`, and writes it in `save`.
       - `InMemoryStock` keeps it through its deep copies.
     - **Migration.** Run `uv run just db-revision "inventory low stock alert"`; it becomes
       `0015`.
       - Add the column.
       - Backfill it once: `UPDATE inventory.stock_items si SET low_alerted = true FROM
         inventory.settings s WHERE si.on_hand <= coalesce(si.threshold_override,
         s.low_stock_threshold)`, so items that are already low send no burst of emails on
         deploy (decision 34).
       - The downgrade drops the column.
   - Observable result: unit tests show:
     - a first drop to the threshold returns True;
     - a second movement below it returns False;
     - going above re-arms it;
     - a reserve never changes it.

3. **Inventory: the commands publish it**
   - Files: `apps/api/src/fragancia_api/modules/inventory/application/commands/stock.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/application/commands/reservations.py` (modify)
   - Do:
     - In `RecordMovement`, `RecordImportedCounts`, `SetThreshold`, `UpdateInventorySettings`
       and `CommitReservation`, call `item.track_low(global_threshold)` on every changed item
       **before** saving it. For `UpdateInventorySettings`, use the new threshold.
     - Collect the items for which it returned True. After the existing
       `StockChanged` publish, publish `low_stock_event(went_low, threshold)` when it is not
       None. That is one event per command execution (decision 33).
     - Threshold changes count as well (decision 34).
       - Raising a threshold so that `on_hand` is now at or below it alerts.
       - Lowering it so that `on_hand` is above re-arms silently.
     - Reserve, extend, release and expire do not call it, because `on_hand` and the thresholds
       do not change.
   - Observable result: application tests show:
     - a loss to the threshold publishes one `StockWentLow` with that line;
     - an import of three counts at 0 publishes one event with three lines;
     - a commit that leaves `on_hand` at the threshold publishes it;
     - a reserve publishes none.

4. **Inventory: the email**
   - Files: `apps/api/src/fragancia_api/modules/inventory/application/ports.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/identity_owners.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/application/handlers/low_stock_email.py` (create),
     `apps/api/src/fragancia_api/modules/inventory/module.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/__init__.py` (modify),
     `apps/api/.importlinter` (modify)
   - Do:
     - **Port** `OwnerRecipients` (`ports.py`), with `active_owners() -> list[Recipient]`.
       `Recipient(name: str, email: str)` is inventory's own type.
     - **Adapter** `IdentityOwners(directory: OwnerDirectory)` (`identity_owners.py`) maps
       `OwnerContact` to `Recipient`.
     - **Handler** `SendLowStockEmail(catalog: PresentationCatalog, owners: OwnerRecipients,
       email: EmailSender)` (`low_stock_email.py`):
       - parse `lines`, then `catalog.describe(ids)` for the labels; an unknown id uses its id
         as the label;
       - `owners.active_owners()`; with none, log `inventory.low_stock_no_owner` and return;
       - send one `EmailMessage` per owner, with the constants `LOW_STOCK_SUBJECT = "Stock bajo
         en La Fragancia Ideal"` and `LOW_STOCK_BODY`:

         ```
         Hola {name}:

         Estas presentaciones llegaron a su nivel de stock bajo:

         {lines}

         Revisa el inventario en el panel para resurtirlas.
         ```

         Each line is `- {label}: {on_hand} en almacén (umbral {threshold})`.
       - A send failure raises, so the relay retries the event.
       - The docstring says a retry can re-send to owners who already got it (at-least-once).
     - **Wiring** (`module.py`): `owners = IdentityOwners(services.get(OwnerDirectory))`, then
       `platform.subscriptions.subscribe(StockWentLow.name, SendLowStockEmail(catalog=catalog,
       owners=owners, email=platform.email))`.
     - **`.importlinter`:** add the two `ignore_imports` lines, in the style of the catalog
       ones:
       - `fragancia_api.modules.inventory.infrastructure.identity_owners -> fragancia_api.modules.identity`;
       - `fragancia_api.modules.inventory.module -> fragancia_api.modules.identity`.
     - **`__init__.py`:** document `inventory.stock.went_low` under "Events".
   - Observable result: with the worker running, a loss to the threshold delivers one email per
     active owner to Mailpit, listing the presentation.

5. **Docs and tests**
   - Files: `docs/architecture.md` (modify), `apps/api/tests/unit/inventory/` (modify),
     `apps/api/tests/unit/identity/` (modify), `apps/api/tests/integration/inventory/` (modify),
     `apps/api/tests/integration/identity/` (modify)
   - Do:
     - **`docs/architecture.md`:** under "Events, outbox and jobs", add a bullet for
       `inventory.stock.went_low` → the owners' email, and the identity facade.
     - **Tests:** existing tests that build `StockItem` keep working through the defaults. The
       tester writes the layered tests.
   - Observable result: `uv run just check` and `uv run just test-integration` are green.

## Acceptance criteria

Run with the API, the worker and Mailpit running. The QA database has at least one active
owner.

- [ ] **First drop.** A presentation with on hand 5 (global threshold 3) gets a loss of 2:
  - exactly one `inventory.stock.went_low` row in the outbox, with that line;
  - within seconds, Mailpit holds one "Stock bajo en La Fragancia Ideal" email per active owner,
    naming the presentation, "3 en almacén (umbral 3)".
- [ ] **No repeat.** A further loss to 2 sends no email.
- [ ] **Re-arm.** A restock to 6, then a loss to 3, sends one email again.
- [ ] **Reservations don't alert.** With on hand 4, reserving 1 (available 3) and letting it
  expire sends no email.
- [ ] **A sale alerts.** Committing a reservation that leaves on hand at 3 sends one email.
- [ ] **One per operation.** An import confirming three presentations at 0 sends one email per
  owner, listing all three.
- [ ] **Threshold change.**
  - Raising one presentation's threshold from 3 to 10 while it has 6 on hand sends one email.
  - Lowering it back re-arms it with no email.
- [ ] **Recipients.** A deactivated owner and a staff user receive nothing.
- [ ] **Deploy.** After `db-migrate`, items already at or below their threshold have
  `low_alerted = true`, and no email is sent until they recover and drop again.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `track_low` at every boundary (equal, below, above, override vs global, re-arm), never on `available`; `low_stock_event` payload and None for no items |
| application | yes     | Each of the five commands: went-low collected and one event per execution, threshold raise and lower, settings change with many items, reserve/release/expire publish none; `SendLowStockEmail`: one message per owner, body lines and labels, no owners logged, send failure raises; `OwnerDirectory` |
| http        | no      | No route changes |
| integration | yes     | Migration round trip and the backfill; `low_alerted` persisted by every repository read; SQL owner directory (role and active filters); outbox → relay → handler → Mailpit-bound `EmailSender` (the SMTP adapter against the CI Mailpit service) |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

- **Existing tests touched (cosmetic).** Eight existing assertions assumed every published or
  outbox event is `inventory.stock.changed`, which the new `went_low` event breaks. Filtered to
  `StockChanged` / `name == "inventory.stock.changed"` in `tests/unit/inventory/`
  (`test_imported_counts.py`, `test_reservation_commands.py`, `test_stock_changed_event.py`) and
  `tests/integration/inventory/` (`test_stock_status_flow.py`, `test_stock_settings_race.py`).
  No new tests written; the tester owns that.
- **`UpdateInventorySettings` now saves every stock item** (it only published events before), so a
  re-arm or a new `low_alerted` is persisted. Implied by step 3, not spelled out.
- **`inventory/infrastructure/in_memory.py` unchanged**: its deep copies already keep
  `low_alerted`.
- **Observation:** `SetThreshold` on a never-stocked presentation creates an empty item
  (on hand 0), which is low, so it alerts. Literal to the plan; the reviewer may want to confirm.
- **`plans-scope` reports `apps/api/tests/integration/catalog/test_sql_brands.py` as out of scope**:
  not modified in this worktree (git status clean for it); it comes from the diff against local
  `main` being behind. Not touched.

## Test coverage

Baseline (before writing): `uv run just check` green (2361 unit + 685 harness), `uv run just
test-integration` green (658 passed, 5 skipped). Closing: `check` green (2435 unit + 685 harness),
`test-integration` green (688 passed, 5 skipped; the 5 skips are the same as the baseline).
Added: 74 unit and 30 integration tests, no GAP, nothing skipped. All integration tests leave
`fragancia_test` as they found it (checked with `psql`: no users, stock items, reservations or
scratch databases; the one leftover `platform.outbox` row, `tests.something.happened`, comes from
the existing `test_transactions_and_outbox.py`, not from this plan).

Test files (all new):
- `apps/api/tests/unit/inventory/test_low_stock_domain.py` (23), `test_low_stock_commands.py` (34),
  `test_low_stock_email.py` (12)
- `apps/api/tests/unit/identity/test_owner_directory.py` (5)
- `apps/api/tests/integration/inventory/test_low_stock_alert.py` (14),
  `test_low_stock_email_flow.py` (5)
- `apps/api/tests/integration/identity/test_sql_owner_directory.py` (5)
- `apps/api/tests/integration/inventory/test_migration_0015_low_stock_alert.py` (6)

| Behavior | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| `track_low` returns True once on the first drop to or below the threshold (equal, below, zero), False above | `domain/stock.py` `track_low` | domain | `test_low_stock_domain.py::test_the_first_time_on_hand_reaches_the_threshold_it_returns_true` | CONFIRMED |
| No repeat while low; going above re-arms; an above-threshold item stays quiet | `domain/stock.py` `track_low` | domain | `test_while_it_stays_low_it_does_not_alert_again`, `test_going_above_the_threshold_re_arms_it_silently`, `test_an_item_above_the_threshold_that_was_never_alerted_stays_quiet` | CONFIRMED |
| Override vs global (higher, lower, zero), a new global, removing an override | `domain/stock.py` `track_low`, `effective_threshold` | domain | `test_the_override_wins_over_the_global_threshold_when_it_is_higher` / `_lower`, `test_an_override_of_zero_alerts_only_at_zero_units`, `test_a_new_global_threshold_is_what_track_low_compares_against`, `test_removing_an_override_re_arms_...` | CONFIRMED |
| Alert state uses `on_hand`, never `available`: hold, unhold, sale, movement | `domain/stock.py` `track_low` | domain | `test_reserved_units_do_not_make_an_item_low`, `test_releasing_reserved_units_cannot_re_arm_...`, `test_holding_units_of_...`, `test_a_sale_that_leaves_on_hand_at_the_threshold_alerts`, `test_a_movement_that_takes_on_hand_to_the_threshold_alerts` | CONFIRMED |
| `StockWentLow` name, lines, JSON payload; `low_stock_event` is None for no items | `domain/stock.py` `StockWentLow`, `low_stock_event` | domain | `test_the_event_is_named_...`, `test_the_event_lists_each_item_...`, `test_the_payload_turns_the_lines_into_json_values`, `test_no_items_means_no_event` | CONFIRMED |
| `RecordMovement`: one event on a loss to the threshold (after `StockChanged`), none while low, re-arm and alert again, first restock already low, override, refused movement | `commands/stock.py:94` | application | `test_low_stock_commands.py::test_a_loss_to_the_threshold_publishes_one_went_low_with_that_line` and the other `RecordMovement` tests | CONFIRMED |
| `SetThreshold`: raise alerts once, lower and clear re-arm silently, repeat is quiet, refused publishes nothing | `commands/stock.py:218` | application | `test_raising_a_threshold_above_the_units_on_hand_alerts_once`, `test_lowering_the_threshold_back_re_arms_without_an_event`, `test_clearing_an_override_...`, `test_setting_the_same_threshold_again_...`, `test_a_refused_threshold_...` | CONFIRMED |
| `SetThreshold` on a never-stocked presentation alerts for its empty item (the Deviations observation) | `commands/stock.py:218` | application | `test_a_threshold_on_a_never_stocked_presentation_alerts_for_its_empty_item` | CONFIRMED (literal; the reviewer decides whether it is wanted) |
| `RecordImportedCounts`: three counts at 0 give one event with three lines; only the low ones listed; no repeat; nothing recorded gives nothing | `commands/stock.py:166` | application | `test_an_import_of_three_counts_at_zero_publishes_one_event_with_three_lines` and four more | CONFIRMED |
| `UpdateInventorySettings`: many items in one event, overrides untouched, lowering re-arms and persists it, already alerted not repeated, no items | `commands/stock.py:259` | application | `test_raising_the_global_threshold_alerts_every_item_it_now_covers_in_one_event` and four more | CONFIRMED |
| `CommitReservation`: a sale to the threshold alerts, two items in one event, above stays quiet, already alerted does not repeat, double commit | `commands/reservations.py:242` | application | `test_a_sale_that_leaves_on_hand_at_the_threshold_alerts` and four more | CONFIRMED |
| Reserve, expire, release and extend publish no `went_low` and keep the flag | `commands/reservations.py:123,294,340` | application | `test_reserving_so_that_available_is_low_does_not_alert`, `test_reserving_and_letting_it_expire_...`, `test_releasing_a_reservation_...`, `test_extending_a_reservation_...` | CONFIRMED |
| `SendLowStockEmail`: one message per owner, greeting, line format with labels, unknown id as label, no owners logged `inventory.low_stock_no_owner`, send failure raises (also after earlier owners were sent), empty lines, real payload, malformed payload raises | `handlers/low_stock_email.py` | application | `test_low_stock_email.py` (11 tests) | CONFIRMED |
| `IdentityOwners` maps `OwnerContact` to `Recipient` | `infrastructure/identity_owners.py` | application | `test_low_stock_email.py::test_the_adapter_maps_...` | CONFIRMED |
| `OwnerDirectory`: only active owners, ordered by name, reactivation, frozen contact | `identity/.../owner_directory.py` | application | `test_owner_directory.py` (5) | CONFIRMED |
| SQL owner directory filters role and `is_active`, orders by name, reflects (de)activation, registered in the container | `identity/infrastructure/sql_owner_directory.py` | integration | `test_sql_owner_directory.py` (5) | CONFIRMED |
| `low_alerted` defaults to false, `save` writes it, `get_for_update`, `get_many_for_update` and `list_all_for_update` read it | `sql_stock_repository.py`, `tables.py` | integration | `test_low_stock_alert.py::test_a_new_item_defaults_...`, `test_save_persists_the_flag_and_every_locked_read_returns_it`, `test_save_clears_the_flag` | CONFIRMED |
| Each command's outbox row on PostgreSQL: first drop, no repeat, re-arm, expire and release (none), sale, import of three, threshold raise/lower, global change (persisted flags) | commands above | integration | `test_low_stock_alert.py` (8 tests) | CONFIRMED |
| Concurrent losses crossing the threshold alert once; a settings change and a movement alert the item once (gated, bounded by `TIMEOUT`, gates released on exit) | row locks in `sql_stock_repository.py` | integration | `test_two_concurrent_losses_that_cross_the_threshold_alert_once`, `test_a_settings_change_and_a_movement_alert_the_same_item_once`, `test_the_race_helpers_time_out_instead_of_hanging` | CONFIRMED |
| Outbox, relay, handler, SMTP to Mailpit: one email per active owner (deactivated owner and staff get nothing), exact body, no repeat then one after re-arm, import lists three, no owners (published and logged), SMTP failure keeps the event and a retry delivers | `module.py` wiring, `low_stock_email.py`, `smtp_email_sender.py` | integration | `test_low_stock_email_flow.py` (5); skipped as NOT CONFIRMED if Mailpit is unreachable | CONFIRMED (Mailpit ran locally) |
| Migration 0015 backfill marks rows already low by units on hand (equal, zero, higher/lower override, reserved not counted) for global 3, global 5 and no settings row (fallback 3) | `0015_inventory_low_stock_alert.py` | integration | `test_migration_0015_low_stock_alert.py::test_the_backfill_marks_exactly_the_items_already_low_by_units_on_hand` (3 cases) | CONFIRMED |
| Migration: column not null with default false; downgrade drops it and keeps the rows; upgrade backfills again and recomputes the flag | same | integration | `test_the_column_defaults_to_false_...`, `test_downgrade_drops_the_column_...`, `test_a_flag_an_item_lost_while_migrated_...` | CONFIRMED |
| Panel/storefront "low" keeps using `available` (decision 29) | `is_low` | domain | `test_reserved_units_do_not_make_an_item_low` asserts `is_low` stays True while `track_low` is False | CONFIRMED |

How the migration is tested without racing other suites: the test creates its own scratch
database `fragancia_mig_<random>_test` next to `fragancia_test` (the same server, the user the
tests already use), runs the real Alembic CLI against it in a subprocess (`-x test=true` with
`DATABASE_URL_TEST` overridden), and drops it in a `finally`. `fragancia_test` is never
downgraded. Skipped as NOT CONFIRMED if the user cannot create a database.

Notes for the reviewer:
- The existing flow tests that call `relay_all` now also deliver `went_low` rows through the real
  SMTP handler; they pass because `identity.users` has no owner left behind. If another suite left
  an owner in `identity.users` they would send real mail to Mailpit.
- CRLF: Mailpit returns the body with CRLF line ends (SMTP); the flow test normalizes them to LF
  before comparing the otherwise exact text.

## Review findings

## Verification
