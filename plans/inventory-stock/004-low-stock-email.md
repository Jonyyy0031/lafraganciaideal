---
status: approved
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

## Test coverage

## Review findings

## Verification
