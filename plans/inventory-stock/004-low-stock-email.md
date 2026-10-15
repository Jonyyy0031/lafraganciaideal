---
status: verify
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
     `apps/api/src/fragancia_api/shared/application/email.py` (modify, repair round 1),
     `apps/api/src/fragancia_api/shared/infrastructure/smtp_email_sender.py` (modify, repair round 1),
     `apps/api/tests/unit/` (modify, repair round 1), `apps/api/tests/integration/` (modify, repair round 1),
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

- **L2 (repair round 1).** The 0015 backfill uses a settings subquery with a fallback of 3, so
  it also works without a settings row. The plan's SQL would not.
- **Repair round 1 product code done** (M1, L3, L1). Seven unit tests pinned to the old
  never-stocked-alerts behaviour now fail by design (`test_low_stock_commands.py`: six;
  `test_low_stock_domain.py::test_a_new_item_is_not_alerted_and_an_empty_one_neither`); the
  implementer changes no tests, so they go to the tester (L4, L5, rejected-recipient test).
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
  - **Main session:** confirmed. After fast-forwarding local `main`, `plans-scope` is clean.

### Repair round 1 (2026-10-15, main session: review → implementing)

**Why.** The review found:
- **M1.** A never-stocked presentation alerts on its first stock change, even when nothing
  dropped. The first Excel import with counts after deploy would email every presentation
  loaded at or below its threshold.
- **L1–L5:** see `## Review findings`.

The user decided:
- never-counted presentations start as already alerted (README decision 35);
- a recipient the mail server rejects permanently is logged and skipped, not retried
  (decision 36).

The relay's unbounded retries stay as the platform finding `platform-outbox-relay-retries-forever`.

**Repair spec.**
1. **M1: new items start alerted.**
   - Files: `apps/api/src/fragancia_api/modules/inventory/domain/stock.py`,
     `apps/api/src/fragancia_api/modules/inventory/infrastructure/sql_stock_repository.py`.
   - `StockItem.empty(...)` passes `low_alerted=True`. Its docstring says why (decision 35): a
     presentation's first count is a load, not a drop. The alert arms once units on hand go above
     the threshold.
   - `SqlStockRepository._insert_empty` inserts `"low_alerted": True` (`:44-63`).
   - The `__init__` default stays False, for items read from storage.
   - The column's server default and the 0015 backfill are unchanged.
2. **L3: a permanent rejection of one recipient.**
   - Files: `apps/api/src/fragancia_api/shared/application/email.py`,
     `apps/api/src/fragancia_api/shared/infrastructure/smtp_email_sender.py`,
     `apps/api/src/fragancia_api/modules/inventory/application/handlers/low_stock_email.py`.
   - `email.py` gains `class EmailRejected(Exception)`: "The server permanently refused this
     recipient (an SMTP 5xx reply); retrying will not help." The `EmailSender` docstring
     mentions it.
   - `SmtpEmailSender._send_blocking` catches `smtplib.SMTPRecipientsRefused` and re-raises it
     as `EmailRejected`. It also does so for `smtplib.SMTPResponseException` with
     `smtp_code >= 500` that `send_message` raises for the recipient. Every other error
     propagates as today, so it is retried.
   - `SendLowStockEmail` catches `EmailRejected` per owner. It logs
     `inventory.low_stock_email_rejected` with the address and continues with the next owner.
     Any other exception still propagates, so the event is retried. Update its docstring.
   - The identity handlers are unchanged: they send one email per event.
3. **L1: architecture docs.**
   - Files: `docs/architecture.md`.
   - Fix lines 50-54: `.importlinter` now allows the catalog and identity facade imports.
   - The inventory row (`:61`) mentions the low-stock email.
   - The identity row (`:64`) mentions the `OwnerDirectory` facade.
4. **L2:** record here, as a deviation, that the 0015 backfill uses a settings subquery with a
   fallback of 3, so it also works without a settings row. The plan's SQL would not.
5. **L4 and L5:** test-only, for the tester.
   - L4: the existing outbox tests must not depend on `identity.users` being empty. Use the
     test clean-up the tester judges right.
   - L5: the global-lowering re-arm test must assert the item was alerted first.
   - Also update the tests pinned to the old M1 behaviour.
   - Add a test that a rejected recipient is skipped and the others still get the email,
     against Mailpit if it can reject, otherwise at the adapter and handler level.

**Scope.** The product code is the five files above, plus the docs. The implementer changes no
tests.

### Repair round 2 (2026-10-15, main session: review → implementing)

**Why.** The re-review found two defects in the SMTP adapter of round 1. Both contradict
decision 36 ("temporary failures are still retried"):
- **R-M1:** every `SMTPRecipientsRefused` became `EmailRejected`, but `smtplib` also raises it
  for 4xx refusals such as greylisting.
- **R-M2:** the 5xx `SMTPResponseException` branch never sees a recipient refusal. It only
  catches failures of the sender (`MAIL FROM`), helo or `DATA`, so a misconfigured sender would
  consume every low-stock event with no retry.

R-L1 is a stale "Next:" line in the architecture docs. The main session applied decision 36's
intent without a new user decision:
- only a permanent (5xx) refusal of the recipients counts as rejected;
- everything else is retried.

R-L2 is accepted as is: a relay-level "554" at `RCPT` skips every owner, which is logged. For a
shop with one owner, retrying forever would be worse.

**Repair spec.**
1. **R-M1 and R-M2: `smtp_email_sender.py`.**
   - Files: `apps/api/src/fragancia_api/shared/infrastructure/smtp_email_sender.py`.
   - Catch only `smtplib.SMTPRecipientsRefused`.
     - Raise `EmailRejected` when **every** entry of `error.recipients`
       (`{address: (code, message)}`) has a code `>= 500`.
     - Otherwise, re-raise the original error.
   - Remove the `SMTPResponseException` branch. Sender, helo and `DATA` failures propagate, so
     the event is retried.
   - Add a one-line comment for each rule.
2. **R-L1: `docs/architecture.md:63`.**
   - Files: `docs/architecture.md`.
   - Replace the stale "Next: reserve on checkout…" with the current state: reservations (plan
     003) and the low-stock email (plan 004) are done.
3. **Tests, for the tester.**
   - Repin the 421 integration test: a 4xx refusal now propagates, so the event is retried.
   - Repin `test_a_5xx_smtp_reply_becomes_email_rejected`: a 5xx sender or `DATA` failure now
     propagates.
   - Add a case with mixed 4xx and 5xx refusals, which propagates.
   - Add an all-5xx case, which is rejected.

**Scope.** The product code is `smtp_email_sender.py`, plus the docs. The implementer changes no
tests.

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
| `SetThreshold` on a never-stocked presentation alerts for its empty item (the Deviations observation) | `commands/stock.py:218` | application | `test_a_threshold_on_a_never_stocked_presentation_alerts_for_its_empty_item` | SUPERSEDED by repair round 1 (decision 35): now `test_a_threshold_on_a_never_stocked_presentation_sends_nothing` |
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

### Repair round 1 (tester, 2026-10-15)

Baseline: `check` red with exactly the 7 unit tests the Deviations name (2428 passed, 7 failed);
`test-integration` red with the 2 named (686 passed, 2 failed, 5 skipped). Closing: `check` green
(`just test` 3137 passed incl. 685 harness, 3 skipped), `test-integration` green (696 passed, 5
skipped, the same 5). Net: +17 unit, +8 integration, no GAP, nothing new skipped. After the runs:
`fragancia_test` has 0 users, stock items, reservations and scratch databases (the one outbox row is
the pre-existing `tests.something.happened`), and Mailpit holds the same 58 messages as before.

| Behavior | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| `StockItem.empty` starts alerted, a stored item defaults to not alerted; an empty item stays quiet until it goes above the threshold and drops | `domain/stock.py` `empty`, `track_low` | domain | `test_low_stock_domain.py::test_an_item_read_from_storage_defaults_to_not_alerted_but_an_empty_one_starts_alerted`, `test_an_empty_item_stays_quiet_until_it_goes_above_the_threshold_and_drops` | CONFIRMED (M1, decision 35) |
| First restock / first import / first threshold at or below the threshold sends nothing and leaves the item alerted; going above then dropping alerts | `commands/stock.py:94,166,218` | application | `test_low_stock_commands.py::test_a_first_restock_at_or_below_the_threshold_sends_nothing`, `test_a_never_counted_item_alerts_after_going_above_the_threshold_and_dropping`, `test_a_first_import_of_counts_at_or_below_the_threshold_sends_nothing`, `test_a_threshold_on_a_never_stocked_presentation_sends_nothing` | CONFIRMED (M1) |
| The six tests pinned to the old behaviour, rewritten to decision 35 (restock that stays low, import of three at 0 after loading above, only the counts reaching the threshold, a count that stays low, releasing, never-stocked threshold) | same | application | `test_low_stock_commands.py` (same names, new arrangements) | CONFIRMED |
| L5: the global-lowering re-arm test asserts the item was alerted before lowering | `commands/stock.py:259` | application | `test_lowering_the_global_threshold_re_arms_silently_and_persists_it` (`assert alerted` after the raise) | CONFIRMED |
| `SendLowStockEmail` skips an `EmailRejected` owner (first, middle), logs `inventory.low_stock_email_rejected` with the address, mails the rest, returns when all are rejected; a non-rejection error still raises and stops | `handlers/low_stock_email.py` | application | `test_low_stock_email.py::test_a_rejected_owner_is_skipped_and_logged_and_the_others_still_get_it` and three more | CONFIRMED (L3, decision 36) |
| `SmtpEmailSender`: `SMTPRecipientsRefused` and 5xx `SMTPResponseException` become `EmailRejected` (cause kept); 4xx replies and a dropped connection propagate unchanged | `smtp_email_sender.py` | unit | `test_smtp_email_sender.py::test_a_refused_recipient_becomes_email_rejected`, `test_a_5xx_smtp_reply_becomes_email_rejected` (3), `test_a_4xx_smtp_reply_propagates_unchanged_so_it_is_retried` (4), `test_a_dropped_connection_propagates_unchanged_so_it_is_retried` | CONFIRMED |
| The same against real `smtplib` replies from a loopback SMTP stub (Mailpit cannot refuse a recipient): a 550 gives `EmailRejected`, an accepted recipient is delivered, a refused connection stays `ConnectionRefusedError`; full flow: outbox, relay, handler, SMTP skips the refused owner, mails the other, logs it, and the event is published with `attempts == 0` | same + `container.py` wiring | integration | `test_low_stock_rejected_recipient.py` (5) | CONFIRMED (against the stub; NOT against Mailpit, which cannot be made to reject) |
| M1 on PostgreSQL: a first import and a first restock at or below the threshold leave no row and a stored flag of true; `_insert_empty` rows start alerted and a global change over them reports nothing; the import of three at 0 and its email now load above first | `sql_stock_repository.py:44-63` | integration | `test_low_stock_alert.py::test_a_first_import_at_or_below_the_threshold_leaves_no_row_and_the_items_alerted`, `test_a_first_restock_at_or_below_the_threshold_leaves_no_row_until_a_real_drop`, `test_empty_rows_inserted_by_the_repository_start_alerted`; the two rewritten import tests in `test_low_stock_alert.py` and `test_low_stock_email_flow.py` | CONFIRMED |
| L4: the inventory integration tests no longer depend on `identity.users` being empty: `clean_inventory` truncates the identity tables before and after every test | `tests/integration/inventory/conftest.py` | integration | the autouse `clean_inventory` fixture | CONFIRMED (by construction; no test can prove a leftover owner is removed without an order-dependent pair, so none was written) |

Observation for the reviewer (not a GAP, the spec says so): `smtplib` raises
`SMTPRecipientsRefused` when every recipient gets a non-2xx reply, a 4xx greylisting included
(confirmed with the stub: `test_a_421_reply_before_the_recipient_is_accepted_is_also_refused_by_smtplib`),
so the adapter treats a temporary refusal of the only recipient as permanent. Likewise a 5xx
`SMTPSenderRefused` or `SMTPDataError` also becomes `EmailRejected` (the adapter catches any 5xx
`SMTPResponseException`), which the handler would log as that owner's rejection. Not tested.
(Both observations are resolved by repair round 2, below.)

### Repair round 2 (tester, 2026-10-15)

Baseline: the four expected failures only (the three `test_a_5xx_smtp_reply_becomes_email_rejected`
cases and the 421 integration test), as in the dispatch. Closing: `check` green (685 harness tests
pass; no GAP), `test-integration` green (697 passed, 5 skipped, the same 5). Net: +3 unit, +1
integration. After the runs: `fragancia_test` has 0 users, 0 stock items, 0 scratch databases, and
Mailpit holds 0 messages.

| Behavior | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| A 5xx that is not a recipient refusal (plain `SMTPResponseException`, `SMTPSenderRefused`, `SMTPHeloError`, `SMTPDataError`; 500, 550, 554) propagates unchanged, never `EmailRejected` | `smtp_email_sender.py` | unit | `test_smtp_email_sender.py::test_a_5xx_failure_that_is_not_a_recipient_refusal_propagates_so_it_is_retried` (3) | CONFIRMED (repins the old 5xx test) |
| A 4xx `SMTPRecipientsRefused` propagates unchanged | same | unit | `test_a_4xx_recipient_refusal_propagates_so_it_is_retried` | CONFIRMED |
| Mixed 4xx and 5xx refusals propagate unchanged | same | unit | `test_a_mix_of_4xx_and_5xx_recipient_refusals_propagates_so_it_is_retried` | CONFIRMED |
| Every recipient 5xx gives `EmailRejected` with the cause kept; a single 550 still does | same | unit | `test_every_recipient_refused_with_a_5xx_becomes_email_rejected`, `test_a_refused_recipient_becomes_email_rejected` | CONFIRMED |
| Against real `smtplib` replies from the loopback stub: a 421 refusal raises `SMTPRecipientsRefused` (code 421), not `EmailRejected`; a 550 still raises `EmailRejected` | same | integration | `test_low_stock_rejected_recipient.py::test_a_421_reply_to_the_recipient_propagates_so_the_event_is_retried` (repinned), `test_a_550_reply_to_the_recipient_is_raised_as_email_rejected` | CONFIRMED (stub, not Mailpit) |
| Full flow with a 4xx (451) refusal: the `went_low` event stays unpublished with `attempts == 1` and a `last_error`, no rejection log; with a 5xx (550) refusal the owner is skipped, the other mailed and the event published with `attempts == 0` | `container.py`, handler, relay | integration | `test_a_temporarily_refused_owner_keeps_the_event_unpublished_for_a_retry` (new), `test_a_refused_owner_is_skipped_and_logged_the_others_are_mailed_and_the_event_is_done` | CONFIRMED |

Notes for the reviewer:
- (Superseded by L4 above.) The existing flow tests that call `relay_all` deliver `went_low` rows
  through the real SMTP handler; they used to depend on `identity.users` holding no owner.
- CRLF: Mailpit returns the body with CRLF line ends (SMTP); the flow test normalizes them to LF
  before comparing the otherwise exact text.

## Review findings

### Re-review after repair round 2 (2026-10-15)

Reviewed against `main...HEAD` at `69f3603` (repair `3bac26a`, its tests `69f3603`); the worktree
was clean. **All passed: status set to `verify`.** The re-review below is kept as history; its
R-M1, R-M2 and R-L1 are closed as listed here. No new defect needs a code change.

**Checklist: 14/14 applicable items pass** (PR body n/a, no PR yet).

- [x] `plans-scope`: "Every change is inside the plan" (28 declared, 41 changed). The only hot
  file, `apps/api/.importlinter`, is still exactly two appended `ignore_imports` lines.
- [x] `uv run just check`: green (2455 unit passed, 3 skipped; 685 harness).
- [x] `uv run just test-integration`: green (697 passed, 5 skipped).
- [x] Domain, CQRS-lite, contracts, money and time, routes, wiring, migration: round 2 touches
  only the SMTP adapter and docs, so none of them change.
- [x] Errors: `EmailRejected` now matches its docstring (`shared/application/email.py:15`,
  "an SMTP 5xx reply") and the port's contract.
- [x] No secrets or personal data (`@low-stock.test`, `@example.test`).
- [x] `## Deviations`: "Repair round 2" matches the code. I spot-checked "Remove the
  `SMTPResponseException` branch": `smtp_email_sender.py:43-52` catches only
  `SMTPRecipientsRefused`.
- [x] Docs: the inventory row of `docs/architecture.md:63` no longer has the stale "Next:".

**Re-review findings, re-checked:**

| Finding | Verdict | Evidence |
| --- | --- | --- |
| R-M1 (4xx refusal treated as permanent) | **fixed** (decision 36) | `smtp_email_sender.py:45-50`: `EmailRejected` only when every `error.recipients` code is `>= 500`; otherwise the original error is re-raised. Checked against `/usr/lib/python3.14/smtplib.py:889-902`: a 421 at `RCPT` closes the connection and raises `SMTPRecipientsRefused` with code 421, which now propagates; the `with` block's `QUIT` on a closed connection is swallowed by `SMTP.__exit__`. A non-numeric reply (code -1) also propagates. The integration test with a real 451 shows the event kept unpublished with `attempts == 1` and no rejection log. |
| R-M2 (generic 5xx branch) | **fixed** | The branch is gone; `SMTPSenderRefused`, `SMTPHeloError`, `SMTPDataError` and a plain `SMTPResponseException` propagate unchanged (unit test over 500/550/554). |
| R-L1 (stale "Next:") | **fixed** | See R2-L1 for a wording nit in the new text. |
| R-L2 (relay-level 5xx at `RCPT` skips every owner) | accepted by the main session in the round 2 deviation | No change expected. |

**Mutants** (each edited in place, run, then restored by hand; `git diff` empty at the end and
`test_smtp_email_sender.py` green again):
- `all(...)` changed to `any(...)`: killed (`test_a_mix_of_4xx_and_5xx_recipient_refusals_…`).
- `>= 500` changed to `>= 400`: killed (2 unit tests; the 421 integration test and
  `test_a_temporarily_refused_owner_keeps_the_event_unpublished_for_a_retry`).
- the old generic 5xx `SMTPResponseException` branch put back: killed
  (`test_a_5xx_failure_that_is_not_a_recipient_refusal_…[500|550|554]`).

**State after the review:**
- `fragancia_test`: 0 users, stock items, reservations, outbox rows and `fragancia_mig_*`
  databases.
- Mailpit: 58 messages before and after; I deleted none.

#### Findings

**Low**

- **R2-L1: the new inventory row text is redundant (docs nit, does not block).**
  `docs/architecture.md:63` says "Emails the owners when a presentation drops to its low-stock
  threshold." and then "Reservations (checkout, cancellation, payment) and the low-stock email
  are done". The low-stock email is stated twice, and "done" reads like a changelog in a table
  that already marks built modules with ✔. Also, the `orders` and `payments` modules that call
  the reservations at checkout and payment do not exist yet, so "(checkout, cancellation,
  payment)" names the intended callers, not working flows. Suggested wording when the docs are
  next touched: "…low-stock thresholds and list, stock reservations (reserve, release, commit)
  for checkout and payment. Emails the owners when a presentation drops to its low-stock
  threshold."

**Out of scope, recorded:** the `catalog` row of the same table (`docs/architecture.md:62`)
still says "Next: Excel import", but plans catalog-perfumes/007 and 008 are `done`. Recorded as
`plans/findings/catalog-architecture-row-stale-excel-import.md`.

### Re-review after repair round 1 (2026-10-15)

Reviewed against `main...HEAD` at `b9b5e3b` (repair `4d446d4`, its tests `b9b5e3b`); the worktree
was clean. **Status stays `review`**: R-M1 and R-M2 need a code change in
`smtp_email_sender.py` (and the matching test changes). The first review below is kept as
history; its M1 and L1–L5 are closed as listed here.

**Checklist: 14/14 applicable items pass** (PR body n/a, no PR yet).

- [x] `plans-scope`: "Every change is inside the plan" (28 declared, 41 changed). The only hot
  file, `apps/api/.importlinter`, is still two appended `ignore_imports` lines.
- [x] `uv run just check`: green (2452 unit passed, 3 skipped; 685 harness).
- [x] `uv run just test-integration`: green (696 passed, 5 skipped).
- [x] Business rules in `domain/`: decision 35 lives in `StockItem.empty`, mirrored by the
  repository's `_insert_empty`; the handler only formats and sends.
- [x] CQRS-lite, contracts, money and time, routes, wiring: the repair changes none of them.
- [x] Errors: `EmailRejected` is a port-level exception for an adapter outcome, not a domain
  error. That's fine.
- [x] Migration: none added; 0015 unchanged, as the spec says.
- [x] No secrets or personal data (`@low-stock.test`, `@example.test`).
- [x] `## Deviations`: I spot-checked "`in_memory.py` unchanged": still true (`plans-scope`
  lists it as declared but unchanged; `StockItem.empty` gives it decision 35). It has one
  omission, which is part of R-M2.
- [x] Docs: `docs/architecture.md` now covers both facades and the low-stock email. There is one
  pre-existing stale fragment (R-L1).

**First-review findings, re-checked:**

| Finding | Verdict | Evidence |
| --- | --- | --- |
| M1 (never-counted alerts) | **fixed** (decision 35) | `StockItem.empty` (`domain/stock.py:187-198`) and `_insert_empty` (`sql_stock_repository.py:57`) start alerted. Every creation path goes through one of them; I grepped all `StockItem(` and `stock_items` inserts. The `__init__` default stays False for stored rows, and the backfill is unchanged. Mutant `empty()` back to False: killed by unit tests, among them `test_a_restock_that_stays_at_or_below_the_threshold_does_not_re_arm` and both new domain tests. Mutant `_insert_empty` back to False: 3 integration tests fail. |
| L1 (stale architecture docs) | **fixed** | `docs/architecture.md:50-56` and the inventory and identity rows. See R-L1 for a leftover. |
| L2 (missing deviation) | **fixed** | First bullet of `## Deviations`. |
| L3 (permanent refusal re-sends forever) | **fixed for a 5xx recipient refusal**, but the adapter over-reaches | Handler `low_stock_email.py:60-69` skips and logs per owner. Mutant `break` instead of continue: 4 unit tests fail. The adapter's classification is wrong in two ways; see R-M1 and R-M2. |
| L4 (tests depend on empty `identity.users`) | **fixed** | `tests/integration/inventory/conftest.py:24-32` truncates the identity tables before and after each inventory test (test DB only). |
| L5 (vacuous re-arm test) | **fixed** | `test_low_stock_commands.py:352` asserts `alerted` after the raise. |

**Mutants** (each edited in place, run, then restored by hand; `git diff` empty at the end):
- `empty()` set to False: killed.
- `_insert_empty` set to False: killed.
- adapter `>= 500` changed to `>= 400`: killed (`test_a_4xx_smtp_reply_propagates_…[451|499]`).
- handler `break` on `EmailRejected`: killed.

**State after the review:**
- `fragancia_test`: 0 users, stock items, reservations and `fragancia_mig_*` databases.
- `platform.outbox`: 0 rows. The pre-existing `tests.something.happened` row was gone after
  the integration run (an inventory fixture truncates the outbox); I deleted nothing.
- Mailpit: 58 messages before and after.

#### Findings

**Medium**

- **R-M1: a temporary (4xx) recipient refusal is treated as permanent, so that owner's alert
  is lost. This contradicts decision 36.**
  - **Where:** `apps/api/src/fragancia_api/shared/infrastructure/smtp_email_sender.py:45-46`
    maps every `SMTPRecipientsRefused` to `EmailRejected`.
  - **What fails:** `smtplib.SMTP.sendmail` (`/usr/lib/python3.14/smtplib.py:893-902`) collects
    any reply to `RCPT TO` other than 250/251 and raises `SMTPRecipientsRefused` once every
    recipient is refused. That includes 421, 450, 451 and 452. The adapter sends one recipient
    per message, so a temporary refusal of that recipient becomes `EmailRejected`. The handler
    then logs it as a rejection, skips the owner and consumes the event.
  - **Contradictions:**
    - decision 36 says "Temporary failures are still retried";
    - `EmailRejected`'s own docstring (`shared/application/email.py:15`) says "an SMTP 5xx
      reply".
  - **Tests:** the tester confirmed this against a real `smtplib` exchange.
    `test_low_stock_rejected_recipient.py::test_a_421_reply_before_the_recipient_is_accepted_is_also_refused_by_smtplib`
    currently pins the wrong behaviour.
  - **Scenario:** production SMTP rate-limits or greylists at `RCPT` ("451 4.7.1 try again
    later", or "421 4.7.0 too many connections"). Owner B's low-stock alert is dropped for
    good, with a log line that says B's address was rejected. Whether a given provider answers
    `RCPT` with a 4xx is provider-dependent (uncertain how often), but the code path is certain.
  - **Fix direction (implementer):** raise `EmailRejected` only when every code in
    `error.recipients.values()` is `>= 500`; otherwise re-raise, so the event is retried. The
    tester inverts the 421 test and adds a 450/451 case.
- **R-M2: the 5xx `SMTPResponseException` branch covers failures that are not about the
  recipient. A misconfigured sender silently drops every low-stock alert.**
  - **Where:** `smtp_email_sender.py:47-50`.
  - **What fails:** the repair spec says the 5xx branch is for errors "that `send_message`
    raises for the recipient". But a `RCPT` refusal never reaches this branch: it is always
    `SMTPRecipientsRefused`, which is not an `SMTPResponseException`. The only
    `SMTPResponseException` subclasses `send_message` can raise are:
    - `SMTPHeloError` (EHLO/HELO);
    - `SMTPSenderRefused` (`MAIL FROM`, `smtplib.py:883-888`);
    - `SMTPDataError` (`DATA` or end of data, `smtplib.py:578`, `:903-909`).

    So in practice the branch only catches server, sender or message failures. Each of these
    would hit every owner the same way. The handler then logs one "rejected" line per owner
    and marks the event published.
  - **Not recorded:** the over-reach is missing from `## Deviations`.
  - **Scenario:** production `SMTP_FROM` is not a verified sender, or the host requires auth
    but `SMTP_USERNAME` is unset (`_send_blocking` skips `login`). The server answers
    `MAIL FROM` with "550 sender not allowed" or "530 5.7.0 authentication required". Then:
    - every low-stock email is consumed with no retry;
    - the logs blame each owner's address;
    - the identity emails, which don't catch `EmailRejected`, keep retrying.

    The same misconfiguration thus looks like a delivery outage for account emails and like
    "bad owner addresses" for stock alerts. Before the repair it was retried and visible.
  - **Fix direction (implementer):** drop the generic 5xx branch, so sender, HELO and data
    failures propagate and are retried.
  - **Uncertain:** a 5xx `SMTPDataError` can, on some servers, be a per-recipient verdict
    given after `DATA` (for example, "552 mailbox full"). If the user wants that case skipped
    too, it needs a decision (the content is the same for every owner, so a content rejection
    would skip all of them). The safe default is to propagate it.
  - **Tests:** `test_smtp_email_sender.py::test_a_5xx_smtp_reply_becomes_email_rejected`
    (a bare `SMTPResponseException`) pins the current behaviour. The tester should replace it
    with `SMTPSenderRefused` / `SMTPDataError` 5xx cases that propagate.

**Low**

- **R-L1: a stale "Next" in the inventory row.** `docs/architecture.md:63` still ends with
  "Next: reserve on checkout, release on cancellation, commit on payment", but plan 003
  (reservations) is `done`. This pre-dates plan 004; the first review mentioned it in passing
  and the repair edited that row without removing it. Docs only.
- **R-L2: an inherent limit, recorded so nobody over-trusts the skip (uncertain, no code
  change proposed).** Even with R-M1 and R-M2 fixed, a relay-level policy refusal at `RCPT` is
  indistinguishable from a bad address by reply code alone. Examples are "554 5.7.1 relay
  access denied" or "550 5.7.1 not authorized", the kind caused by a sender or auth
  misconfiguration. The handler would skip every owner in that case too.
  - Since every owner of one event would be refused, that pattern is detectable: for example,
    the handler could re-raise when every owner was rejected.
  - Whether to add that is a user or architect call under decision 36; I am not proposing it
    as required.

### Tester's observations on `smtp_email_sender.py`, judged

- **(a) 4xx refusals become `EmailRejected`: confirmed, a defect.** This is R-M1. It violates
  decision 36's "temporary failures are still retried", and the fix is local to the adapter.
- **(b) The 5xx catch covers `SMTPSenderRefused` and `SMTPDataError`: confirmed, a defect.**
  This is R-M2. It is worse than the tester framed it: that branch can never see a recipient
  refusal at all, so everything it catches is a non-recipient failure.

### First review (superseded by the re-review above for M1 and L1–L5)

Reviewed on 2026-10-15 against `main...HEAD` (`f49b55e` implementation, `160a874` tests); the
worktree was clean. **Status stays `review`**: M1 needs a user decision and may need code, and
L1 needs a docs edit.

### Checklist: 13/14 applicable items pass (docs item fails)

- [x] `plans-scope`: "Every change is inside the plan" (24 declared, 34 changed). The one hot
  file, `.importlinter`, is append-only: two `ignore_imports` lines added at the end.
- [x] `uv run just check`: green (2435 unit passed, 3 skipped; 685 harness).
- [x] `uv run just test-integration`: green (688 passed, 5 skipped).
- [x] Business rules in `domain/`: `track_low` and `low_stock_event`. The handler only formats
  the email.
- [x] CQRS-lite: every new write happens inside the existing `TransactionRunner.run`, and the
  `OwnerDirectory` facade reads through `database.reader()`.
- [x] Contracts: no contract or route change, so `openapi.json` is correctly untouched.
- [x] Errors: no new error codes. The handler raises only for the unexpected (a send failure
  or a malformed payload).
- [x] Money and time: not involved. `now` still comes from the `Clock`.
- [x] Migration 0015 is new and reversible (the downgrade drops the column), with no
  cross-schema foreign key. Its backfill rule equals `track_low`. Integration tests cover the
  round trip on a scratch database.
- [x] Routes: none added.
- [x] Wiring:
  - identity registers `OwnerDirectory`, and inventory reads it from the registry;
  - the adapter lives in `infrastructure/`;
  - `test_container` and `arch` are green.
- [x] No secrets or real personal data: the tests use `@low-stock.test` and `@example.test`.
- [x] `## Deviations` exists and its claims check out. I spot-checked "UpdateInventorySettings
  now saves every item" (`commands/stock.py:279-282`) and "`in_memory.py` unchanged": both true.
  One deviation is missing; see L2.
- [ ] **Docs: stale lines remain (L1).**
- n/a PR body: no PR exists yet for this branch.

### Focus areas requested by the main session

**Re-arm logic (decisions 10, 32–34), command by command:**

| Command | Calls `track_low` | Threshold used | One event per execution | Verdict |
| --- | --- | --- | --- | --- |
| `RecordMovement` | yes, before save (`stock.py:103`) | global from the share-locked settings, plus the override through `effective_threshold` | yes | correct |
| `RecordImportedCounts` | yes, per recorded count (`:178`); skipped counts are excluded | same | yes, after the loop (`:183-184`); none when nothing was recorded | correct |
| `SetThreshold` | yes, after `set_threshold` (`:234`) | new override, or the global one | yes | a raise alerts and a lower re-arms (decision 34); see M1 for a never-stocked item |
| `UpdateInventorySettings` | yes, on every item, with the new threshold (`:279`) | new global; items with an override are unaffected | yes | correct |
| `CommitReservation` | yes, per item (`reservations.py:245`) | global | yes | a double commit returns early, so it cannot repeat |
| Reserve, extend, release, expire | no | — | — | correct: `on_hand` and the thresholds don't change, and they save the `low_alerted` they loaded |

`track_low` compares `on_hand` with the effective threshold (decision 32), alerts once while
low, and re-arms only when the item is above the threshold (decision 10). The panel's `is_low`
still uses `available` (decision 29).

**Lock order of the new saves:**
- `list_all_for_update` (`sql_stock_repository.py:92-95`) already holds every item
  `FOR UPDATE` in `presentation_id` order, under the settings row's `FOR UPDATE`.
- The new saves (`stock.py:280-281`) are upserts on rows already locked, so they take no new
  locks.
- No row can be inserted concurrently: every path that inserts placeholder rows
  (`_insert_empty`) takes the settings `FOR SHARE` first. I checked movement, import, threshold,
  reserve, commit, release and expire.
- So no new wait cycle is possible.
- Cost: every global-threshold change now writes every stock row, even unchanged ones. That is
  fine at this shop's scale (no finding).

**Identity facade and `.importlinter`:**
- `OwnerDirectory` and `OwnerContact` are exported from `identity/__init__.py`.
- The SQL filters on `role = 'owner'` and `is_active`, ordered by name.
- The inventory adapter maps the identity types to its own `Recipient`.
- The two `ignore_imports` lines are exactly the module and adapter imports.
- The `ports.py` ↔ `owner_directory.py` import cycle is guarded by `TYPE_CHECKING`, and import
  works (`check` is green).

**Email handler retries:**
- Any exception (a catalog lookup, the owners query, any send) propagates, so the relay rolls
  back and backs off. The behaviour is as planned and documented as at-least-once.
- With no active owner, the event is consumed (logged and marked published). That alert is
  never re-sent when an owner is added later. That is the plan as written.
- See L3 for a permanent per-recipient failure.

**Migration 0015 backfill:**
- `on_hand <= coalesce(threshold_override, (SELECT low_stock_threshold … LIMIT 1), 3)` matches
  `track_low`, and `3` equals `DEFAULT_LOW_STOCK_THRESHOLD`.
- Unlike the plan's `FROM inventory.settings` join, it also covers a database with no settings
  row. That is an improvement, but it is undocumented (L2).
- `reserved` is correctly ignored.

**Mutants** (each edited in place, run, then restored by hand; `git diff` was empty after each):

| Mutant | Result |
| --- | --- |
| `track_low` on `available` | killed (`test_reserved_units_do_not_make_an_item_low`) |
| no re-arm | killed (7 tests) |
| `UpdateInventorySettings` drops the save loop | killed (`test_raising_the_global_threshold_alerts_every_item_…`) |
| `CommitReservation` drops the `went_low` publish | killed (3 tests) |
| the handler emails only the first owner | killed (2 tests) |
| backfill `<=` changed to `<` | killed (5 migration integration tests) |

**State after the review:**
- `fragancia_test`: 0 users, stock items, reservations, outbox rows and `fragancia_mig_*`
  databases.
- Mailpit: 58 messages before and after, so none were left by the run.
- The full integration run left the pre-existing `tests.something.happened` outbox row. I counted
  it (1) and deleted it by name.

### Tester's notes, judged

1. **`SetThreshold` on a never-stocked presentation alerts.** This is real, and wider than
   `SetThreshold`. See M1; the user decides.
2. **Existing relay tests now reach the real SMTP handler.** This is accurate.
   - `relay_all` in `test_stock_status_flow.py:68` (also used by the import, reservation and
     settings-race tests) now runs `SendLowStockEmail` with the container's SMTP sender.
   - Their isolation depends on `identity.users` holding no owner. `clean_identity`
     (`tests/integration/identity/conftest.py:22-30`) truncates only *before* each identity
     test.
   - Today the run leaves no user, and Mailpit was unchanged. But a crashed or partially run
     identity test, or a new identity test that ends with an owner, would make these tests mail
     that owner silently and still pass.
   - L4: test-only, for the tester.
3. **The leftover platform outbox row.** This is confirmed, pre-existing and not from this plan
   (`clean_outbox` truncates only before each test). It is recorded as
   `plans/findings/platform-outbox-test-leaves-a-row.md`.

### Findings

**Medium**

- **M1 — A presentation that was never stocked alerts on its first touch, even when nothing
  dropped (needs a user decision).**
  - **Where:**
    - `StockItem.empty` (`domain/stock.py:187`) and `_insert_empty`
      (`sql_stock_repository.py:44-63`) create items with `low_alerted = False` at `on_hand = 0`,
      which is always at or below the threshold;
    - the backfill only covers rows that exist at deploy.
  - **What happens:** the first `track_low` on such an item returns True whenever the result is
    still at or below the threshold.
  - **Scenarios:**
    - **The initial stock load.** After deploy, the first Excel import with "Existencias"
      (plan 005, decision 7) records 1–3 bottles for many presentations that had no row. One
      email then lists every one of them as "low". This is the burst decision 34 wanted to
      avoid. It is also not a "drop" (decision 10): the stock went up from 0.
    - **The same from the panel.** A first restock of 2 units (threshold 3), or a first
      `SetThreshold` on a never-counted presentation, emails "2 en almacén" or "0 en almacén".
    - **Placeholder rows.** The placeholder rows that `ReserveStock` leaves inside a caller's
      transaction (`reservations.py:120-122`), or that a skipped import count leaves
      (`stock.py:161-175`, `continue` after `get_for_update` inserted the row), stay at 0 /
      False. The next global-threshold change emails them all.
  - **Status of the code:** this is literal to the plan (the defaults are False), and the tests
    pin it (`test_a_threshold_on_a_never_stocked_presentation_alerts_for_its_empty_item`,
    "first restock already low").
  - **Possible fix, if the user wants "drops" literally:**
    - treat a never-counted item as already alerted: `empty()` and `_insert_empty` set
      `low_alerted = True`;
    - so the first alert needs the item to rise above the threshold and fall back.
  - **Uncertain:** whether the user wants the initial-load email.

**Low**

- **L1 — Stale docs** in `docs/architecture.md`:
  - `:50-54` says `.importlinter` "allows exactly that import", the catalog one. Now identity is
    allowed too, and `OwnerDirectory` is a second built example.
  - The module table rows are stale:
    - `inventory` (`:61`) does not mention the low-stock email (it still lists reservations as
      "Next", which was already stale);
    - `identity` (`:64`) does not mention its first public facade.
  - The new events bullet at `:175-182` is correct.
- **L2 — Missing deviation.** Step 2's backfill SQL (a `FROM inventory.settings` join) was
  replaced by a subquery with a fallback of `3`
  (`migrations/versions/0015_inventory_low_stock_alert.py:30-36`). It is better: with no
  settings row, the plan's join would mark nothing. But `## Deviations` does not record it.
- **L3 — A permanent failure for one owner re-sends forever to the owners before it**
  (`handlers/low_stock_email.py:58-65`, together with the relay having no attempt cap).
  - **Scenario:** owners A and B; the SMTP provider synchronously refuses B (for example, a
    suppressed address). A gets the same email at every retry, up to every 5 minutes,
    indefinitely.
  - **Status:** this is the plan's at-least-once design. The likelihood is low with Mailpit or a
    typical relay. Uncertain: it depends on the production SMTP provider's behaviour.
  - **Where it's recorded:** the root cause is the relay, recorded as
    `plans/findings/platform-outbox-relay-retries-forever.md`.
  - **A local mitigation for the user to weigh:** send per owner (one job each), or skip and log
    a recipient refusal.
- **L4 — Test hygiene (tester).** This is the tester's note 2: the existing relay tests depend
  on an empty `identity.users`. Suggestion: an inventory integration fixture that asserts, or
  arranges, no active owner before `relay_all`. Test-only.
- **L5 — A vacuous test (tester).**
  `test_low_stock_commands.py::test_lowering_the_global_threshold_re_arms_silently_and_persists_it`
  (`:309`) never asserts that the item was alerted after the raise to 10. With the "no save"
  mutant, it passes, because the flag was never stored. Another test kills that mutant, so this
  only weakens the coverage. Add `assert alerted(inv, STOCKED)` after the first `execute(10)`.

## Verification

Verified by the main session on 2026-10-15, against `feat/inventory-low-stock-email` at
`6fafc80`.
- **Environment:** the API (`just api`, port 8100), the worker (`just worker`) and the compose
  Mailpit (UI at 8026) ran on the development database. `just db-migrate` ran
  `0014 -> 0015, inventory low stock alert`.
- **Driver:** the session-scratchpad script `qa_inv/qa_low_stock.py` drove the admin API and
  the container's commands, then read the outbox, `inventory.stock_items` and Mailpit's API.
- **Recipients:**
  - there were three active owners (`example@example.com`, `verifier007@example.test`,
    `verifier@example.test`);
  - the script created a QA staff user and a QA owner, then deactivated the owner, through
    `CreateUser` and `DeactivateUser`.
- **Presentations used:**
  - A = QA Confirma 30 ml, on hand 5;
  - A50 = 50 ml, on hand 20;
  - B = 100 ml, on hand 1;
  - C = QA Existencias 30 ml, on hand 4;
  - the global threshold was 3.

**Suites:** the review closed with `just check` green (2455 unit, 685 harness),
`just test-integration` green (697 passed, 5 skipped), and `plans-scope` clean. Only the docs
changed since.

**Flows driven against the acceptance criteria**
- [x] **Deploy.** After the migration, `low_alerted` was true only for B (on hand 1, already
  low). A, A50 and C were false.
- [x] **First drop.**
  - A lost 2 (5 → 3), and exactly one `inventory.stock.went_low` row was written, with payload
    `{"lines": [{"presentation_id": A, "on_hand": 3, "threshold": 3}]}`.
  - Within 12 s, Mailpit held one "Stock bajo en La Fragancia Ideal" email per active owner
    (3).
  - The body listed "- QA Versace QA Confirma EDP 30 ml: 3 en almacén (umbral 3)", with the
    exact Spanish copy.
- [x] **No repeat.** 3 → 2 sent no email.
- [x] **Re-arm.** A restock to 6, then a loss to 3, sent 3 emails again.
- [x] **Reservations don't alert.** C (on hand 4) reserved 1 (available 3). The reservation was
  expired by the real worker after 22 s, and no email was sent.
- [x] **A sale alerts.** Reserving 1 of C and committing it (on hand 4 → 3) sent 3 emails.
- [x] **One per operation.**
  - A and C were restocked to 10, and A50 stayed at 20.
  - `RecordImportedCounts {A: 0, A50: 0, C: 0}` wrote **one** `went_low` row and sent one email
    per owner (3).
  - Each email listed all three presentations at "0 en almacén (umbral 3)".
- [x] **Threshold change.**
  - A50 was restocked from 0 to 6, which re-armed it (`low_alerted` false).
  - Raising its threshold to 10 sent 3 emails.
  - Clearing the override (back to 3) re-armed it with no email.
- [x] **Recipients.** The QA staff user and the deactivated QA owner received none of the
  emails.
- [x] **Logs.** There were no error, warning, `low_stock_email_rejected` or `low_stock_no_owner`
  lines in the API or worker logs.

**NOT VERIFIED live** (covered by tests only):
- **The import through the real Excel confirm.** The "one per operation" case called
  `RecordImportedCounts`, the command the `catalog.import.applied` subscriber runs, directly.
  The confirm-to-counts path itself was verified live in plan 005.
- **A recipient rejected by the mail server, and temporary SMTP failures.** Mailpit cannot
  refuse a recipient. Both are covered by the loopback SMTP stub in the integration tests.

**Data left in the development database:**
- the QA users `qa004-staff-…@example.test` (staff) and `qa004-off-…@example.test` (deactivated
  owner);
- changed stock: A 0, A50 6, B 1, C 0;
- 15 low-stock emails in Mailpit.

**Verdict: verified.** The plan is ready for the user to mark it `done`.
