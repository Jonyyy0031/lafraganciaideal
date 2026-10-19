---
status: done
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

7. **Review repair (added 2026-10-19 after the first review)**
   - Files: `apps/api/src/fragancia_api/modules/orders/application/commands/lifecycle.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_order_repository.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/domain/repositories.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/http/router.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/in_memory.py` (modify),
     `apps/api/openapi.json` (modify),
     `docs/architecture.md` (modify),
     `apps/api/tests/unit/orders/` (modify), `apps/api/tests/integration/orders/` (modify)
   - Do:
     - **M1, a deadlock in the overdue job.**
       - `CancelOverdueOrders.run_once` runs **one transaction per order**. Each one claims a
         single overdue order (`claim_overdue(now, 1)` with `FOR UPDATE SKIP LOCKED`), cancels
         it, releases its reservation, saves and publishes.
       - It loops until nothing is claimed, or 100 orders have been processed in this run, or an
         order is skipped (see L1).
       - Each transaction then holds at most one reservation's stock items, taken in id order,
         so it keeps inventory's global lock order (`reservations.py:1-20`).
       - The class docstring states this.
     - **L1, the stuck queue.** Inside the per-order loop, an order whose cancel or release is
       refused is logged and skipped as today. The loop also stops for the run, so it is not
       re-claimed in a hot loop.
       - Record in the class docstring that such orders are re-claimed every run and can starve
         newer ones once the payments initiative commits reservations from a webhook.
       - The payments initiative must handle this. Add a line to the README's "Dependency notes"
         under Payment.
     - **L2, notes that wait.** `get_by_number_for_update` and `claim_overdue` lock with
       `FOR NO KEY UPDATE` (`with_for_update(key_share=True)`). A note insert, which only takes
       `FOR KEY SHARE` through its foreign key, then no longer waits on a move in flight. The
       docstrings that say a note takes no lock become true. Update the tester's pinned test
       `test_a_note_waits_for_a_move_in_flight_and_lands_once_it_commits` to assert that the
       note lands while the move is still in flight.
     - **L3.** Update the inventory row of `docs/architecture.md`: `return_sold`, the `returned`
       status and `INVENTORY_RESERVATION_NOT_COMMITTED`.
     - **L4.** `InMemoryOrders.save` stores a copy without the pending events: pull them before
       copying, or copy with an empty event list. That way a second command never republishes.
       Remove the workaround in the test that asserts only the last event.
   - Observable result:
     - An integration test runs the job and inventory's `ExpireReservations` concurrently over
       several overdue orders that share presentations in crossed id order, and neither
       deadlocks.
     - An integration test shows a note committing while a move holds the order row.
     - A unit test shows the job processing orders one transaction at a time and stopping after
       a skip.
     - `uv run just check`, `uv run just test-integration` and `plans-scope` are clean.

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
| Job claims overdue unpaid orders only, reason "No pagado a tiempo", actor system, release per reservation, batch 100 in ONE transaction (superseded: now one transaction per order), count | `lifecycle.py:188-227` | application | `test_order_lifecycle.py::test_the_job_*` | CONFIRMED |
| SUPERSEDED 2026-10-19 (see repair round): deviation 5 "the others still cancel" no longer holds; a refused release is logged (`orders.overdue_release_refused`) and the run stops | `lifecycle.py:207-217` | application and integration | `test_order_lifecycle.py::test_deviation_5_a_refused_release_...`, `::test_deviation_5_when_every_release_...`; `test_order_lifecycle_flow.py::test_deviation_5_the_job_skips_and_logs_...` (real SQL) | CONFIRMED |
| SUPERSEDED 2026-10-19 (see repair round; now also stops the run): deviation 5 a refused cancel is skipped and logged (`orders.overdue_cancel_refused`) without releasing | `lifecycle.py:199-206` | application | `test_order_lifecycle.py::test_deviation_5_a_refused_cancel_...` (stale claim) | CONFIRMED |
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

- (SUPERSEDED, see repair round) **`add_note` is not lock-free in practice.** The command takes no explicit lock, as the plan
  says, but the `order_notes.order_id` foreign key makes PostgreSQL take a key-share lock on the
  order row, which waits for a `FOR UPDATE` holder. So a note added while a move on the same
  order is in flight waits until that move commits (no deadlock; a move waiting on a note waits
  only for the note's short transaction). The test pins this observed behavior.
- (SUPERSEDED by L4) **`InMemoryOrders.save` keeps recorded events on its stored copy.** The SQL adapter does not,
  so in the in-memory fake a second command on the same order publishes the earlier event
  again. Tests that run several commands on one order assert on the last event only
  (`test_marking_the_refund_done_...`). A fake-only artifact, not product behavior.
- The migration test was run for real on scratch databases (`fragancia_mig_<random>_test`,
  created and dropped by the fixture); `fragancia_test` and the development database were not
  touched by it.

### Repair round 2026-10-19 (step 7: M1, L1, L2, L4)

Baseline (this round, before my edits): `uv run just check` green; `uv run just
test-integration` 981 passed, 6 skipped. Closing: `check` 3413 passed, 3 skipped; integration
982 passed, 5 skipped (the skip count differs by one between runs; not investigated, no test of
mine is skipped). No GAP, no NOT CONFIRMED. No product code edited. Rows marked SUPERSEDED above
describe the pre-repair behavior and are not covered by this block.

| Behavior | Source | Layer | Test | State |
| -------- | ------ | ----- | ---- | ----- |
| M1: one transaction per order; each release runs in its own transaction; a final empty claim ends the run | `lifecycle.py` (`run_once`, `_cancel_next`) | application | `test_order_lifecycle.py::test_review_m1_the_job_runs_one_transaction_per_order` | CONFIRMED |
| M1: claims one order at a time (`claim_overdue(now, 1)`) | same | application | `::test_review_m1_the_job_claims_one_order_at_a_time` | CONFIRMED |
| M1: at most `OVERDUE_BATCH` (100) orders per run, the rest on the next run | same | application | `::test_review_m1_a_run_processes_at_most_100_orders` | CONFIRMED |
| L1: a refused release is logged and STOPS the run (later orders untouched, not tried) | `lifecycle.py` | application and integration | `::test_deviation_5_a_refused_release_skips_and_logs_that_order_and_stops_the_run`, `::test_deviation_5_when_every_release_is_refused_...`; `test_order_lifecycle_flow.py::test_deviation_5_the_job_skips_and_logs_...` (real SQL) | CONFIRMED |
| L1 (added now): a refused CANCEL also stops the run with a third order behind it, is logged, and the same order is re-claimed (and stops the run again) next time | `lifecycle.py` | application | `::test_review_l1_a_refused_cancel_stops_the_run_and_is_claimed_again_next_time` | CONFIRMED |
| M1 over real SQL: the overdue job and `ExpireReservations` over crossed stock sets do not deadlock and end with every order cancelled and stock back at `(20, 0)`. The overlap is now PROVEN, not hoped for: the test holds `FOR UPDATE` on every stock item, starts both jobs, waits (polling `pg_locks` for ungranted locks) until at least two sessions wait, and only then releases; it fails with "the jobs did not overlap" otherwise | `lifecycle.py`; inventory `ExpireReservations` | integration | `test_order_lifecycle_flow.py::test_review_m1_the_job_and_the_expiry_job_over_crossed_stock_do_not_deadlock` (+ `wait_until_sessions_wait_on_locks`) | CONFIRMED (run 3 times green; not run against pre-repair code, so a regression guard, not a demonstrated red) |
| L2: `get_by_number_for_update` and `claim_overdue` use `FOR NO KEY UPDATE`: a note (FK key-share) no longer waits for a move in flight | `sql_order_repository.py` | integration | `test_sql_order_lifecycle.py::test_a_note_lands_while_a_move_holds_the_order_row` | CONFIRMED |
| L2: `claim_overdue` still skips an order locked by another claim or a move | `sql_order_repository.py` | integration | `test_sql_order_lifecycle.py::test_a_second_claim_skips_...` | CONFIRMED (unchanged) |
| L4: `InMemoryOrders.save` stores a copy without pending events; a second command on the same order does not republish the first event | `orders/infrastructure/in_memory.py` | application | `test_order_lifecycle.py` refund test asserts the exact event list | CONFIRMED |
| `AddOrderNote` after L2 (docstring change only) | `lifecycle.py:162-185` | application | existing `test_adding_a_note_*` | CONFIRMED (unchanged) |

Tests this round: unit +1 (L1 cancel stop); integration 0 new, 1 strengthened (M1 overlap).

## Review findings

Reviewed 2026-10-19 on `feat/orders-lifecycle` (`main...HEAD`, worktree clean), reviewer
subagent (opus). Paths below are under `apps/api/src/fragancia_api/modules/` unless rooted.

### Pass 1 — checklist: 13/14 applicable passed, 1 failed (docs), 1 n/a

- [x] `plans-scope` clean: "31 declared, 48 changed … Every change is inside the plan". No hot
      file (`container.py`, `docs/modules.json`, `.importlinter`) touched.
- [x] `uv run just check` green (live run): ruff, mypy "no issues found in 427 source files",
      import-linter 7 kept / 0 broken, 3410 passed / 3 skipped, plans OK, harness-check OK.
- [x] `uv run just test-integration` green (live run): 981 passed, 5 skipped.
- [x] Business rules in `domain/` (transition table, per-method rules, text bounds); router only
      parses the number and maps errors; query adapter has none.
- [x] CQRS-lite: every command goes aggregate → repository inside `transactions.run` and returns
      `Result`; `ListOrders`/`GetOrder` go through the `OrderQueries` port and return contract
      models; the repository methods are not screen-specific.
- [x] Contracts in `orders/contracts.py`; `openapi.json` regenerated (check's drift test green).
- [x] Errors `ORDERS_ORDER_NOT_FOUND` / `ORDERS_TRANSITION_INVALID` /
      `ORDERS_ORDER_DATA_INVALID` / `INVENTORY_RESERVATION_NOT_COMMITTED`, stable SCREAMING_SNAKE.
- [x] Money in integer cents, times from `Clock`, ids from `new_id()`.
- [x] New migration 0021: no drops on upgrade, FKs within `orders`, reversible (round trip
      tested on a scratch database).
- [x] Routes on `admin_router` with router-level `orders:manage` and per-route `orders:finance`
      on payment, cancel-paid and refund.
- [x] Wiring in `orders/module.py` and `inventory/module.py` resolves (container test green).
- [x] No secrets or real personal data.
- [x] `## Deviations` honest: spot-checked 2 (commit refuses `RETURNED` with
      `ReservationNotActive`, release maps it to `ReservationCommitted`,
      `inventory/application/commands/reservations.py:204,293`) and 5 (`lifecycle.py:199-217`).
- [ ] **Docs: one stale row.** `docs/architecture.md:63` (inventory) still lists reservations as
      "(reserve, extend, commit, release, expiry job)" without the new `return` /
      `returned` status. See L3.
- n/a PR body: no PR exists yet for this branch (opened after verify).

### Pass 2 — findings

**M1 (medium, needs a code change) — the overdue job breaks inventory's lock order across
orders and can deadlock.** `orders/application/commands/lifecycle.py:194-222`.
`CancelOverdueOrders.run_once` runs the whole batch in one transaction and calls
`stocks.release` once per order. Each `ReleaseReservation` of an *active* reservation locks
its reservation row and then its own stock items in `presentation_id` order
(`inventory/application/commands/reservations.py:296-299`). Nothing sorts across calls, so after
order A the transaction holds A's items and then asks for B's reservation and items. That breaks
the invariant `reservations.py:1-20` relies on ("every operation takes stock items in one global
order … so no wait cycle forms").

Reservations are created with `expires_at == payment_due_at` (`orders/application/commands/checkout.py:267-275`).
So whenever the orders job runs before inventory's expire job in a given minute, the claimed
orders still have active reservations and the job takes item locks. Two concrete cycles:

- **Against `ExpireReservations`**
  (`inventory/infrastructure/sql_reservation_repository.py:87-97`, `expires_at <= now`,
  `SKIP LOCKED`):
  1. The orders job releases rA (active) and holds stock item P.
  2. The expire job has already claimed rB and asks for P, so it waits on the orders job.
  3. The orders job's `get_for_update(rB)` waits on the expire job, and the two deadlock.
- **Against any sorted multi-item operation** (checkout's `ReserveStock`, `MarkOrderPaid` →
  commit of another order, `RecordMovement`):
  1. The job holds P2, from A's release.
  2. The other transaction holds P1 and asks for P2.
  3. The job asks for P1, for B's release, and the two deadlock.

PostgreSQL aborts one side after `deadlock_timeout`. If that is the job, its batch rolls back
and the next run retries it. If it is a customer's checkout or an owner's "mark paid", they get
a 500. Data stays consistent. This was reasoned from the code and not reproduced: no
integration test runs the job concurrently with the expire job or a checkout
(`test_order_lifecycle_flow.py` only covers two job runs and two payments). A possible fix,
for the implementer to decide: one `transactions.run` per order (claim one, cancel, release,
save, commit, loop up to the batch). Each transaction then holds one reservation's items, the
same shape as the manual commands. The lock-order doc in `lifecycle.py:1-7` should then say so.

**L1 (low) — a permanently refused order can starve the job (deviation 5).**
`lifecycle.py:207-217` together with `sql_order_repository.py:120-134`. A skipped order stays
`pending_payment` and overdue. The claim is oldest-due first with `LIMIT 100`, so it is
re-claimed and logged every minute forever. With 100 or more such orders, no newer overdue
order is ever cancelled. Today this cannot happen: release refuses only a
committed/returned/missing reservation, and commit happens only atomically with `paid`. It
becomes possible once the payments initiative commits reservations from a webhook. Note it for
that initiative, or have the job surface the stuck orders. No change is needed for this plan.

**L2 (low) — "takes no lock on the order" is inaccurate (tester observation 1, confirmed).**
The claim appears in `orders/domain/repositories.py:50`, `lifecycle.py:163-164` and
`orders/http/router.py:377-378`. Inserting into `order_notes` makes PostgreSQL take `FOR KEY
SHARE` on the parent row through the FK, and that conflicts with the plain `FOR UPDATE` used by
`get_by_number_for_update` and `claim_overdue` (`sql_order_repository.py:116,133`). A note
therefore waits for an in-flight move or for a whole overdue batch (up to 100 orders, see M1).
It cannot deadlock. Two ways out: correct the docstrings, or lock with
`with_for_update(key_share=True)` (`FOR NO KEY UPDATE`). The second is enough because
`order.id` never changes, and it makes notes genuinely non-blocking.

**L3 (low) — stale inventory row in `docs/architecture.md:63`.** The row does not mention
`return_sold` / the `returned` reservation status. The orders row (same file) does mention it.

**L4 (low, test fake only) — `InMemoryOrders.save` keeps pending domain events on the stored
copy (tester observation 2, confirmed).** At `orders/infrastructure/in_memory.py:103-111`, `stored
= deepcopy(order)` is taken before the command calls `pull_events()`. The next
`get_by_number_for_update` returns those events again, and a second command re-publishes them.
The SQL adapter does not do this, so product behavior is fine. The fake can hide a real
double-publish bug in tests that chain commands, which today assert only on the last event.
Clearing the stored copy's events (as it already does for `new_history`) would align the fake
with SQL.

Checked and found correct (no finding):

- **Lock order in the manual commands.** They take the order row, then
  reservation → settings (share) → items, one reservation per transaction.
- **Job vs. a concurrent manual move.** The job uses `SKIP LOCKED`. A manual command blocked on
  a claimed row re-reads it after the job commits (READ COMMITTED; Core rows have no identity
  map) and gets `ORDERS_TRANSITION_INVALID`.
- **Two payments at once** sell once (tested).
- **Late commit after expiry.** It re-checks `available` and refuses with `StockUnavailable`,
  which is a 409. The nested runner only returns `Err`, and the outer `run` rolls back the
  order (`shared/infrastructure/database.py:64-77`).
- **No partial writes on refusal.** `ReturnReservation` writes nothing before every line's
  movement succeeds, and RETURN quantities stay within `QUANTITY_MAX`. Release and commit
  refuse before any write.
- **The state machine** cannot reach a forbidden state: `cancel_*` and `mark_refunded` check
  status explicitly, and a refund can be marked once.
- **Authorization.** Finance routes need both permissions. The actor id is a `str`
  (`shared/application/actor.py`).

Result: 1 medium and 4 low findings. M1 needs a code change, so the status stays `review`
(repair handoff to the implementer). L2–L4 can ride along with that repair; L1 is a note for
the payments initiative.

### Re-review 2026-10-19 after the step 7 repair (commits 6ce81ea, 0624b37, 922256a)

Reviewer subagent (opus), `feat/orders-lifecycle`, worktree clean. This supersedes the result
line above for the current code; the first review is kept as history.

**Pass 1 — checklist: 14/14 applicable passed, 1 n/a (PR body: no PR yet).**

- [x] `plans-scope`: "31 declared, 48 changed … Every change is inside the plan"; no hot file.
- [x] `uv run just check` green (live): 3413 passed / 3 skipped; harness 685 passed; lint,
      mypy, import-linter, plans and adapter drift ran clean before the tests.
- [x] `uv run just test-integration` green (live): 982 passed, 5 skipped.
- [x] Domain, CQRS-lite, contracts (`openapi.json` regenerated by the repair; drift test
      green), error codes, Money/Clock/ids, migration, routes, wiring, secrets: unchanged by
      the repair and still as checked in the first review.
- [x] Docs: L3 fixed (`docs/architecture.md` inventory row). One remaining stale sentence in
      the initiative README is N2 below. It is a doc nit, not a checklist failure.
- [x] Deviations/evidence honest: the repair log and the SUPERSEDED rows in Test coverage
      describe the behavior change of deviation 5. Spot-checked against `lifecycle.py:209-250`.

**Prior findings: all resolved.** Each has a test that fails without its fix.

- **M1 resolved.** `run_once` (`lifecycle.py:209-219`) loops `range(OVERDUE_BATCH)`, one
  `transactions.run(self._cancel_next)` each. `_cancel_next` (`:221-250`) does
  `claim_overdue(now, 1)`, cancel, release, save, publish. The publish happens inside the
  same transaction, so the outbox stays atomic per order. Termination: an empty claim, a
  refusal (`Ok(False)`) or 100 iterations; `Err` is unreachable but also breaks. An
  unexpected exception propagates after the earlier orders have committed, so arq retries
  only the rest. Lock cycle re-traced: a job transaction holds one order row and then one
  reservation and its items in id order, the same shape as the manual commands.
  `ExpireReservations` skips locked reservations and takes its items globally sorted, so no
  cycle remains.
  **Demonstrated red against the pre-repair job.** A scratch pytest plugin (outside the repo:
  the scratchpad `oldjob/old_overdue_job.py`, a verbatim copy of `lifecycle.py` at af64c3e
  whose `pytest_configure` swaps the old `CancelOverdueOrders.run_once` in memory) ran
  `test_review_m1_the_job_and_the_expiry_job_over_crossed_stock_do_not_deadlock`. Result:
  **5/5 runs failed with `psycopg.errors.DeadlockDetected: deadlock detected`**. Against the
  current code it passed 4/4 runs, plus the full integration run. No product or test file was
  modified for this.
  The unit tests also fail on the old code: it called `claimed == [100]`, not `[1, 1, 1]`,
  and ran one transaction, not 4.
- **L1 addressed** by stopping the run at a refused order; a hot loop is no longer possible.
  Tests: `test_review_l1_…`, the deviation 5 tests and the flow test. The old code tried every
  order, so they fail on it. The trade-off is N1.
- **L2 resolved.** `sql_order_repository.py:117,135` use `with_for_update(key_share=True)`.
  `test_a_note_lands_while_a_move_holds_the_order_row` commits a note under a held move lock
  within 3 s. Under the old `FOR UPDATE` the note waited, which is what the previous version
  of this test asserted, so this one would time out.
  - **No path needs a stronger lock.** `save` (`:141-152`) writes only `status`,
    `cancel_reason`, `refund_status`, `shipping_carrier`, `tracking_number` and `updated_at`.
    The only unique keys are `id`, `number` and `access_token_digest`, which no code updates.
    Nothing deletes an order row, and no other code locks `orders.orders`. So every UPDATE is
    already a no-key update: the held lock is never upgraded, and `FOR KEY SHARE` from the
    FKs (`order_lines`, `order_events`, `order_notes`) never conflicts.
  - **Moves still serialize.** `FOR NO KEY UPDATE` conflicts with itself, and `SKIP LOCKED`
    still skips rows held by a move or claim (`test_a_second_claim_skips_…` green).
- **L3 resolved** (`docs/architecture.md`).
- **L4 resolved** for `save` (`in_memory.py:108`). The refund test now asserts the exact
  event list `[("cancelled", "cancelled")]`; without the fix the cancel event would be
  published again and the list would have two entries. The same pattern remains in `add`
  (N3).

**Pass 2 — new findings (none blocking).**

**N1 (low, design note; no change for this plan).** `lifecycle.py:227-243`: stopping at a
refused order makes the starvation in L1 worse. Before the repair it took 100 permanently
refused orders to block the job. Now a single one blocks it: it is the oldest due, so it is
re-claimed first and stops every run, and no newer overdue order is ever cancelled.
- **Unreachable today.** Release refuses only `committed`, `returned` or a missing
  reservation (`inventory/application/commands/reservations.py`, `ReleaseReservation`).
  Commit happens only atomically with `paid`, and nothing deletes reservations.
- **Documented.** The class docstring and the repair log record it.
- **For the payments initiative.** It must exclude refused ids within a run, or move such
  orders out of `pending_payment`, before webhooks can commit a reservation.

**N2 (low, docs).** `plans/orders-checkout/README.md:42-43` still says the job "skips an
order whose release is refused, but re-claims it every run". After the repair that order
halts every run of the job, as N1 explains. The repair log claimed "no edit needed". The
dependency note should state the stronger consequence so the payments initiative sizes it
right. It is a doc edit only.

**N3 (low, test fake, latent).** `orders/infrastructure/in_memory.py:83-84`: `InMemoryOrders.add`
deep-copies the order before checkout pulls its events (`checkout.py:296-298`). The stored copy
therefore keeps `orders.order.placed`, and a later command run through the fake would publish
it again. This is the same defect as L4, in `add`. It is not observable today: no unit test
chains checkout with a lifecycle command, and the lifecycle tests' `make_order` pulls the
events before `add`. A one-line `pull_events()` on the copy would close it. It is optional
and not required for this plan.

**N4 (nit, test hygiene).** `test_order_lifecycle_flow.py:708`: if
`wait_until_sessions_wait_on_locks` fails, the `jobs` gather is left un-awaited when the
holder connection closes. This affects only a failing run and is cosmetic.

**Skip-count note.** The tester saw 6 skips in one run and 5 in another. This run had 5. It
was not investigated and nothing points at this plan.

**Result (re-review).** No finding needs a product-code change for this plan, so the status
moves to `verify`. What remains is docs and evidence: N2 (initiative README wording), and
optionally N3 (fake-only) and N4 (test hygiene), which the main session or the tester can
take with the verify phase. The verifier must exercise the repaired job in the running app,
because the earlier verification does not cover the new code.

## Repair log

Step 7 implemented 2026-10-19 (status `implementing`). Prior review evidence is kept above;
the repaired code repeats testing, review and verify.

- **M1.** `CancelOverdueOrders.run_once` now runs one `transactions.run` per order
  (`_cancel_next`: `claim_overdue(now, 1)`, cancel, release, save, publish), up to
  `OVERDUE_BATCH` (100) orders per run. The docstring states the lock-order reason.
- **L1.** The run stops at the first order whose cancel or release is refused (logged), so it
  is not re-claimed in a hot loop. The docstring records the starvation risk; the initiative
  README's "Dependency notes" under Payment already carries the line (no edit needed).
  Existing deviation 5 behavior changed accordingly: the rest of the batch is no longer
  cancelled in the same run (deviation 5 text above describes the pre-repair behavior).
- **L2.** `get_by_number_for_update` and `claim_overdue` lock with
  `with_for_update(key_share=True)` (`FOR NO KEY UPDATE`). Docstrings of the port, the
  `AddOrderNote` command and the route updated; `openapi.json` regenerated (route description).
- **L3.** `docs/architecture.md` inventory row mentions `return_sold`, `returned` and
  `INVENTORY_RESERVATION_NOT_COMMITTED`.
- **L4.** `InMemoryOrders.save` stores a copy without pending events.
- **Tests.**
  - Unit (`tests/unit/orders/test_order_lifecycle.py`): replaced `test_the_job_claims_a_batch_of_100`
    with `test_review_m1_the_job_claims_one_order_at_a_time`,
    `test_review_m1_the_job_runs_one_transaction_per_order` and
    `test_review_m1_a_run_processes_at_most_100_orders`; the deviation 5 tests now expect the
    run to stop at the refused order; the refund test asserts the exact event list (L4
    workaround removed).
  - Integration: `test_a_note_waits_for_a_move_in_flight_...` became
    `test_a_note_lands_while_a_move_holds_the_order_row`;
    `test_review_m1_the_job_and_the_expiry_job_over_crossed_stock_do_not_deadlock` runs both
    jobs concurrently over crossed stock; the deviation 5 flow test expects the stop.
  - Not proven: the deadlock test was not run against the pre-repair code (no way to restore
    it without reverting files), so it is a regression guard, not a demonstrated red.
    (Superseded: the re-review showed it red, 5 of 5 `DeadlockDetected` against the old job.)
- 2026-10-19, re-review N2–N4, main session:
  - **N2:** the README's "Dependency notes" now say that a refused order halts every run, and
    what the payments initiative must do about it (it also records N1).
  - **N3:** `InMemoryOrders.add` stores its copy without the pending events, like `save` (a fake
    only).
  - **N4:** the deadlock test cancels the jobs it gathered when the overlap wait fails.
  - `uv run just check` green (3413 passed); `test_order_lifecycle_flow.py` green (24 passed).

## Verification

2026-10-19, main session, live against the development database. The API ran on :8100 and the
arq worker was running, with migration 0021 applied; this is the code after the re-review fixes.
- **Script:** `scratchpad/qa_inv/qa_lifecycle.py`.
- **Accounts:** owner `verifier007`, and staff `qa004-staff-…`.
- **Delivery options:** set for the run and restored afterwards (home 18000, free from 250000;
  local 9000 for 01000; pickup on).
- **Products:** the QA perfume from plan 002.
- **Orders:** `LFI-1005` to `LFI-1010` stay in the development database. Two of them really
  sold one unit each (the 50 ml for `LFI-1005` and the 100 ml for `LFI-1006`, both delivered),
  so the 50 ml is 5 on hand and the 100 ml is 0. Every other unit was released or returned.

- [x] **Overdue (also covers the orders of plan 003).**
  - When the worker started, `LFI-1001`–`1004`, overdue since plan 003, were all `cancelled`
    with reason "No pagado a tiempo". Each has one history row pending_payment → cancelled by
    actor `system`.
  - `LFI-1010`, with `payment_due_at` set 1 minute in the past, was cancelled by the worker
    within one cycle, with the same reason and actor `system`. Its reservation is `released`,
    and the 50 ml went from 5/1 to 5/0.
- [x] **List and detail.**
  - Staff `GET /admin/orders?status=pending_payment` listed the five new orders, newest first
    (`LFI-1009` … `LFI-1005`).
  - `q=LFI-1005` and `q=1005` found just that order; `q=qa004@exa` and `q=luis pé` found all
    five.
  - The detail showed the address (state `CMX`), the line and an empty history.
  - Anonymous got 401; `/admin/orders/abc` got 404.
- [x] **Paid by hand.**
  - Staff `payment` got 403 `FORBIDDEN`.
  - Owner with "Transferencia directa" got 204: the status is `paid`, the reservation
    `committed`, and there is one `sale` movement "Venta: order:<id>". The 50 ml went from 6/4
    to 5/3.
  - The outbox has `orders.order.status_changed` pending_payment → paid.
- [x] **Forward moves.**
  - Home order:
    - shipped without a carrier gave 422 `ORDERS_ORDER_DATA_INVALID` `{field: carrier}`;
    - with "Estafeta" / "123456" it gave 204, then delivered gave 204;
    - delivered → preparing and cancelling the delivered order gave 409
      `ORDERS_TRANSITION_INVALID`.
    - The history has three rows with actor, note, carrier and tracking number.
  - Pickup order:
    - paid ("Efectivo en tienda");
    - shipped gave 409 `{from: paid, to: shipped}`;
    - preparing → ready_for_pickup → delivered each gave 204, with four history rows.
- [x] **Cancel unpaid.** Staff, reason "El cliente se arrepintió": 204. The order is `cancelled`
  with that reason, and the reservation is `released` (50 ml 5/3 → 5/2).
- [x] **Cancel paid.**
  - After the payment (50 ml 5/2 → 3/0), `cancel` gave 409, and staff `cancel-paid` gave 403.
  - Owner `cancel-paid` gave 204: `cancelled`, `refund_status: pending`, the reservation
    `returned`, and the movements are `sale 2` and then `return 2` "Devolución: order:<id>"
    (back to 5/0).
  - Staff `refund` gave 403. Owner `refund` gave 204 with `done`, and a second call gave 409.
  - The outbox has pending → paid, paid → cancelled, and cancelled → cancelled for the refund.
- [x] **Notes.** Staff and owner notes gave 201 each, and the detail lists them newest first
  with their author ids.
- [x] **Made to order only.** `LFI-1009` (`reservation_id` null) was marked paid (204) and then
  cancelled as paid (204, `refund_status: pending`), with no inventory call.

NOT VERIFIED live:
- the deadlock race between the two jobs (the integration test shows it red against the old job
  and green now);
- the starvation of re-review N1, which cannot occur before the payments initiative.
