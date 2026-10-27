---
status: review
module: payments
min_implementer: high
depends_on: ["002"]
---

# 003 — OXXO and SPEI

## Context

Checkout Pro offers cards only: the preference excludes `ticket`, `atm` and `bank_transfer`
(`apps/api/src/fragancia_api/modules/payments/infrastructure/mercadopago_gateway.py:23`, used at
`:170`). This plan enables cash in stores and SPEI transfers (decision 14). A cash ticket or a
transfer is a Mercado Pago payment in `pending` until the customer pays it. While it is pending,
the order's payment window and its reservation are extended to the ticket's expiry, up to 48
hours (decision 5). The customer gets our own email with the instructions (decision 15), and the
order page can show the pending ticket (decision 16). It also folds in the open finding
`payments-webhook-setup-docs-incomplete`.

**What exists today:**
- **Gateway.**
  - `_body` builds the preference (`mercadopago_gateway.py:150-176`): `payment_methods` with
    the excluded types and `installments: 1`, and `expiration_date_to` = the order's
    `payment_due_at` with millisecond timestamps.
  - `get_payment` maps the payment (`mercadopago_gateway.py:69-130`). It reads
    `transaction_amount`, `date_approved` and `payment_type_id`, but not
    `date_of_expiration`, `payment_method_id` or the receipt URL.
- **Processing.** `ProcessPaymentNotification.__call__` (`payments/application/handlers/process_payment.py:54-87`)
  claims and refreshes the payment row and applies only `approved` payments. A `pending` one is
  just stored. The `Payment` aggregate is at `payments/domain/payment.py:25-60`.
- **Orders facade.** `OrderPaymentConfirmations(OrderMove)`
  (`orders/application/commands/confirm_payment.py:36-46`) is the way payments moves an order.
  `StockHolds` has `commit`, `release` and `return_sold` but no `extend`
  (`orders/application/ports.py:56-77`; adapter at `orders/infrastructure/stock_holds.py:19-40`).
- **Inventory already extends a reservation.** `StockReservations.extend(reservation_id, expires_at)`
  (`inventory/application/stock_reservations.py:70-72`) works only for an active reservation
  that has not expired, and only to a later expiry
  (`inventory/domain/reservation.py:115-121`). Stock does not change, so no event is published
  (`inventory/application/commands/reservations.py:137-139`).
- **The order's deadline.** `payment_due_at` is set at checkout (`orders/domain/order.py:264-290`).
  Nothing moves it later. The overdue job cancels pending orders past it
  (`orders/application/commands/lifecycle.py:189-208`). The customer page already returns
  `payment_due_at` (`orders/contracts.py:319`).
- **Customer emails.** They are outbox subscribers in orders, one per event
  (`orders/application/handlers/order_emails.py:206-232`, `SendOrderPlacedEmail`; wiring at
  `orders/module.py:202-228`), in Cancún time, with the `{STOREFRONT_URL}/pedido#token=` link.
- **Mercado Pago**, checked on 2026-10-27 against the official SDK types
  (`mercadopago/sdk-nodejs`):
  - The preference (`src/clients/preference/commonTypes.ts`) has `date_of_expiration`
    (ISO 8601). Mercado Pago documents it as the expiry of cash payments made from the
    preference. It also has `excluded_payment_types` and `excluded_payment_methods`.
  - The payment (`src/clients/payment/commonTypes.ts`) has `payment_type_id` (`ticket`,
    `bank_transfer`, `atm`, `credit_card`…), `payment_method_id`, `date_of_expiration` and
    `transaction_details.external_resource_url` (the voucher or instructions page).
  - A cash or transfer payment stays `pending` until it is paid, then becomes `approved`, which
    plan 002 already applies. An unpaid one ends `cancelled` or `expired`.

**Approach.**
- **Where the ticket lives.** Two options were compared:
  - Store the ticket in orders: orders' page reads it directly, but orders would hold
    payment-provider data.
  - Keep the ticket in payments (chosen) and give the storefront a payments query that takes the
    order's token, like `POST /payments/checkout`. Orders stays unaware of payments (initiative
    README, "Dependency notes").
- **Orders still owns the deadline.** A new facade method moves `payment_due_at` and extends the
  reservation in one transaction. It records an event that orders' own email handler turns into
  the instructions email, because the customer's data and the private link live in orders.
- **The 48 hours.** Preferences get `date_of_expiration` = link creation + 48 h. A ticket can only
  be issued while the link is valid (30 minutes or the current window), so it expires at most
  48 h after it is issued, and never before 47.5 h for a fresh order. We extend to the payment's
  own `date_of_expiration` as Mercado Pago reports it.
- **Imitated files:** the facade method follows `confirm_payment.py:36-100`; the email handler
  follows `SendOrderPlacedEmail` (`order_emails.py:206-232`); the payments query endpoint
  follows `payments/http/router.py` `start_checkout`; the migration follows
  `apps/api/migrations/versions/0024_payments_payments.py`.

## Out of scope

- Refunds (plan 004). A ticket paid after the order was paid by card is already `refund_due`
  (plan 002).
- Payment at bank counters or ATMs (`atm` stays excluded, decision 14).
- Interest-free installments (decision 4).
- Reminders before a ticket expires.
- The storefront pages that render the ticket (phase 3).
- The open finding `payments-webhook-request-id-injected-by-platform`.

## Dependencies

- `002` (done): `ProcessPaymentNotification`, `Payment`, `payments.payments`,
  `OrderPaymentConfirmations` and the gateway's `get_payment`.

## Steps

1. **Docs: the webhook setup (finding `payments-webhook-setup-docs-incomplete`)**
   - Files: `apps/api/.env.example` (modify), `apps/api/README.md` (modify), `apps/api/src/fragancia_api/config.py` (modify), `plans/findings/payments-webhook-setup-docs-incomplete.md` (modify)
   - Do:
     - Rewrite the Mercado Pago comments in `.env.example` and in `config.py:65-66` (comments
       only, no setting changes):
       - credentials come from the test seller account (they start with `APP_USR-`);
       - the panel's Webhooks page has a "Modo de prueba" tab and a "Modo productivo" tab, each
         with its own URL and secret;
       - set the URL in both tabs and mark the event "Pagos (legacy)";
       - `MERCADOPAGO_WEBHOOK_SECRET` must be the secret of the tab that matches the credentials
         (the test tab for a test seller account);
       - "Simular notificación" signs with the secret of the tab it is sent from.
     - Same text in the README's payments section.
     - Set the finding to `status: planned` with `plan: payments-mercadopago/003`.
   - Observable result: docs only; `uv run just check` green.

2. **Gateway: enable cash and SPEI, read the ticket**
   - Files: `apps/api/src/fragancia_api/modules/payments/application/ports.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/mercadopago_gateway.py` (modify), `apps/api/src/fragancia_api/modules/payments/application/commands/start_checkout.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/in_memory.py` (modify)
   - Do:
     - `_EXCLUDED_PAYMENT_TYPES = ("atm",)` (decision 14). Update the comment.
     - Add `cash_expires_at: datetime` to `CheckoutRequest`. `StartCheckout` sets it to
       `now + CASH_TICKET_TTL`, with `CASH_TICKET_TTL = timedelta(hours=48)` (decision 5).
       `_body` sends `"date_of_expiration": request.cash_expires_at.isoformat(timespec="milliseconds")`.
     - `GatewayPayment` gains `payment_method: str | None`, `expires_at: datetime | None`
       (`date_of_expiration`) and `ticket_url: str | None`
       (`transaction_details.external_resource_url`). `get_payment` reads them as optional fields
       (a wrong type → `None`, like `date_approved`).
   - Observable result: the preference body excludes only `atm` and carries `date_of_expiration`
     48 h after `now`; `get_payment` maps the three new fields.

3. **Orders: extend the payment window, and the instructions email**
   - Files: `apps/api/src/fragancia_api/modules/orders/domain/order.py` (modify), `apps/api/src/fragancia_api/modules/orders/application/ports.py` (modify), `apps/api/src/fragancia_api/modules/orders/infrastructure/stock_holds.py` (modify), `apps/api/src/fragancia_api/modules/orders/infrastructure/in_memory.py` (modify), `apps/api/src/fragancia_api/modules/orders/application/commands/confirm_payment.py` (modify), `apps/api/src/fragancia_api/modules/orders/application/handlers/order_emails.py` (modify), `apps/api/src/fragancia_api/modules/orders/module.py` (modify), `apps/api/src/fragancia_api/modules/orders/__init__.py` (modify)
   - Do:
     - **Domain.** `Order.await_payment(until: datetime, method: str, instructions_url: str | None, now: datetime) -> Result[bool, DomainError]`:
       - only in `pending_payment`, otherwise the usual refusal;
       - `payment_due_at = until` when `until` is later; returns whether it moved;
       - always records `OrderPaymentPending` (`orders.order.payment_pending`: `order_id`,
         `number`, `due_at` (the new or unchanged deadline), `method` and `instructions_url`).
       No history row: the status does not change.
     - **Stock.** `StockHolds.extend(reservation_id, expires_at)` over inventory's
       `StockReservations.extend`, in the adapter and the in-memory fake.
     - **Facade.** `OrderPaymentConfirmations.hold_for_payment(order_id, until, method, instructions_url) -> Result[PaymentConfirmation, DomainError]`,
       in one `transactions.run`:
       1. `get_by_id_for_update`; None → `not_found`.
       2. Not pending → `not_pending` with the status.
       3. `await_payment`. If the deadline moved and there is a reservation, `extend` it. A
          refusal of the extension (an expired or released reservation) is logged as
          `orders.payment_hold_not_extended` and does not undo the deadline. The ticket can still
          be paid; its later commit is a late commit (plan 002).
       4. Save and publish → `PaymentConfirmationOutcome.HELD` (a new value).
     - **Email.** `SendPaymentInstructionsEmail` subscribes to `orders.order.payment_pending` and
       sends the customer a plain-text email, like `SendOrderPlacedEmail`:
       - subject `Tu ficha de pago del pedido {number}`;
       - body: `Para completar tu pedido {number}, paga {money} con {method} antes del {due_at, Cancún, "%d/%m/%Y a las %H:%M"}.\n` then, when there is a URL,
         `Consulta la ficha con las instrucciones aquí:\n{instructions_url}\n`, then the usual footer;
       - `method` is the label the facade received.
     - Wire the subscriber in `module.py`, export `HELD`, and describe `hold_for_payment` and the
       event in `orders/__init__.py`.
   - Observable result: unit tests over the in-memory adapters cover the facade (moved, not
     moved, not pending, not found, extension refused); the email copy is tested.

4. **Payments: store the ticket and hold the order**
   - Files: `apps/api/src/fragancia_api/modules/payments/domain/payment.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/tables.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/sql_payment_repository.py` (modify), `apps/api/migrations/versions/0025_payments_tickets.py` (create), `apps/api/src/fragancia_api/modules/payments/application/handlers/process_payment.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/order_payments.py` (modify)
   - Do:
     - **Domain.**
       - `Payment` gains `payment_method`, `expires_at`, `ticket_url` and `held_at` (when the
         order was held for it).
       - `CASH_TYPES = ("ticket", "bank_transfer")`, `PENDING = "pending"`.
       - `needs_hold()`: status pending, type in `CASH_TYPES`, `expires_at` set, `held_at` None.
       - `mark_held(now)`.
       - `METHOD_LABELS = {"ticket": "efectivo en tienda", "bank_transfer": "transferencia SPEI"}`.
     - **Migration `0025`.** Add the four nullable columns to `payments.payments`; the
       downgrade drops them. Update `tables.py` and the repository (read and write).
     - **Handler.** In `work()`, after the approved branch: if `payment.needs_hold()`, call the
       new `OrderConfirmations.hold(order_id, until=payment.expires_at, method=METHOD_LABELS[type], instructions_url=payment.ticket_url)`.
       - `held`, `not_pending` or `not_found` → `mark_held` (a ticket for a cancelled or paid
         order needs nothing more, and is never retried).
       - `Err` → return it (the relay retries).
       - The adapter maps the facade's outcomes like `confirm`.
     - **Re-delivery.** `held_at` makes the hold, and so the email, happen once per payment.
   - Observable result: a pending OXXO payment extends the order once; re-delivery sends no
     second email; an approved one after it is applied as in plan 002.

5. **Payments: the pending-ticket query for the order page (decision 16)**
   - Files: `apps/api/src/fragancia_api/modules/payments/contracts.py` (modify), `apps/api/src/fragancia_api/modules/payments/application/queries/__init__.py` (create), `apps/api/src/fragancia_api/modules/payments/application/queries/pending_tickets.py` (create), `apps/api/src/fragancia_api/modules/payments/domain/repositories.py` (modify), `apps/api/src/fragancia_api/modules/payments/http/router.py` (modify), `apps/api/src/fragancia_api/modules/payments/module.py` (modify), `apps/api/src/fragancia_api/modules/payments/__init__.py` (modify), `apps/api/openapi.json` (modify)
   - Do:
     - `POST /api/v1/payments/pending`, body `{access_token}` (as `/payments/checkout`, decision
       42 of orders-checkout).
       - Answers `PendingTickets {items: [{method, amount_cents, expires_at, ticket_url}]}`: the
         order's payments with status `pending`, a cash type, `expires_at > now`, newest first.
       - Unknown token → 404 `PAYMENTS_ORDER_NOT_FOUND`. A non-pending order → `items: []`.
     - The read goes through a `PaymentQueries` port (`pending_for_order(order_id, now)`) with
       its SQL adapter in `sql_payment_repository.py`, and `OrderPayments.payable` resolves the
       order.
     - `uv run just openapi`.
   - Observable result: the route is in `openapi.json`; it lists a live OXXO ticket and hides an
     expired one.

6. **Close the Mercado Pago link when the order stops waiting (decision 18)**
   - Files: `apps/api/src/fragancia_api/modules/payments/application/ports.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/mercadopago_gateway.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/in_memory.py` (modify), `apps/api/src/fragancia_api/modules/payments/application/handlers/close_checkout.py` (create), `apps/api/src/fragancia_api/modules/payments/domain/repositories.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/sql_checkout_repository.py` (modify), `apps/api/src/fragancia_api/modules/payments/module.py` (modify)
   - Do:
     - **Gateway.** `PaymentGateway.close_checkout(preference_id, at: datetime) -> Result[bool, PaymentGatewayUnavailable]`:
       - sends `PUT {api_url}/checkout/preferences/{preference_id}` (the official SDK's
         `src/clients/preference/update/index.ts`, checked 2026-10-27) with
         `{"expires": true, "expiration_date_to": at, "date_of_expiration": at}`, millisecond
         ISO timestamps;
       - same client settings and bearer header as `create_checkout`;
       - 2xx → `Ok(True)`; 404 → `Ok(False)`; anything else → `Err`, logged as
         `payments.gateway_error`.
     - **Repository.** `CheckoutRepository.close(order_id, at)`: sets the stored checkout's
       `expires_at = at`, so `StartCheckout` never reuses it.
     - **Subscriber.** `CloseCheckout` subscribes to `orders.order.status_changed`. It acts only
       when `from_status` is `pending_payment` and `to_status` is `paid` or `cancelled`, then:
       1. reads the order's stored checkout (`get_for_order`); none → return;
       2. `close_checkout(preference_id, now)`; `Err` → raise, so the relay retries;
          `Ok(False)` → log `payments.checkout_missing`;
       3. `close(order_id, now)` inside `transactions.run`.
     - Wire it in `module.py`.
     - **Limit.** A guest already in Mercado Pago's checkout when the order is paid or cancelled
       can still finish a payment. That payment is handled as in plan 002 and step 4: recorded,
       with no hold.
   - Observable result: after a card payment or a cancel, the old link no longer accepts
     payments, and the stored checkout is closed.

7. **Wiring and docs**
   - Files: `apps/api/README.md` (modify), `docs/architecture.md` (modify), `plans/payments-mercadopago/README.md` (modify)
   - Files: `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_order_repository.py` (modify), `apps/api/src/fragancia_api/modules/payments/domain/events.py` (modify) (deviations 1 and 8, accepted by the main session), `apps/api/tests/unit/payments/` (modify), `apps/api/tests/integration/payments/` (modify), `apps/api/tests/unit/orders/` (modify), `apps/api/tests/integration/orders/` (modify) (tests adjusted at implementation, deviation 4, and the layered tests of the testing phase)
   - Do: describe cash/SPEI, the hold, the new endpoint and `orders.order.payment_pending` in the
     README and architecture docs. In the initiative README, record plan 003's row as unchanged.
   - Observable result: `uv run just check` green; `plans-scope` clean.

## Acceptance criteria

- [ ] A new link's preference excludes only `atm` and carries `date_of_expiration` 48 h ahead;
      the sandbox checkout offers OXXO (or other stores) and SPEI.
- [ ] Choosing OXXO in the sandbox leaves the payment `pending`. Within a minute of the
      notification:
  - the order's `payment_due_at` equals the ticket's `date_of_expiration`;
  - the reservation's `expires_at` equals it too;
  - `payments.payments` has the method, expiry, URL and `held_at`;
  - Mailpit has "Tu ficha de pago del pedido LFI-…" with the amount, the Cancún deadline and the
    ticket link.
- [ ] Re-delivering that notification changes nothing and sends no second email.
- [ ] `POST /api/v1/payments/pending` with the order's token lists the ticket; with an unknown
      token, 404 `PAYMENTS_ORDER_NOT_FOUND`.
- [ ] The same with SPEI (`bank_transfer`).
- [ ] With a ticket pending, the guest can still get a card link and pay; the order becomes
      `paid`, and the ticket paid later would be `refund_due` (plan 002).
- [ ] Paying the ticket (approved) marks the order paid with the reservation committed.
- [ ] A second ticket that expires later moves the deadline again; one that expires earlier does
      not shorten it.
- [ ] An unpaid ticket: at the extended deadline the overdue job cancels the order (existing
      email).
- [ ] After an order is paid or cancelled, opening its old link in the sandbox no longer lets
      the guest pay or issue a ticket; `payments.checkouts.expires_at` equals the close time.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `Order.await_payment` (later, earlier, not pending, event always); `Payment.needs_hold` / `mark_held` |
| application | yes     | handler: hold once, outcomes, re-delivery, approved after hold; facade outcomes incl. extension refused; instructions email copy; pending query filtering; `CloseCheckout` (which moves act, no checkout, 404, raise on gateway error) |
| http        | yes     | `/payments/pending` 200/404, token only in the body; preference body (`atm` only, `date_of_expiration`) |
| integration | yes     | migration 0025 round trip; repository new columns and `pending_for_order`; facade + inventory `extend` in one transaction |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

Implementer, 2026-10-27. All seven steps done. `plans-scope` lists the files below as out of
scope; each is recorded here.

1. **`orders/infrastructure/sql_order_repository.py` (not in the file list): `save` now writes
   `payment_due_at`.**
   - Plan: step 3 moves `payment_due_at` in the domain and saves the order.
   - Reality: `SqlOrderRepository.save` (`sql_order_repository.py:151-164`) updated only the
     status, cancel reason, refund status, carrier, tracking number and `updated_at`. Without
     the column, a moved deadline would never reach the database.
   - Done: one line, `payment_due_at=order.payment_due_at`, in the `UPDATE`. Treated as a
     file-list omission, not a design change. The reviewer should confirm.
2. **`PaymentQueries` lives in `payments/application/ports.py`, not in `domain/repositories.py`.**
   - Plan: step 5 put the port in `domain/repositories.py`.
   - Convention: `XxxQueries` ports return response models and live in
     `application/ports.py`, as catalog does (`catalog/application/ports.py:83`). The domain
     must not import `contracts`.
   - Done: `PaymentQueries.pending_for_order(order_id, now) -> list[PendingTicket]` in
     `application/ports.py` (already in the plan's file list). `domain/repositories.py` still
     gets `CheckoutRepository.close` (step 6). The SQL adapter is `SqlPaymentQueries`, in
     `sql_payment_repository.py` as planned. `InMemoryPayments` implements it too.
3. **`PendingTicket.method` is the Spanish label** (`METHOD_LABELS`: "efectivo en tienda" /
   "transferencia SPEI"). The plan did not say whether the contract's `method` is the label or
   Mercado Pago's id. The label matches what the facade and the email get. The stored
   `payment_method` (e.g. `oxxo`) is not exposed.
4. **Existing tests changed so `check` stays green**. Each change follows from behavior the plan
   changes; none adds coverage (that is the tester's job):
   - `tests/unit/payments/support.py`: `make_request` sets the new required `cash_expires_at`.
   - `tests/unit/payments/test_mercadopago_gateway.py`: the expected body excludes only `atm`
     and has `date_of_expiration`. `test_only_cards_in_one_installment` is renamed
     `test_only_atm_is_excluded_in_one_installment`.
   - `tests/unit/payments/test_process_payment.py`: `FakeConfirmations.hold` exists to satisfy
     the port, and raises if it is called (these plan 002 tests never reach it).
   - `tests/unit/orders/test_confirm_payment.py`: the outcome list includes `held`.
   - `tests/unit/orders/test_order_emails_wiring.py`: `orders.order.status_changed` now also has
     payments' `CloseCheckout`.
   - `tests/integration/payments/test_sql_payments.py`: the row has the four new columns
     (null).
5. **Implementation choices the plan left open (not deviations, listed for review):**
   - The new fields on `GatewayPayment` and `Payment` default to `None`, so existing builders
     keep working.
   - `Payment.describe(payment_method, expires_at, ticket_url)` takes the ticket fields on
     every delivery. `refresh` keeps its signature.
   - `Payment.is_live_ticket(now)` backs the in-memory query.
   - The gateway reads `date_of_expiration` as `None` when it is malformed or has no offset.
   - `Order.await_payment` on a non-pending order answers `OrderTransitionInvalid` (`to`:
     `pending_payment`). The facade checks the status first, so this path is not reached
     through the facade.
   - Instructions email: the amount is the order's `total_cents`, and the deadline is the
     event's `due_at`.
   - `ProcessPaymentNotification._hold` logs `payments.ticket_not_held` for `not_pending` and
     `not_found`, then marks the payment held.
6. **Initiative README**: per step 7, plan 003's row stays unchanged, so the file was not
   edited.
7. **Migration** generated with `just db-revision` and reformatted like 0024. It was applied to
   the development database with `alembic upgrade head` (equivalent to `just db-migrate`) and
   to the test database with `just db-migrate --test`. `alembic check` is clean.

8. **Step 6 split in two subscribers (repair asked by the main session, 2026-10-27).**
   - Plan: `CloseCheckout` subscribes to `orders.order.status_changed` and calls Mercado Pago
     there.
   - Problem (flagged at the first handoff): that subscriber shares the event with orders'
     `SendOrderStatusEmail`, which runs first. A Mercado Pago outage raised and the relay
     retried the whole event, so the customer's status email could be sent again.
   - Done:
     - `RequestCheckoutClose` subscribes to `orders.order.status_changed`. It acts only on
       `pending_payment` → `paid`/`cancelled`. It publishes the new payments event
       `payments.checkout.close_requested` (`CheckoutCloseRequested` in `domain/events.py`,
       payload `order_id`) inside the relay's transaction, with no network call.
     - `CloseCheckout` now subscribes to `payments.checkout.close_requested`. It does
       `get_for_order`, `close_checkout` (raising on a gateway error, so only its own outbox row
       retries; `Ok(False)` logs `payments.checkout_missing`) and `CheckoutRepository.close`.
     - Both are wired in `payments/module.py`. The event is listed in `payments/__init__.py`,
       `docs/architecture.md` and the README.
     - `tests/unit/orders/test_order_emails_wiring.py` now expects `RequestCheckoutClose` on
       `orders.order.status_changed`.

## Test coverage

Tester, 2026-10-27. Baseline (`just check` and `just test-integration`): green, 4014 unit
passed / 1 xfailed and 1138 integration passed. Closing: `just check` 4200 passed, 5 skipped,
1 xfailed (the pre-existing one); `just test-integration` 1184 passed, 5 skipped. No product
code was touched. No GAP found. Files are new, under `apps/api/tests/`.

| Behavior | Source | Layer | Test | State |
| -------- | ------ | ----- | ---- | ----- |
| `Order.await_payment`: later deadline moves (even by 1 s), equal or earlier does not, returns whether it moved | `orders/domain/order.py:376-398` | domain | `unit/orders/test_order_payment_pending_domain.py` | CONFIRMED |
| `await_payment` records `OrderPaymentPending` every time (new or unchanged `due_at`, method, URL), payload shape, no status change or history | `order.py:389-397` | domain | same | CONFIRMED |
| `await_payment` refuses every non-pending status (`ORDERS_TRANSITION_INVALID`, from/to details), records nothing | `order.py:383-384` | domain | same | CONFIRMED |
| `Payment.needs_hold`, `mark_held`, `describe`, `is_live_ticket`, `CASH_TYPES`, `METHOD_LABELS`, `CheckoutCloseRequested` | `payments/domain/payment.py`, `events.py` | domain | `unit/payments/test_payment_tickets_domain.py` | CONFIRMED |
| Facade `hold_for_payment`: moved + reservation extended, not moved (earlier or equal) no extend, second later ticket moves again, no reservation, extension refused (logged `orders.payment_hold_not_extended`, deadline and event kept), not pending (each status), not found, domain `Err` passes through, no commit or release | `orders/application/commands/confirm_payment.py:78-110` | application | `unit/orders/test_hold_for_payment.py` | CONFIRMED |
| `StockHoldsAdapter.extend` forwards and passes inventory's answer | `orders/infrastructure/stock_holds.py` | application | `unit/orders/test_stock_holds_extend_adapter.py` | CONFIRMED |
| Instructions email: subject, body (amount, method, Cancún deadline, optional page, footer), handler reads order by number, uses the event's deadline/label/page and order id, missing order, send failure retries, rejected address | `orders/application/handlers/order_emails.py` (`payment_instructions_email`, `SendPaymentInstructionsEmail`) | application | `unit/orders/test_payment_instructions_email.py` | CONFIRMED |
| Container subscribes only the instructions email to `orders.order.payment_pending` | `orders/module.py` | application | `unit/orders/test_payment_instructions_wiring.py` | CONFIRMED |
| Handler: pending OXXO / SPEI holds once with its label and page, held once per payment on re-delivery, not_pending / not_found marked held and logged, orders' refusal raises and is retried, non-tickets never hold, approved after hold applied with no second hold, page/expiry refreshed without re-hold, hold inside the transaction | `payments/application/handlers/process_payment.py:84-158` | application | `unit/payments/test_process_payment_tickets.py` | CONFIRMED |
| `RequestCheckoutClose` acts only on `pending_payment` to `paid`/`cancelled`; `CloseCheckout` (checkout closed at now, none, unknown preference logged, gateway error raises and leaves it open, idempotent) | `payments/application/handlers/close_checkout.py` | application | `unit/payments/test_close_checkout.py` | CONFIRMED |
| `GetPendingTickets`: labels, newest first, expired and boundary hidden, other payments/orders hidden, unknown token, non-pending order empty | `payments/application/queries/pending_tickets.py` | application | `unit/payments/test_pending_tickets.py` | CONFIRMED |
| `StartCheckout` sets `cash_expires_at` = now + 48 h; a held order gets a new link for its extended window | `payments/application/commands/start_checkout.py` | application | `unit/payments/test_mercadopago_tickets.py` | CONFIRMED |
| Container resolves the pending query (SQL) and wires `CloseCheckout` to its event with the checkout's gateway; adapter `hold` maps `held` / `not_found` / `not_pending` | `payments/module.py`, `infrastructure/order_payments.py` | application | `unit/payments/test_payments_tickets_wiring.py` | CONFIRMED |
| `POST /payments/pending`: 200 with items, 200 empty, 404 `PAYMENTS_ORDER_NOT_FOUND`, 422 on a bad body, token not accepted in URL, public, OpenAPI | `payments/http/router.py:56-65` | http | `unit/payments/test_pending_tickets.py` | CONFIRMED |
| Preference excludes only `atm` and carries `date_of_expiration` | `mercadopago_gateway.py:23,170` | http | updated at implementation: `unit/payments/test_mercadopago_gateway.py` (deviation 4) | CONFIRMED |
| `get_payment` maps method, expiry (offset required), page; bad types become `None`, never unavailable | `mercadopago_gateway.py:_payment` | http (adapter) | `unit/payments/test_mercadopago_tickets.py` | CONFIRMED |
| `close_checkout`: PUT body and URL, bearer, timeout, 2xx ok, 404 `Ok(False)`, other status / transport error unavailable and logged without the token, no token | `mercadopago_gateway.py:68-100` | http (adapter) | `unit/payments/test_mercadopago_tickets.py` | CONFIRMED |
| Sandbox offers OXXO and SPEI, honors `date_of_expiration` | plan acceptance 1 and 2 | http | `test_the_sandbox_offers_oxxo_and_spei_and_honors_the_cash_expiry` | NOT CONFIRMED (needs the Mercado Pago sandbox; verifier) |
| `PUT /checkout/preferences/{id}` makes the old link refuse payments | plan step 6 | http | `test_a_closed_preference_no_longer_accepts_a_payment` | NOT CONFIRMED (needs the sandbox; verifier) |
| Migration 0025: four nullable columns with their types and lengths, constraints and indexes unchanged, old rows kept, downgrade drops only them, round trip | `migrations/versions/0025_payments_tickets.py` | integration | `integration/payments/test_migration_0025_payments_tickets.py` | CONFIRMED |
| Repository reads and writes the four columns; `claim` keeps stored values; `held_at` kept | `sql_payment_repository.py` | integration | `integration/payments/test_sql_tickets.py` | CONFIRMED |
| `SqlPaymentQueries.pending_for_order`: filters, expiry boundary, newest first with id tie-break, one order only | `sql_payment_repository.py:96-125` | integration | same | CONFIRMED |
| `SqlCheckoutRepository.close` (only that order, never reusable, no row is a no-op); request and close over the real outbox; gateway failure leaves it open | `sql_checkout_repository.py`, `close_checkout.py` | integration | same | CONFIRMED |
| Facade + real inventory `extend` in one transaction: saved `payment_due_at` (deviation 1) and reservation `expires_at` equal, outbox event payload, earlier ticket shortens nothing, later one moves again, expired reservation keeps the deadline and pays late, cancelled / unknown order, overdue job cancels at the extended deadline and releases | `confirm_payment.py`, `sql_order_repository.py:151-164` | integration | `integration/orders/test_payment_hold_flow.py` | CONFIRMED |
| Handler end to end: pending OXXO / SPEI stores the ticket and holds once (3 deliveries, one outbox event), paid later applies and commits keeping `held_at`, ticket after a card payment is `refund_due`, ticket for a cancelled order marked held with no email | `process_payment.py`, `order_payments.py` | integration | same | CONFIRMED |
| Email actually delivered to Mailpit, the Cancún text with a real sandbox ticket, the reminder-free overdue cancel email | plan acceptance | e2e | not tested | NOT CONFIRMED (no e2e layer; verifier) |

Notes for the reviewer:
- No GAP: every behavior the plan promises was found in code.
- `Payment.is_live_ticket` (Python) and `SqlPaymentQueries.pending_for_order` (SQL) are tested
  with the same table of cases, so the two implementations cannot drift apart unnoticed.
- The in-memory `FakeGateway`, `InMemoryPayments` and `InMemoryStockHolds` extensions are
  exercised only through the tests above.
- The first closing run of `just check` failed on two ruff E501 lines in new tests and, on the
  second, on mypy errors in new tests; both were fixed in the tests before the final green run.

## Review findings

## Verification
