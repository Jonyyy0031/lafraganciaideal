---
status: review
module: orders
min_implementer: mid
depends_on: ["003"]
---

# 004 — Order lifecycle in the panel

## Context

Orders exist since plan 003, but nothing moves them. The panel needs to:
- list and open orders;
- move them through their states (decision 9);
- cancel them (decisions 10 and 24);
- mark them paid by hand until the payments initiative exists.

The page the customer sees, reached by private link or by number and email, is now plan 005, and
the emails move to plan 006 (decision 38). This plan applies decisions 9, 10, 19 and 24, and the
new 32–38.

**What exists today:**
- **The order aggregate.** `Order` has the single status `PENDING_PAYMENT`, with `place` as its
  only transition (`apps/api/src/fragancia_api/modules/orders/domain/order.py:27-28`,
  `:114-200`).
- **Storage.**
  - The rows are `orders.orders` and `orders.order_lines`
    (`modules/orders/infrastructure/tables.py:60-120`).
  - The repository has `next_number` and `add` only
    (`modules/orders/infrastructure/sql_order_repository.py:20-98`).
  - The reader has `placed_from_cart` (`:100-128`).
- **Inventory's facade** `StockReservations` (`modules/inventory/application/stock_reservations.py:19-72`).
  - `commit` sells the units with one `sale` movement per line. It is idempotent. A late commit
    (expired or released) needs the units available, otherwise `INVENTORY_STOCK_UNAVAILABLE`
    (`modules/inventory/application/commands/reservations.py:167-256`).
  - `release` is idempotent on released and expired reservations, and refuses a committed one
    (`:259-308`).
  - There is no way back from `committed`. The ledger already has a `return` kind, which adds
    units (`modules/inventory/domain/stock.py:67-73`, `:205-227`).
  - Reservation statuses are active, committed, released and expired
    (`modules/inventory/domain/reservation.py:39-43`). The status column is `varchar(12)`
    without a check (`modules/inventory/infrastructure/tables.py:62-79`).
  - Lock order: the reservation row(s) first, then the settings row (share), then stock items by
    id (`reservations.py:1-20`).
- **Inventory's expiry job** expires reservations every minute (`modules/inventory/module.py:166-178`).
- **Permissions** are atomic strings in `ROLE_PERMISSIONS`
  (`modules/identity/domain/user.py:80-91`), checked per router or per route with
  `require_permission` (`apps/api/src/fragancia_api/shared/http/access.py:43-50`). The admin
  actor carries `id` and `permissions` (`shared/application/actor.py:6-18`); an inventory route
  reads `request.state.actor.id` (`modules/inventory/http/router.py:72`).
- **Paging** uses `Page[T]` with `page`/`size` limits (`shared/contracts/__init__.py:1-20`), as
  in the catalog admin perfume list (`modules/catalog/http/perfume_router.py:75-84`).
- **Error mapping.** `ConflictError` gives 409; `InvalidValueError` and
  `BusinessRuleViolationError` give 422; `NotFoundError` gives 404
  (`shared/http/errors.py:50-57`).

**Approach.**
- **The `Order` aggregate gains the full state machine:**

  | From | To |
  | ---- | -- |
  | pending_payment | paid, cancelled |
  | paid | preparing (optional), shipped, ready_for_pickup, cancelled |
  | preparing | shipped, ready_for_pickup |
  | shipped, ready_for_pickup | delivered |

  States only move forward (decision 35). Every change appends a history row with who did it,
  when, and the data entered, and publishes `orders.order.status_changed` for the emails
  (plan 006).
- **Stock follows the order:**
  - marking paid commits the reservation;
  - cancelling an unpaid order releases it;
  - cancelling a paid order returns the units through a new inventory operation,
    `return_sold`, which records `return` movements, and leaves the refund pending (decision 36).
- **A worker job** cancels unpaid orders whose `payment_due_at` passed (decision 24).
- **Permissions.**
  - `orders:manage` (owners and staff) covers the list, the detail, the forward moves, notes and
    cancelling an unpaid order.
  - The new `orders:finance` (owners only) covers marking paid, cancelling a paid order and
    marking the refund done (decisions 32 and 34).
  - Cancelling a paid order has its own route, so both permissions are declared per route and
    never inferred from the order's state.
- **Rejected:**
  - One generic `PATCH` with a target status: the moves need different data and permissions.
  - Releasing a committed reservation inside inventory: `release` means "the payment failed",
    while a return is a different ledger fact.

## Out of scope

- **The customer's order page,** by link or by number and email: plan 005.
- **The emails:** plan 006. This plan only publishes `orders.order.status_changed`.
- **The payments initiative:**
  - the Mercado Pago webhook moving an order to paid;
  - automatic refunds;
  - extending the reservation for OXXO and SPEI.
- **Moving a state backwards** (decision 35); a mistake is explained with a note.
- **Cancelling after `paid`** (preparing, shipped or delivered), as decision 9 says. Returns
  after delivery are a later initiative.
- **Showing the name of who made a change.** History and notes store the admin's user id. The
  panel can resolve names later through identity.
- **Choosing which units go back to stock** on a paid cancellation: they all go back
  (decision 36).

## Dependencies

- **003 (done):** the `Order` aggregate and rows, `SqlOrderRepository` and `SqlOrderReader`, and
  `StockHoldsAdapter`.
- **inventory-stock/003 (done):** `StockReservations` (`commit`, `release`).
- **identity:** `ROLE_PERMISSIONS`, which this plan extends.

## Steps

1. **Inventory: return the units of a sold reservation**
   - Files: `apps/api/src/fragancia_api/modules/inventory/domain/reservation.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/domain/errors.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/application/commands/reservations.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/application/stock_reservations.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/module.py` (modify),
     `apps/api/src/fragancia_api/modules/inventory/__init__.py` (modify)
   - Do:
     - **Status.** `ReservationStatus.RETURNED = "returned"`.
       - `Reservation.mark_returned(now)` raises unless the reservation is `COMMITTED`.
       - `return_reason()` returns `f"Devolución: {self.reference}"`.
     - **Error.** `ReservationNotCommitted(ConflictError)`, with code
       `INVENTORY_RESERVATION_NOT_COMMITTED`.
     - **`ReturnReservation`,** in the style of `CommitReservation` (`reservations.py:167-256`):
       1. Lock the reservation row.
       2. `RETURNED` gives `Ok`, changing nothing (idempotent).
       3. Any status other than `COMMITTED` refuses `ReservationNotCommitted`.
       4. Take the settings (share) and the stock items by id.
       5. Per line, call `item.record_movement(MovementKind.RETURN, quantity, reason,
          SALE_ACTOR, now)`.
       6. Run `mark_returned` and save.
       7. Publish `StockChanged` per item. A return never makes stock go low, so there is no
          `track_low`.
       8. Update the module docstring's lock-order list to include it.
     - **The facade.** `StockReservations` gains a `return_sold(reservation_id)` method that
       documents the rules, and `module.py` wires the command. The `__init__.py` docstring
       mentions it.
   - Observable result: unit and integration tests show the following.
     - A committed reservation's units come back on hand, with one `return` movement per line
       carrying the reason "Devolución: order:<id>".
     - A second call changes nothing.
     - An active, released or expired reservation is refused.

2. **Identity: the order permissions**
   - Files: `apps/api/src/fragancia_api/modules/identity/domain/user.py` (modify)
   - Do: add `ORDERS_MANAGE = "orders:manage"` to owner and staff, and
     `ORDERS_FINANCE = "orders:finance"` to owner only (decisions 19, 32 and 34).
   - Observable result: the identity permission tests list both.

3. **Orders domain: the state machine**
   - Files: `apps/api/src/fragancia_api/modules/orders/domain/order.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/domain/errors.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/domain/repositories.py` (modify)
   - Do:
     - **`OrderStatus`** gains `PAID = "paid"`, `PREPARING = "preparing"`, `SHIPPED = "shipped"`,
       `READY_FOR_PICKUP = "ready_for_pickup"`, `DELIVERED = "delivered"` and
       `CANCELLED = "cancelled"`.
     - **`RefundStatus(StrEnum)`** has `PENDING = "pending"` and `DONE = "done"`.
     - **New fields on `Order`:**
       - `cancel_reason: str | None`;
       - `refund_status: RefundStatus | None`;
       - `shipping_carrier: str | None` and `tracking_number: str | None`.
     - **`OrderEvent`,** frozen, is one history row:
       - `at`, `from_status`, `to_status`;
       - `actor` (an admin user id, or `"system"`);
       - `note: str | None`, `carrier: str | None`, `tracking_number: str | None`.

       `Order` keeps `new_history: list[OrderEvent]`, appended by each transition and written by
       the repository. Older history rows are not loaded on the write side.
     - **Methods.** Each returns `Result[None, DomainError]`, sets `updated_at`, appends an
       `OrderEvent`, and records `OrderStatusChanged(order_id, number, from_status, to_status)`
       with `name = "orders.order.status_changed"`:
       - `mark_paid(note, actor, now)`: from `PENDING_PAYMENT`. `note` is required, 1–300
         characters after collapsing whitespace (decision 32).
       - `start_preparing(actor, now)`: from `PAID`.
       - `ship(carrier, tracking_number, actor, now)`: from `PAID` or `PREPARING`, never for
         pickup. For `home`, the carrier and tracking number are required, 1–60 characters each.
         For `local`, both must be absent (decision 33).
       - `ready_for_pickup(actor, now)`: from `PAID` or `PREPARING`, `pickup` only.
       - `deliver(actor, now)`: from `SHIPPED` or `READY_FOR_PICKUP`.
       - `cancel_unpaid(reason, actor, now)`: from `PENDING_PAYMENT`, with a reason of 1–300
         characters.
       - `cancel_paid(reason, actor, now)`: from `PAID`, with a reason of 1–300 characters. It
         sets `refund_status = PENDING` (decision 36).
       - `mark_refunded(note, actor, now)`: needs `CANCELLED` and `refund_status == PENDING`.
         It sets `DONE`, and the history row keeps the status cancelled → cancelled with the
         note.
     - **`AUTO_CANCEL_REASON = "No pagado a tiempo"`** and `SYSTEM_ACTOR = "system"`.
     - **New errors:**

       | Error | Code | Status | `details` |
       | ----- | ---- | ------ | --------- |
       | `OrderNotFound(NotFoundError)` | `ORDERS_ORDER_NOT_FOUND` | 404 | — |
       | `OrderTransitionInvalid(ConflictError)` | `ORDERS_TRANSITION_INVALID` | 409 | `{"from": <status>, "to": <status>}` |
       | `OrderDataInvalid(InvalidValueError)` | `ORDERS_ORDER_DATA_INVALID` | 422 | `{"field": ...}` |

       `OrderDataInvalid` covers the note, the reason, the carrier and the tracking number, a
       carrier given for local delivery, and a missing one for home delivery. Messages are in
       Spanish, like plan 003's.
     - **`OrderRepository`** gains:
       - `get_by_number_for_update(number)`;
       - `claim_overdue(now, limit)`, which takes `pending_payment` orders with
         `payment_due_at < now`, locked `FOR UPDATE SKIP LOCKED`;
       - `save(order)`, which updates the order row and inserts `new_history`.
   - Observable result: domain tests run every allowed move and refuse every disallowed pair
     (the full 7×7 matrix). They also cover the delivery method rules for ship and pickup, the
     note, reason and carrier bounds, the refund flag, and one event per move.

4. **Orders application: commands, queries and the job**
   - Files: `apps/api/src/fragancia_api/modules/orders/application/commands/lifecycle.py` (create),
     `apps/api/src/fragancia_api/modules/orders/application/queries/orders.py` (create),
     `apps/api/src/fragancia_api/modules/orders/application/ports.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/contracts.py` (modify)
   - Do:
     - **The `StockHolds` port** (`ports.py`) gains three methods, each returning
       `Result[None, DomainError]`:
       - `commit(reservation_id)`;
       - `release(reservation_id)`;
       - `return_sold(reservation_id)`.
     - **The `OrderQueries` port** has:
       - `page(status, q, page, size) -> AdminOrderPage`;
       - `detail(number) -> AdminOrderDetail | None`.
     - **Commands.** Each runs one `transactions.run` that locks the order with
       `get_by_number_for_update`, then calls inventory when needed (the order row first, then
       inventory's own order), saves, and publishes the order's events:
       - **`MarkOrderPaid(number, note, actor)`:** `mark_paid`, then `stocks.commit` when there
         is a reservation. A refusal (`INVENTORY_STOCK_UNAVAILABLE` or `_INSUFFICIENT`, an
         expired reservation with no units left) passes through as 409 and rolls back the order
         (decision 32).
       - **`AdvanceOrder(number, to, carrier, tracking_number, actor)`:** `to` is one of
         preparing, shipped, ready_for_pickup or delivered, dispatched to the matching method.
       - **`CancelUnpaidOrder(number, reason, actor)`:** `cancel_unpaid`, then `stocks.release`
         when there is a reservation (idempotent on expired).
       - **`CancelPaidOrder(number, reason, actor)`:** `cancel_paid`, then `stocks.return_sold`
         when there is a reservation.
       - **`MarkOrderRefunded(number, note, actor)`.**
       - **`AddOrderNote(number, text, actor)`:** text of 1–1000 characters. It goes into its
         own table, takes no lock on the order, and refuses an unknown number with
         `ORDERS_ORDER_NOT_FOUND`.
       - **`CancelOverdueOrders.run_once()`,** the job (decision 24):
         1. `claim_overdue(now, 100)`.
         2. Per order: `cancel_unpaid(AUTO_CANCEL_REASON, SYSTEM_ACTOR, now)`, then
            `stocks.release`, then save and publish.
         3. All in one transaction; it returns the count.
     - **Queries:**
       - **`ListOrders(status, q, page, size)`.** `q` matches `LFI-1001`, `1001`, an email
         substring or a name substring, case-insensitively. Newest number first.
       - **`GetOrder(number)`.** It returns the detail: header, lines, address, pickup point,
         invoice, the full history oldest first, notes newest first, the refund status, the
         carrier and tracking, and `reservation_id`.
     - **Contracts:**
       - `AdminOrderSummary` (number, created_at, customer name and email, status,
         delivery_method, total_cents, refund_status) and `AdminOrderPage`;
       - `AdminOrderDetail`, `AdminOrderLine`, `AdminOrderEvent` and `AdminOrderNote`;
       - the request bodies `MarkPaidRequest(note)`,
         `AdvanceOrderRequest(to, carrier, tracking_number)`, `CancelOrderRequest(reason)`,
         `RefundDoneRequest(note)` and `OrderNoteRequest(text)`.

       Bounds only; the rules live in the domain.
   - Observable result: application tests with fakes cover every command's happy path, which
     inventory call it makes (or none when the order has no reservation), the rollback when
     inventory refuses, and the job claiming only overdue unpaid orders.

5. **Orders infrastructure: tables, migration, adapters**
   - Files: `apps/api/src/fragancia_api/modules/orders/infrastructure/tables.py` (modify),
     `apps/api/migrations/versions/0021_orders_lifecycle.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_order_repository.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_order_queries.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/stock_holds.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/in_memory.py` (modify)
   - Do:
     - **New `orders.orders` columns:**
       - `cancel_reason` (300) and `refund_status` (10), with check `IN ('pending', 'done')`;
       - `shipping_carrier` (60) and `tracking_number` (60);
       - a partial index on `payment_due_at` where `status = 'pending_payment'`, for the job;
       - an index on `status`;
       - check `status IN (...)` with the seven values.
     - **`orders.order_events`:**
       - `id` (uuid, PK), `order_id` (FK cascade, indexed) and `at`;
       - `from_status` and `to_status` (varchar 30);
       - `actor` (varchar 64);
       - nullable `note` (300), `carrier` (60) and `tracking_number` (60).
     - **`orders.order_notes`:**
       - `id` (uuid, PK), `order_id` (FK cascade, indexed) and `at`;
       - `actor` (varchar 64) and `text` (1000).
     - **Migration `0021_orders_lifecycle`:** autogenerate, tidy by hand, reversible.
     - **`SqlOrderRepository`:**
       - `get_by_number_for_update` uses the existing mapper;
       - `claim_overdue` runs `SELECT … FOR UPDATE SKIP LOCKED LIMIT`;
       - `save` updates the status, the new columns and `updated_at`, then inserts the history
         rows;
       - `add_note`.
     - **`SqlOrderQueries`** has `page` and `detail` (`Database.reader()`).
     - **`StockHoldsAdapter`** gains `commit`, `release` and `return_sold`, which pass the result
       through unchanged.
     - **In-memory:** the in-memory fakes implement the new ports, and `InMemoryStockHolds`
       records each call.
   - Observable result: integration tests save a transition with its history, claim overdue
     orders, page and filter, and run the migration round trip on a scratch database.

6. **HTTP, wiring, job, docs**
   - Files: `apps/api/src/fragancia_api/modules/orders/http/router.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/module.py` (modify),
     `apps/api/openapi.json` (modify),
     `docs/architecture.md` (modify),
     `apps/api/src/fragancia_api/modules/orders/__init__.py` (modify)
   - Files: `apps/api/tests/unit/orders/` (modify), `apps/api/tests/integration/orders/` (modify),
     `apps/api/tests/unit/inventory/` (modify), `apps/api/tests/integration/inventory/` (modify),
     `apps/api/tests/unit/identity/` (modify), `apps/api/tests/integration/identity/` (modify)
   - Do:
     - **Router.** `orders_admin = admin_router(prefix="/orders", tags=["orders · admin"],
       dependencies=[Depends(require_permission("orders:manage"))])`. The routes `payment`,
       `cancel-paid` and `refund` also declare `Depends(require_permission("orders:finance"))`.

       | Route | Answers | Body |
       | ----- | ------- | ---- |
       | `GET ""` | `AdminOrderPage` | query: `status`, `q`, `page`, `size` |
       | `GET "/{number}"` | `AdminOrderDetail` | — |
       | `POST "/{number}/payment"` | 204 | `MarkPaidRequest` |
       | `POST "/{number}/advance"` | 204 | `AdvanceOrderRequest` |
       | `POST "/{number}/cancel"` | 204 | `CancelOrderRequest`; unpaid only |
       | `POST "/{number}/cancel-paid"` | 204 | `CancelOrderRequest` |
       | `POST "/{number}/refund"` | 204 | `RefundDoneRequest` |
       | `POST "/{number}/notes"` | 201 | `OrderNoteRequest` |

       - `{number}` accepts `LFI-1001` or `1001`; anything else is 404.
       - The actor comes from `request.state.actor.id`.
       - Each docstring lists its error codes and decisions.
     - **Wiring.** Wire the commands, the queries and the job
       `ScheduledJob("orders_cancel_overdue", …, 60, 120)` in `module.py`.
     - **Docs.**
       - Regenerate `openapi.json`.
       - Extend the orders row of `docs/architecture.md`: states, the job, `return_sold`,
         permissions and the event.
       - Document `orders.order.status_changed` in the orders `__init__.py` docstring.
     - The tester writes the layered tests.
   - Observable result: `uv run just check` and `uv run just test-integration` pass, and the
     routes are in OpenAPI.

## Acceptance criteria

Run with the API and the worker on the development database. The QA perfume and delivery
options are the ones from plan 003. Orders are placed through `POST /checkout`.

- [ ] **List and detail.**
  - As staff, `GET /admin/orders?status=pending_payment` lists the new orders, newest first;
    `q=LFI-` with a number, an email fragment or a name fragment finds them.
  - `GET /admin/orders/LFI-n` shows the lines, the address and the history with one row per
    move.
  - Anonymous gets 401.
- [ ] **Paid by hand.**
  - Staff `POST …/payment` gets 403.
  - Owner with note "Transferencia directa" gets 204. The status is `paid`, the reservation is
    `committed`, and a `sale` movement exists per in-stock line; `on_hand` fell and `reserved`
    fell.
  - One `orders.order.status_changed` outbox row exists.
- [ ] **Forward moves.**
  - A home order goes paid → shipped. Without carrier or tracking it gets 422; with "Estafeta" /
    "123456" it gets 204 and they are stored. Then delivered.
  - A pickup order goes paid → preparing → ready_for_pickup → delivered.
  - `shipped` on a pickup order gets 409 `ORDERS_TRANSITION_INVALID`.
  - Any move backwards (delivered → paid) gets 409.
- [ ] **Cancel unpaid.**
  - Staff cancel with a reason gives 204. The status is `cancelled` with the reason, and the
    reservation is `released` (units available again).
  - `cancel` on a paid order gets 409.
- [ ] **Cancel paid.**
  - Staff `cancel-paid` gets 403.
  - Owner gets 204: the status is `cancelled`, `refund_status` is `pending`, the reservation is
    `returned`, and one `return` movement per in-stock line puts `on_hand` back.
  - `refund` with a note gives `refund_status: done`; a second call gets 409.
- [ ] **Overdue.** An unpaid order whose `payment_due_at` is set 1 minute in the past is cancelled
  within a minute by the worker, with reason "No pagado a tiempo", actor `system`, and the
  reservation released (or already expired).
- [ ] **Notes.** A note shows in the detail, newest first, with its author id.
- [ ] **Made to order only.** Marking paid and cancelling an order without a reservation makes no
  inventory call and succeeds.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | The full transition matrix; per-method rules (home carrier/tracking required, local forbidden, pickup only for ready_for_pickup); note, reason and text bounds; refund flag; history row and event per move; inventory `Reservation.mark_returned` |
| application | yes     | Each command: the inventory call it makes (none without a reservation), refusal passthrough and rollback, not found, the job's claim and reason; `ReturnReservation` idempotence and refusals |
| http        | yes     | Permissions per route (staff 403 on payment, cancel-paid, refund; anonymous 401), number parsing, every status and error code, OpenAPI |
| integration | yes     | `SqlOrderRepository` save, history and `claim_overdue` with `SKIP LOCKED`; `SqlOrderQueries` filters and paging; the end-to-end moves over real inventory (commit, release, return movements and on_hand); the job; migration 0021 round trip on a scratch database |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

All cosmetic; fixed forward.

1. **Existing tests updated to stay green.** The plan lists test directories only for the
   tester, but the new permissions, the seven statuses, the second orders job and the new
   `StockReservations` constructor argument broke existing assertions. Only expectations were
   updated (no new tests): `apps/api/tests/unit/identity/{test_auth_http,test_resolve_session_actor,test_sessions,test_team_http,test_user_domain}.py`,
   `apps/api/tests/integration/identity/{test_sql_identity,test_sql_account_links}.py`,
   `apps/api/tests/unit/inventory/support.py` (builds `ReturnReservation`),
   `apps/api/tests/unit/orders/{test_cart_wiring,test_order_domain}.py` and
   `apps/api/tests/integration/orders/{test_sql_carts,test_sql_orders}.py`.
   `plans-scope` lists these as out of scope; the integration/identity ones are outside the
   plan's declared test directories.
2. **Inventory commit/release of a `returned` reservation.** Adding `RETURNED` to
   `ReservationStatus` would have let `CommitReservation` sell a returned reservation again
   (double count) and left `ReleaseReservation`'s `match` non-exhaustive. `CommitReservation`
   now refuses a returned one with `INVENTORY_RESERVATION_NOT_ACTIVE`, and `ReleaseReservation`
   refuses it with `INVENTORY_RESERVATION_COMMITTED` (it was sold). Documented in the
   `StockReservations` docstring.
3. **`OrderRepository.add_note`** is on the port (the plan listed it only under the SQL
   adapter), and the in-memory repository keeps `history` and `notes` dicts for tests.
4. **Extra domain helpers** in `orders/domain/order.py`: `parse_order_number`,
   `clean_note_text` (notes keep line breaks, 1 to 1000 characters) and `ALLOWED_TRANSITIONS`.
5. **`CancelOverdueOrders`** skips (and logs) an order whose cancel or release is refused
   instead of failing the whole batch; the others are still cancelled in the same transaction.
6. **Check constraints** `ck_orders_status` and `ck_orders_refund_status` were added to
   migration 0021 by hand (autogenerate does not detect them). `alembic check` reports no drift.
7. **Ordering of permission lists** in tests is alphabetical (`orders:finance`,
   `orders:manage` between `inventory:manage` and `outbox:manage`).
8. Cancelled orders store the reason also as the history row's `note`.

## Test coverage

Baseline (before any test): `uv run just check` 3097 passed, 3 skipped, nothing failing;
`uv run just test-integration` 883 passed, 5 skipped. Closing: `uv run just check` 3410 passed,
3 skipped (+313); `uv run just test-integration` 981 passed, 5 skipped (+98). No GAP and no
NOT CONFIRMED were needed: everything below was confirmed from code or from local execution.
No product code was edited.

Source paths are under `apps/api/src/fragancia_api/modules/`; test paths under
`apps/api/tests/`.

| Behavior | Source | Layer | Test | State |
| -------- | ------ | ----- | ---- | ----- |
| `RETURNED` status, `mark_returned` only from `COMMITTED`, `return_reason` | `inventory/domain/reservation.py:45,131,154` | domain | `unit/inventory/test_reservation_return_domain.py` | CONFIRMED |
| `ReturnReservation`: one `return` movement per line, reason `Devolución: <ref>`, actor system, `StockChanged`, idempotent, refuses active/released/expired, not found | `inventory/application/commands/reservations.py:317-380` | application | `unit/inventory/test_return_reservation.py` | CONFIRMED |
| Deviation 2: `CommitReservation` refuses a returned reservation (`INVENTORY_RESERVATION_NOT_ACTIVE`, nothing sold again) and `ReleaseReservation` refuses it (`INVENTORY_RESERVATION_COMMITTED`) | `reservations.py:204,293` | application (in-memory) and integration (real SQL) | `test_return_reservation.py::test_commit_of_a_returned_...`, `::test_release_of_a_returned_...`, `::test_the_facade_refuses_commit_and_release_...`; `integration/orders/test_order_lifecycle_flow.py::test_a_returned_reservation_is_idempotent_to_return_and_refuses_commit_and_release` | CONFIRMED |
| `return_sold` over real inventory: units back on hand, one `return` movement per in-stock line, reservation `returned`, refused when never sold | `stock_reservations.py`, `reservations.py:317` | integration | `test_order_lifecycle_flow.py::test_cancelling_a_paid_order_returns_the_units_...`, `::test_returning_a_reservation_that_was_never_sold_...` | CONFIRMED |
| Permissions `orders:manage` (owner, staff) and `orders:finance` (owner) | `identity/domain/user.py:80-91` | domain / http | updated by the implementer in `unit/identity/test_user_domain.py` and the identity http and integration tests (deviation 1); not rewritten here | CONFIRMED |
| Full 7x7 transition matrix; refusals carry `{"from","to"}`; table equals the plan's | `orders/domain/order.py:62-78,427-431` | domain | `unit/orders/test_order_lifecycle_domain.py::test_the_transition_matrix_...` (49 cases), `::test_the_module_transition_table_matches_the_plan_table` | CONFIRMED |
| `mark_paid`: note required, 1-300 after collapsing whitespace, transition checked before note, history row and `orders.order.status_changed` | `order.py:319-329` | domain | `test_order_lifecycle_domain.py::test_mark_paid_*` | CONFIRMED |
| `ship`: home needs carrier and tracking (1-60), local forbids both, pickup never ships; `ready_for_pickup` pickup only; `deliver`; `start_preparing` | `order.py:331-381` | domain | `test_order_lifecycle_domain.py` (ship, ready_for_pickup, deliver sections) | CONFIRMED |
| `cancel_unpaid` / `cancel_paid`: reason 1-300, refund pending only for paid, status rules | `order.py:383-408` | domain | `test_order_lifecycle_domain.py` (cancel section) | CONFIRMED |
| `mark_refunded`: needs cancelled + pending, history row cancelled to cancelled, second mark refused | `order.py:410-425` | domain | `test_order_lifecycle_domain.py` (mark_refunded section) | CONFIRMED |
| `parse_order_number`, `clean_note_text` (deviation 4) | `order.py:84-97` | domain | `test_order_lifecycle_domain.py::test_parse_order_number`, `::test_clean_note_text_*` | CONFIRMED |
| Each lifecycle command: which inventory call (none without a reservation), refusal passes through with the order unsaved, not found, invalid move or data calls no inventory | `orders/application/commands/lifecycle.py:56-185` | application | `unit/orders/test_order_lifecycle.py` | CONFIRMED |
| `AddOrderNote`: stored with author and time, validates text before lookup, unknown number | `lifecycle.py:162-185` | application | `test_order_lifecycle.py::test_adding_a_note_*`, `::test_a_note_*` | CONFIRMED |
| Job claims overdue unpaid orders only, reason "No pagado a tiempo", actor system, release per reservation, batch 100, count | `lifecycle.py:188-227` | application | `test_order_lifecycle.py::test_the_job_*` | CONFIRMED |
| Deviation 5: a refused release is skipped and logged (`orders.overdue_release_refused`), the others still cancel | `lifecycle.py:207-217` | application and integration | `test_order_lifecycle.py::test_deviation_5_a_refused_release_...`, `::test_deviation_5_when_every_release_...`; `test_order_lifecycle_flow.py::test_deviation_5_the_job_skips_and_logs_...` (real SQL) | CONFIRMED |
| Deviation 5: a refused cancel is skipped and logged (`orders.overdue_cancel_refused`) without releasing | `lifecycle.py:199-206` | application | `test_order_lifecycle.py::test_deviation_5_a_refused_cancel_...` (stale claim) | CONFIRMED |
| `StockHoldsAdapter.commit/release/return_sold` pass inventory's result through unchanged | `orders/infrastructure/stock_holds.py` | application | `unit/orders/test_stock_holds_lifecycle_adapter.py` | CONFIRMED |
| Routes: anonymous 401, no orders permission 403, staff 403 on payment/cancel-paid/refund (and finance alone not enough), 204/201 statuses, 404 for unparseable or unknown number, 409/422 codes and `details`, actor from the session, query bounds | `orders/http/router.py:211-346` | http | `unit/orders/test_order_lifecycle_http.py` | CONFIRMED |
| OpenAPI declares the eight operations, their success status and 404/409/422, and the order models | `router.py`, `contracts.py:180-320` | http | `test_order_lifecycle_http.py::test_the_order_operations_...`, `::test_the_openapi_*` | CONFIRMED |
| `SqlOrderRepository.save` (status, new columns, history rows, no double insert, rollback), `get_by_number_for_update` | `sql_order_repository.py:120-160` | integration | `integration/orders/test_sql_order_lifecycle.py` | CONFIRMED |
| `claim_overdue`: unpaid and `payment_due_at < now` only (exact `now` excluded), oldest due first, limit, `SKIP LOCKED` against another claim and against an order being moved | `sql_order_repository.py:104-118` | integration | `test_sql_order_lifecycle.py::test_claim_*`, `::test_a_second_claim_skips_...` | CONFIRMED |
| `add_note`: stores, unknown number false, no write to the order row | `sql_order_repository.py:162-175` | integration | `test_sql_order_lifecycle.py::test_add_note_*`, `::test_a_note_does_not_touch_the_order_row` | CONFIRMED |
| "Takes no lock on the order" (`AddOrderNote`) | `lifecycle.py:162-185` | integration | `test_sql_order_lifecycle.py::test_a_note_waits_for_a_move_in_flight_...` | CONFIRMED with a caveat (see Findings below) |
| `SqlOrderQueries.page`: status filter, search by `LFI-n` / `n` / email / name ignoring case, LIKE wildcards literal, newest first, paging and total | `sql_order_queries.py:36-77` | integration | `test_sql_order_lifecycle.py::test_the_list_*`, `::test_the_search_*`, `::test_paging_*` | CONFIRMED |
| `SqlOrderQueries.detail`: header, lines, address/pickup/invoice, history oldest first, notes newest first, refund, carrier, reservation id; None when unknown | `sql_order_queries.py:79-` | integration | `test_sql_order_lifecycle.py` (detail section) | CONFIRMED |
| Paid by hand over real inventory: commit, `sale` movement per in-stock line, `on_hand`/`reserved`, history, outbox `status_changed` payload; rollback when inventory refuses; late commit of an expired reservation; two payments at once sell once | `lifecycle.py:84-93` | integration | `test_order_lifecycle_flow.py` (paid by hand section) | CONFIRMED |
| Made-to-order-only orders pay, cancel and cancel overdue without touching inventory | `lifecycle.py:71` | integration | `test_order_lifecycle_flow.py::test_an_order_without_a_reservation_*`, `::test_the_job_cancels_an_overdue_order_without_a_reservation` | CONFIRMED |
| Forward moves and backwards refusal over real adapters | `lifecycle.py:96-125` | integration | `test_order_lifecycle_flow.py` (forward moves section) | CONFIRMED |
| Cancel unpaid releases (also an expired reservation); cancel paid returns units and leaves refund pending; refund done once | `lifecycle.py:128-159` | integration | `test_order_lifecycle_flow.py` (cancel sections) | CONFIRMED |
| The job over real SQL: cancels overdue unpaid with reason/actor/events, leaves not-overdue and paid orders, two concurrent runs cancel each order once | `lifecycle.py:188-227` | integration | `test_order_lifecycle_flow.py::test_the_job_*`, `::test_two_runs_of_the_job_at_once_...` | CONFIRMED |
| Migration 0021 round trip on a scratch database: absent at 0020; four nullable columns; `order_events` and `order_notes` columns; indexes (the unpaid one partial); status and refund checks accept the seven statuses / pending, done, null and refuse anything else; FK cascade; orders stored before keep their data; downgrade removes everything and keeps orders; upgrade after downgrade rebuilds an identical schema snapshot | `migrations/versions/0021_orders_lifecycle.py` | integration | `integration/orders/test_migration_0021_orders_lifecycle.py` (28 tests) | CONFIRMED |
| e2e | n/a | e2e | no e2e infrastructure yet (plan table: no) | n/a |

Tests added per layer (parametrized cases counted): domain 129 in orders (49 are the matrix)
plus 9 in inventory; application 54 in orders, 12 in inventory and 6 in the adapter file; http
103; integration 98 (28 migration, 47 repository and queries, 23 flow). Unit total +313 over the
baseline, integration +98.

Observations for the reviewer (no product code was touched, nothing here is a GAP):

- **`add_note` is not lock-free in practice.** The command takes no explicit lock, as the plan
  says, but the `order_notes.order_id` foreign key makes PostgreSQL take a key-share lock on the
  order row, which waits for a `FOR UPDATE` holder. So a note added while a move on the same
  order is in flight waits until that move commits (no deadlock; a move waiting on a note waits
  only for the note's short transaction). The test pins this observed behavior.
- **`InMemoryOrders.save` keeps recorded events on its stored copy.** The SQL adapter does not,
  so in the in-memory fake a second command on the same order publishes the earlier event
  again. Tests that run several commands on one order assert on the last event only
  (`test_marking_the_refund_done_...`). A fake-only artifact, not product behavior.
- The migration test was run for real on scratch databases (`fragancia_mig_<random>_test`,
  created and dropped by the fixture); `fragancia_test` and the development database were not
  touched by it.

## Review findings

## Verification
