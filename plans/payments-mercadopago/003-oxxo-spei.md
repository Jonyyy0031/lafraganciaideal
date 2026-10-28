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

8. **Review repair (added 2026-10-27 after the first review: M1, L1, L2, L3, and the finding
   `payments-cancelled-order-keeps-live-ticket`)**
   - Files: `apps/api/src/fragancia_api/modules/payments/domain/payment.py` (modify), `apps/api/src/fragancia_api/modules/payments/domain/repositories.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/sql_payment_repository.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/in_memory.py` (modify), `apps/api/src/fragancia_api/modules/payments/application/handlers/process_payment.py` (modify), `apps/api/src/fragancia_api/modules/payments/application/handlers/close_checkout.py` (modify), `apps/api/src/fragancia_api/modules/payments/application/ports.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/mercadopago_gateway.py` (modify), `apps/api/src/fragancia_api/modules/payments/__init__.py` (modify), `apps/api/README.md` (modify), `plans/findings/payments-cancelled-order-keeps-live-ticket.md` (modify), `plans/payments-mercadopago/README.md` (modify)
   - Do:
     - **M1 (decision 19): the 48 hours count from the first ticket.**
       - Add `PaymentRepository.first_hold_expiry(order_id) -> datetime | None`: the
         `expires_at` of the order's earliest-held payment (`held_at` not null, ordered by
         `held_at`).
       - In the handler's hold path, pass `until = min(payment.expires_at, first)` when there
         is an earlier hold.
       - A later ticket therefore never moves the deadline past the first ticket's expiry. It
         is still recorded and still sends its instructions email, which shows the order's
         actual deadline (the event's `due_at`).
     - **L1.** `Payment.needs_hold(now)` is also false when `expires_at <= now`. Such a payment
       is `mark_held` with no hold and no email, and logged as `payments.ticket_expired_on_arrival`.
     - **L2.** `get_payment` keeps `ticket_url` only when it is at most 500 characters (the
       column size). Otherwise it stores None and logs `payments.ticket_url_too_long`, so a
       long URL never blocks the payment's later notifications.
     - **L3.** `close_checkout` treats any 4xx except 404 as permanent:
       `Ok(False)`, logged as `payments.checkout_close_refused` with the status. Only 5xx and
       transport errors are `Err` and retry.
     - **Finding (decision 20): cancel live tickets when the order stops waiting.**
       - `PaymentGateway.cancel_payment(payment_id) -> Result[bool, PaymentGatewayUnavailable]`
         sends `PUT {api_url}/v1/payments/{id}` with `{"status": "cancelled"}`, as the official
         SDK's `src/clients/payment/cancel/index.ts` does (checked 2026-10-27).
         2xx → `Ok(True)`; 4xx → `Ok(False)`, logged as `payments.ticket_cancel_refused`
         (e.g. already paid or already expired); 5xx and transport errors → `Err`.
       - In `CloseCheckout`, after the preference, cancel each of the order's payments with
         status `pending` and a cash type (any expiry), read through a new
         `PaymentRepository.pending_cash(order_id)`. Any `Err` raises, so the close event
         retries; repeating a cancel is harmless.
       - The row is not edited here: Mercado Pago's notification of the cancelled status
         refreshes it as usual. A ticket paid before the cancel still arrives `approved` and is
         `refund_due` (plan 002).
       - Set the finding to `status: planned` with `plan: payments-mercadopago/003`.
     - Describe both rules in `payments/__init__.py` and the README.
   - Observable result:
     - a second ticket issued 47 h after the first leaves the deadline at the first one's
       expiry;
     - an already-expired ticket sends no email;
     - cancelling an order with a pending OXXO ticket cancels it in Mercado Pago, and a later
       notification stores `cancelled`.

9. **Re-review repair (added 2026-10-27 after the re-review: R1, R2, R3)**
   - Files: `apps/api/src/fragancia_api/modules/payments/application/queries/pending_tickets.py` (modify), `apps/api/src/fragancia_api/modules/payments/contracts.py` (modify), `apps/api/openapi.json` (modify), `apps/api/src/fragancia_api/modules/payments/domain/events.py` (modify), `apps/api/src/fragancia_api/modules/payments/application/handlers/process_payment.py` (modify), `apps/api/src/fragancia_api/modules/payments/application/handlers/cancel_ticket.py` (create), `apps/api/src/fragancia_api/modules/payments/module.py` (modify), `apps/api/src/fragancia_api/modules/payments/__init__.py` (modify), `docs/architecture.md` (modify), `apps/api/README.md` (modify)
   - Do:
     - **R1.** `GetPendingTickets` returns each item's `expires_at` as
       `min(ticket.expires_at, order.payment_due_at)`; the order is already resolved through
       `payable`. Update `PendingTicket.expires_at`'s description to say it is when the ticket
       stops paying the order, then run `uv run just openapi`.
     - **R2 (decision 20, applied to late notifications).** When the hold answers `not_pending`
       for a payment that `needs_hold` (a pending cash ticket whose order was already paid or
       cancelled), the handler publishes `TicketCancelRequested`
       (`payments.ticket.cancel_requested`, payload `payment_id`) inside its transaction,
       besides `mark_held`.
       - A new subscriber `CancelTicket` (`cancel_ticket.py`) calls `gateway.cancel_payment`.
         `Err` raises, so only that event retries; `Ok(False)` is already logged by the gateway.
       - `not_found` publishes nothing.
       - Wire it in `module.py` and list the event in `payments/__init__.py`.
     - **R3.** The payments row of `docs/architecture.md` and the README's payments section
       describe:
       - the cap at the first ticket (decision 19);
       - ticket cancellation on close and on late notifications (decision 20);
       - which Mercado Pago answers are permanent (400, 404, 409, 422) and which retry.
   - Observable result: the pending query never shows a deadline past the order's; a ticket
     notified after the order was cancelled is cancelled in Mercado Pago; `uv run just check`
     green.

10. **Verification repair (added 2026-10-27 after the first sandbox run: V1, V2, decisions 21
    and 22)**
   - Files: `apps/api/src/fragancia_api/modules/payments/infrastructure/mercadopago_gateway.py` (modify), `apps/api/src/fragancia_api/modules/payments/application/handlers/process_payment.py` (modify), `apps/api/src/fragancia_api/modules/payments/domain/payment.py` (modify), `apps/api/src/fragancia_api/modules/payments/application/ports.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/order_payments.py` (modify), `apps/api/src/fragancia_api/modules/orders/domain/order.py` (modify), `apps/api/src/fragancia_api/modules/orders/application/commands/confirm_payment.py` (modify), `apps/api/src/fragancia_api/modules/orders/application/handlers/order_emails.py` (modify), `apps/api/src/fragancia_api/modules/orders/__init__.py` (modify), `apps/api/src/fragancia_api/modules/payments/__init__.py` (modify), `apps/api/README.md` (modify), `docs/architecture.md` (modify), `plans/findings/payments-ticket-cancel-refused-with-test-credentials.md` (create)
   - Do:
     - **V1: the link close body.** `close_checkout` sends only `{"expires": true, "expiration_date_to": at}`.
       Mercado Pago answered `400 invalid date of expiration` when `date_of_expiration` was set
       to now, and accepted the body without it (verified live 2026-10-27; the preference showed
       the new `expiration_date_to`).
     - **Decision 22: a grace margin after the ticket expires.**
       - Add `TICKET_GRACE = timedelta(hours=48)` in `payments/domain/payment.py`.
       - The hold's order deadline becomes the ticket's expiry plus `TICKET_GRACE`. The
         decision 19 cap becomes the first ticket's expiry plus `TICKET_GRACE`.
       - The reservation is extended to the same time, so the overdue job does not cancel the
         order while a store payment is still being credited.
       - The ticket's own expiry stays as Mercado Pago reports it, in `payments.payments` and in
         `/payments/pending`. That query now caps at the order deadline, which includes the
         margin.
     - **The email shows the ticket's deadline, not the order's.**
       - `hold_for_payment` and `Order.await_payment` take a new `pay_by: datetime`, the
         ticket's own expiry. The facade's `until` is the order deadline.
       - `OrderPaymentPending` gains `pay_by`, and the instructions email prints `pay_by`.
       - The payments adapter passes both values.
     - **Decision 21.** No code change: the OXXO expiry is Mercado Pago's (the end of the day in
       Mexico City time, up to about 56 h), and the hold already uses it.
     - **V2.** Record the finding `payments-ticket-cancel-refused-with-test-credentials`:
       - `PUT /v1/payments/{id}` with `{"status":"cancelled"}` answered
         `401 Unauthorized use of live credentials` with the test seller account's credentials;
       - the close event therefore retries until it fails;
       - whether cancelling works with the client's production credentials is unverified.
     - Describe decisions 21 and 22 in both `__init__.py` files, the README and
       `docs/architecture.md`.
   - Observable result:
     - an OXXO ticket expiring at T holds the order and the reservation until T + 48 h;
     - the instructions email still says "antes del" T;
     - closing a link no longer gets 400.

11. **Step 10 review repair (added 2026-10-28: S1)**
   - Files: `apps/api/src/fragancia_api/modules/payments/application/handlers/process_payment.py` (modify), `apps/api/src/fragancia_api/modules/payments/application/queries/pending_tickets.py` (modify), `apps/api/src/fragancia_api/modules/payments/contracts.py` (modify), `apps/api/openapi.json` (modify), `apps/api/src/fragancia_api/modules/payments/__init__.py` (modify), `apps/api/README.md` (modify)
   - Do:
     - **The email's deadline leaves the margin to credit the payment.** In the hold path,
       `pay_by = until - TICKET_GRACE`. That is the earlier of this ticket's expiry and the
       first ticket's expiry. A single ticket still gets its own expiry. A second ticket is never
       told a deadline inside the margin or at the cancel time.
     - **`/payments/pending` uses the same rule.** Each item's `expires_at` is
       `min(ticket.expires_at, order.payment_due_at - TICKET_GRACE)`. The ticket's own expiry
       stays stored as Mercado Pago reports it.
       - Update `PendingTicket.expires_at`'s description: the last moment to pay so that the
         order still waits for the credit. Then run `uv run just openapi`.
     - Describe the rule in `payments/__init__.py` and the README.
   - Observable result:
     - first ticket expiring at F, then a second expiring at F+30h: the second's email and
       `/payments/pending` say F;
     - a single ticket's email still says its own expiry.

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
| `Payment.needs_hold(now)` (superseded: now also requires `expires_at > now`), `mark_held`, `describe`, `is_live_ticket`, `CASH_TYPES`, `METHOD_LABELS`, `CheckoutCloseRequested` | `payments/domain/payment.py`, `events.py` | domain | `unit/payments/test_payment_tickets_domain.py` | CONFIRMED |
| (Superseded by step 10: `pay_by` parameter and event, see its rows.) Facade `hold_for_payment`: moved + reservation extended, not moved (earlier or equal) no extend, second later ticket moves again, no reservation, extension refused (logged `orders.payment_hold_not_extended`, deadline and event kept), not pending (each status), not found, domain `Err` passes through, no commit or release | `orders/application/commands/confirm_payment.py:78-110` | application | `unit/orders/test_hold_for_payment.py` | CONFIRMED |
| `StockHoldsAdapter.extend` forwards and passes inventory's answer | `orders/infrastructure/stock_holds.py` | application | `unit/orders/test_stock_holds_extend_adapter.py` | CONFIRMED |
| (Superseded by step 10: the email prints `pay_by`, not `due_at`.) Instructions email: subject, body (amount, method, Cancún deadline, optional page, footer), handler reads order by number, uses the event's deadline/label/page and order id, missing order, send failure retries, rejected address | `orders/application/handlers/order_emails.py` (`payment_instructions_email`, `SendPaymentInstructionsEmail`) | application | `unit/orders/test_payment_instructions_email.py` | CONFIRMED |
| Container subscribes only the instructions email to `orders.order.payment_pending` | `orders/module.py` | application | `unit/orders/test_payment_instructions_wiring.py` | CONFIRMED |
| (Superseded by step 10: the hold is now expiry + `TICKET_GRACE`.) Handler: pending OXXO / SPEI holds once with its label and page, held once per payment on re-delivery, not_pending / not_found marked held and logged, orders' refusal raises and is retried, non-tickets never hold, approved after hold applied with no second hold, page/expiry refreshed without re-hold, hold inside the transaction | `payments/application/handlers/process_payment.py:84-158` | application | `unit/payments/test_process_payment_tickets.py` | CONFIRMED |
| `RequestCheckoutClose` acts only on `pending_payment` to `paid`/`cancelled`; `CloseCheckout` (checkout closed at now, unknown preference logged, gateway error raises and leaves it open, idempotent; superseded: "none" no longer returns early, tickets are cancelled, see the repair rows) | `payments/application/handlers/close_checkout.py` | application | `unit/payments/test_close_checkout.py` | CONFIRMED |
| `GetPendingTickets`: labels, newest first, expired and boundary hidden, other payments/orders hidden, unknown token, non-pending order empty (superseded: `expires_at` is now capped at the order's deadline, see the step 9 rows; the `query` helper there uses a 48 h deadline) | `payments/application/queries/pending_tickets.py` | application | `unit/payments/test_pending_tickets.py` | CONFIRMED |
| `StartCheckout` sets `cash_expires_at` = now + 48 h; a held order gets a new link for its extended window | `payments/application/commands/start_checkout.py` | application | `unit/payments/test_mercadopago_tickets.py` | CONFIRMED |
| Container resolves the pending query (SQL) and wires `CloseCheckout` to its event with the checkout's gateway; adapter `hold` maps `held` / `not_found` / `not_pending` | `payments/module.py`, `infrastructure/order_payments.py` | application | `unit/payments/test_payments_tickets_wiring.py` | CONFIRMED |
| `POST /payments/pending`: 200 with items, 200 empty, 404 `PAYMENTS_ORDER_NOT_FOUND`, 422 on a bad body, token not accepted in URL, public, OpenAPI | `payments/http/router.py:56-65` | http | `unit/payments/test_pending_tickets.py` | CONFIRMED |
| Preference excludes only `atm` and carries `date_of_expiration` | `mercadopago_gateway.py:23,170` | http | updated at implementation: `unit/payments/test_mercadopago_gateway.py` (deviation 4) | CONFIRMED |
| `get_payment` maps method, expiry (offset required), page; bad types become `None`, never unavailable | `mercadopago_gateway.py:_payment` | http (adapter) | `unit/payments/test_mercadopago_tickets.py` | CONFIRMED |
| `close_checkout`: PUT body and URL, bearer, timeout, 2xx ok, 404 `Ok(False)`, 401/403/408/429/5xx / transport error unavailable and logged without the token, no token. (Superseded: 400/409/422 by step 8; the PUT body no longer has `date_of_expiration` by step 10, see its rows.) | `mercadopago_gateway.py:68-100` | http (adapter) | `unit/payments/test_mercadopago_tickets.py` | CONFIRMED |
| Sandbox offers OXXO and SPEI, honors `date_of_expiration` | plan acceptance 1 and 2 | http | `test_the_sandbox_offers_oxxo_and_spei_and_honors_the_cash_expiry` | NOT CONFIRMED (needs the Mercado Pago sandbox; verifier) |
| `PUT /checkout/preferences/{id}` makes the old link refuse payments | plan step 6 | http | `test_a_closed_preference_no_longer_accepts_a_payment` | NOT CONFIRMED (needs the sandbox; verifier) |
| Migration 0025: four nullable columns with their types and lengths, constraints and indexes unchanged, old rows kept, downgrade drops only them, round trip | `migrations/versions/0025_payments_tickets.py` | integration | `integration/payments/test_migration_0025_payments_tickets.py` | CONFIRMED |
| Repository reads and writes the four columns; `claim` keeps stored values; `held_at` kept | `sql_payment_repository.py` | integration | `integration/payments/test_sql_tickets.py` | CONFIRMED |
| `SqlPaymentQueries.pending_for_order`: filters, expiry boundary, newest first with id tie-break, one order only | `sql_payment_repository.py:96-125` | integration | same | CONFIRMED |
| `SqlCheckoutRepository.close` (only that order, never reusable, no row is a no-op); request and close over the real outbox; gateway failure leaves it open | `sql_checkout_repository.py`, `close_checkout.py` | integration | same | CONFIRMED |
| Facade + real inventory `extend` in one transaction: saved `payment_due_at` (deviation 1) and reservation `expires_at` equal, outbox event payload, earlier ticket shortens nothing, later one moves again, expired reservation keeps the deadline and pays late, cancelled / unknown order, overdue job cancels at the extended deadline and releases | `confirm_payment.py`, `sql_order_repository.py:151-164` | integration | `integration/orders/test_payment_hold_flow.py` | CONFIRMED |
| Handler end to end: pending OXXO / SPEI stores the ticket and holds once (3 deliveries, one outbox event), paid later applies and commits keeping `held_at`, ticket after a card payment is `refund_due`, ticket for a cancelled order marked held with no email | `process_payment.py`, `order_payments.py` | integration | same | CONFIRMED |
| **Repair (step 8, tester 2026-10-27).** L1: `needs_hold(now)` boundary (`expires_at == now` is not held), `expired_on_arrival` for every case, never both | `payment.py:needs_hold, expired_on_arrival` | domain | `unit/payments/test_ticket_hold_repair.py` | CONFIRMED |
| M1: first ticket holds to its own expiry; later ticket capped at the first's expiry (record keeps Mercado Pago's expiry); earlier second ticket not raised; third capped at the first; other order and the ticket itself never cap; expired-on-arrival never caps | `process_payment.py:_hold` | application | `unit/payments/test_ticket_hold_repair.py` | CONFIRMED |
| `InMemoryPayments.first_hold_expiry` / `pending_cash` (earliest `held_at`, id tie-break, ignores `expires_at <= held_at`, one order, copies) | `infrastructure/in_memory.py` | application | same | CONFIRMED |
| L1: expired-on-arrival ticket (past, equal to now): no call to orders (no email), `held_at` set, logged `payments.ticket_expired_on_arrival`, not repeated on re-delivery; one second ahead still held | `process_payment.py:85-98` | application | same | CONFIRMED |
| Decision 20: `CloseCheckout` cancels pending cash tickets after closing the preference (oldest first, expired ones too, with no stored checkout, only that order's pending cash tickets), refused cancel (`Ok(False)`) goes on and edits no row, `Err` on cancel raises, `Err` on close raises before any cancel | `close_checkout.py` | application | same | CONFIRMED |
| L2: `ticket_url` of 500 characters kept; over 500 dropped, logged `payments.ticket_url_too_long` (payment id, length, no URL), payment still mapped | `mercadopago_gateway.py:_payment` | http (adapter) | `unit/payments/test_mercadopago_repair.py` | CONFIRMED |
| L3 / repair deviation 5, `close_checkout`: 400/409/422 `Ok(False)` + `payments.checkout_close_refused`; 404 `Ok(False)` silent; 401/403/408/429/500/502/503 `Err` | `mercadopago_gateway.py:close_checkout` | http (adapter) | same | CONFIRMED |
| `cancel_payment`: `PUT /v1/payments/{id}` `{"status":"cancelled"}`, bearer, timeout, slash; 2xx `Ok(True)`; 400/404/409/422 `Ok(False)` + `payments.ticket_cancel_refused`; 401/403/408/429/5xx and transport `Err`; no token | `mercadopago_gateway.py:cancel_payment` | http (adapter) | same | CONFIRMED |
| `SqlPaymentRepository.first_hold_expiry` (earliest hold, tie-break, ignores expired-on-arrival, one order) and `pending_cash` (filters, order) | `sql_payment_repository.py` | integration | `integration/payments/test_sql_tickets_repair.py` | CONFIRMED |
| `CloseCheckout` over SQL: cancels pending tickets, edits no row, works with no stored checkout, raises on gateway error | `close_checkout.py` | integration | same | CONFIRMED |
| **Step 9 (tester 2026-10-27).** (Superseded by step 11: the cap is `payment_due_at - TICKET_GRACE`.) R1: each ticket's `expires_at` is `min(own expiry, order.payment_due_at)` (later, earlier, equal, per ticket, other fields untouched, filtering unchanged); http answer carries the capped value; contract description says "whichever is earlier" | `pending_tickets.py` | application / http | `unit/payments/test_ticket_late_repair.py` | CONFIRMED |
| R2: handler publishes `payments.ticket.cancel_requested` (payload `payment_id`) only on `not_pending` for a ticket that needs a hold, once on re-delivery; none on `held`, `not_found`, non-cash, non-pending status, expired, or an orders refusal | `process_payment.py:_hold` | application | same | CONFIRMED |
| R2: `CancelTicket` calls `cancel_payment`, a refused cancel is not an error, a gateway `Err` raises `payments.gateway_unavailable` | `handlers/cancel_ticket.py` | application | same | CONFIRMED |
| **Step 10 (tester 2026-10-28).** V1: the `close_checkout` body is exactly `{expires, expiration_date_to}`, no `date_of_expiration` | `mercadopago_gateway.py:close_checkout` | http (adapter) | `unit/payments/test_ticket_grace.py` | CONFIRMED |
| (Superseded by step 11: `pay_by = until - TICKET_GRACE`, the first expiry for a later ticket.) Decision 22: `TICKET_GRACE` = 48 h; single ticket holds to expiry + grace and tells its own expiry as `pay_by`; the record keeps Mercado Pago's expiry; later ticket capped at first expiry + grace; `pay_by` = `min(own expiry, until)` (beyond the cap → the cap; inside the margin → own expiry; earlier second → own expiry, hold own expiry + grace) | `process_payment.py:_hold`, `payment.py` | application | same | CONFIRMED |
| (Superseded by step 11: cap at deadline less the margin.) Decision 22: `/payments/pending` shows the ticket's own expiry when inside the order deadline, still capped at the deadline otherwise (unit + http) | `pending_tickets.py` | application / http | same | CONFIRMED |
| `pay_by` flows from `Order.await_payment` and the facade into `OrderPaymentPending` apart from `due_at` (also when the deadline does not move); the email and its handler print `pay_by`, not the deadline | `order.py`, `confirm_payment.py`, `order_emails.py` | domain / application | `unit/orders/test_pay_by.py` | CONFIRMED |
| (Superseded by step 11 for the later ticket's `pay_by`: the first expiry.) Decision 22 over PostgreSQL: order and reservation reach expiry + grace, event `pay_by` = expiry and `due_at` = expiry + grace, stored expiry is Mercado Pago's, overdue job leaves the order alone; a later ticket is capped at first expiry + grace with `pay_by` = the earlier of its expiry and the cap | `process_payment.py`, `confirm_payment.py` | integration | `integration/orders/test_ticket_grace_flow.py` | CONFIRMED |
| **Step 11 (tester 2026-10-28).** S1: a single ticket tells its own expiry; a later ticket (10, 47, 60, 200 h after the first) tells the first expiry; an earlier second ticket tells its own; checked through the email text (Cancún time) | `process_payment.py:_hold` (`pay_by = until - TICKET_GRACE`) | application | `unit/payments/test_ticket_pay_by_margin.py` | CONFIRMED |
| S1: `/payments/pending` caps at `min(own expiry, payment_due_at - TICKET_GRACE)` (inside, beyond, exactly equal, the deadline itself never shown), unit and http | `pending_tickets.py` | application / http | same | CONFIRMED |
| S1 over PostgreSQL with two tickets: outbox `pay_by` values are `[first, first]` for a later second ticket and `[second, first]` for an earlier one; `due_at` stays first + grace, order and reservation likewise | `process_payment.py`, `confirm_payment.py` | integration | `integration/orders/test_ticket_pay_by_flow.py` | CONFIRMED |
| R2 wiring: the container subscribes only `CancelTicket` to `payments.ticket.cancel_requested`, with the gateway the checkout uses | `payments/module.py:111` | application | `unit/payments/test_payments_tickets_wiring.py` | CONFIRMED |
| R2 end to end: ticket notified after the order was cancelled is marked held, no instructions email, one outbox `cancel_requested` row, `CancelTicket` cancels it in the fake gateway; a pending order publishes no request | `process_payment.py`, `cancel_ticket.py` | integration | `integration/orders/test_late_ticket_flow.py` | CONFIRMED |
| Handler end to end: second ticket capped at the first's expiry (order, reservation, both outbox events), expired-on-arrival holds nothing and sends no email, does not cap the next real ticket | `process_payment.py` | integration | `integration/orders/test_payment_hold_repair_flow.py` | CONFIRMED |
| The real Mercado Pago accepts `PUT /v1/payments/{id}` `cancelled` for an OXXO / SPEI ticket and the 4xx classification matches its real answers | decision 20 | http | not tested | NOT CONFIRMED (needs the sandbox; verifier) |
| Email actually delivered to Mailpit, the Cancún text with a real sandbox ticket, the reminder-free overdue cancel email | plan acceptance | e2e | not tested | NOT CONFIRMED (no e2e layer; verifier) |

Notes for the reviewer:
- No GAP: every behavior the plan promises was found in code.
- `Payment.is_live_ticket` (Python) and `SqlPaymentQueries.pending_for_order` (SQL) are tested
  with the same table of cases, so the two implementations cannot drift apart unnoticed.
- The in-memory `FakeGateway`, `InMemoryPayments` and `InMemoryStockHolds` extensions are
  exercised only through the tests above.
- Repair pass (tester 2026-10-27): `just check` 4277 passed, 5 skipped, 1 xfailed;
  `just test-integration` 1203 passed, 5 skipped. No product code touched, no GAP.
- The first closing run of `just check` failed on two ruff E501 lines in new tests and, on the
  second, on mypy errors in new tests; both were fixed in the tests before the final green run.

## Review findings

Reviewer, 2026-10-27. Diff `main...HEAD` (7 commits) plus a clean worktree. No PR exists yet.

### Pass 1 — Checklist: 14/15 passed, 1 not applicable, 0 failed

- [x] `plans-scope`: "Every change is inside the plan" (40 declared, 57 changed). The hot files
      (`container.py`, `docs/modules.json`, `.importlinter`) are not touched.
- [x] `uv run just check`: green (4200 passed, 5 skipped, 1 xfailed; harness 685 passed).
- [x] `uv run just test-integration`: green (1184 passed, 5 skipped).
- [x] Business rules are in the domain: `Order.await_payment`, and `Payment.needs_hold`,
      `is_live_ticket` and `METHOD_LABELS`. The router and the adapters have none.
- [x] CQRS-lite: `hold_for_payment` runs aggregate, then repository, then publish inside
      `transactions.run` and returns a `Result`. `GetPendingTickets` reads through the
      `PaymentQueries` port (`Database.reader()`) and returns `PendingTicket` models.
      `CheckoutRepository.close` is not specific to one screen.
- [x] Contracts: `PendingTicketsRequest`, `PendingTicket` and `PendingTickets` are in
      `payments/contracts.py`. `fragancia_api.main.openapi --check` reports "openapi.json is up
      to date".
- [x] Errors: 404 `PAYMENTS_ORDER_NOT_FOUND` (an existing code). The facade's refusals come back
      as `Err`. The gateway's failures are `PaymentGatewayUnavailable`.
- [x] Money is integer cents. Datetimes come from `Clock` and are timezone-aware: the gateway
      drops a `date_of_expiration` with no offset. No new ids are created.
- [x] Migration 0025 adds four nullable columns, drops nothing and adds no foreign keys. The
      downgrade drops exactly those four, and the round-trip test is green.
- [x] Routes: `/payments/pending` is on `public_router` (`payments/http/router.py:19`). There
      are no admin routes.
- [x] Wiring: the subscribers and the query are registered in `orders/module.py` and
      `payments/module.py`. The container tests are green and no module is registered twice.
- [x] No secrets, `.env` contents or real personal data.
- [x] Deviations are honest. Spot-checked: deviation 1 is the one added line
      `payment_due_at=order.payment_due_at` (`sql_order_repository.py:162`). Deviation 8 is the
      two-subscriber split (`close_checkout.py:29-103`), and `RequestCheckoutClose` joins the
      relay's session (`database.py:64-66`). The initiative README diff (decisions 14 to 18)
      comes from the plan-approval commit `2ece0e9`, not from the implementation.
- [x] Docs: `apps/api/README.md`, `docs/architecture.md`, `.env.example`, `config.py`
      comments and both modules' `__init__.py` describe the new behavior. The finding moved to
      `planned`.
- [ ] PR body: not applicable, no PR yet. The main session must write it with the six sections.

### Pass 2 — Findings

**High:** none.

**Medium**

- **M1 (needs a user decision, not a code defect against the plan): tickets can be chained, so
  one unpaid order can hold its units with no limit.**
  - Where: `payments/application/commands/start_checkout.py:102`
    (`cash_expires_at=now + CASH_TICKET_TTL`), `payments/domain/checkout.py:19` (a stored link
    is reused only while `expires_at == payment_due_at`) and `orders/domain/order.py:385`
    (`moved = until > self.payment_due_at`, with no ceiling).
  - Scenario: the order is placed at T, and a ticket issued at T+10 min holds it until about
    T+48 h. The hold changes `payment_due_at`, so the next "Pagar" builds a new preference.
    That preference is valid until T+48 h, with `date_of_expiration` = its creation time + 48 h.
    At T+47 h the guest issues a second ticket from it, which expires at about T+95 h. The hold
    moves the deadline and extends the reservation again, and each round sends another
    instructions email. Repeated every two days, the order never expires: the units stay
    reserved and no one else can buy them.
  - The unit test `test_an_order_held_for_a_ticket_gets_a_new_link_for_its_extended_window`
    (`tests/unit/payments/test_mercadopago_tickets.py:252`) confirms the new link. The code
    matches the plan's step 2 and decision 17 ("extends to the ticket that expires latest (up to
    48 hours)"). The plan's own reasoning ("never before 47.5 h for a fresh order") does not
    cover a link created after a hold.
  - The user decides whether "up to 48 hours" is counted from each ticket (the current behavior)
    or from the first one or from the order. A possible fix is to cap the window, for example
    `cash_expires_at = min(now + 48 h, first hold + 48 h)` or a ceiling on `until` in
    `hold_for_payment`. This would also cover a Mercado Pago `date_of_expiration` beyond what we
    asked for, which today is trusted without a bound.

**Low**

- **L1: a ticket that has already expired still sends the instructions email, with a deadline
  in the past.** `payments/domain/payment.py:85-92` (`needs_hold` does not check
  `expires_at > now`) and `order.py:385-397` (the event is always recorded). Scenario: a
  notification is retried by the relay or arrives late from Mercado Pago after the ticket's
  `date_of_expiration`, while the order is still pending, for example kept open by a later
  ticket. The customer gets "paga … antes del <a past date>" for a ticket that can no longer be
  paid. Fix: skip the hold and mark it held when `expires_at <= now`. Small.
- **L2 (uncertain): a `ticket_url` longer than 500 characters would block every later
  notification for that payment, including its approval.** `payments/infrastructure/tables.py:41`
  (`String(500)`). `describe` stores the URL on every delivery, so the `INSERT`/`UPDATE` raises
  a `DataError` on each attempt. The relay retries for about 24 h and then fails the event
  permanently, and an approved payment would never mark the order paid. Mercado Pago's ticket
  URLs are usually about 100–150 characters, so this is unlikely. The fix would be to truncate
  or drop an oversized URL in the gateway's `_text`, or to use `Text`.
- **L3 (uncertain, for the verifier's sandbox run): Mercado Pago may refuse the close body.**
  `mercadopago_gateway.py:80-89` sends `date_of_expiration = expiration_date_to = now` and
  `expires: true`. It may also do this for a preference that already expired: the overdue
  cancel happens after `payment_due_at`, the moment the preference stopped taking payments.
  If Mercado Pago answers 400 in either case, `CloseCheckout` raises on every attempt, and each
  cancelled order leaves one `payments.checkout.close_requested` row retrying for about 24 h
  before it fails permanently. Only the sandbox can tell. The acceptance criterion "old link no
  longer lets the guest pay" covers the first case. The verifier should also try closing an
  already-expired preference.

**Outside the scope of this plan:** a new finding,
`plans/findings/payments-cancelled-order-keeps-live-ticket.md` (status `open`). Closing the link
does not cancel an OXXO or SPEI ticket that was already issued. That ticket stays payable after
the order is cancelled or paid by card, which leads to `refund_due`.

**Result:** no High findings. M1 is a product decision that could need a code change, so the
status stays `review` until the user rules on it. L1 is a small fix that can go with M1's
repair. L2 and L3 are uncertain and can be accepted or left to the verifier.

### Re-review after the step 8 repair (reviewer, 2026-10-27)

Diff `04c3003..HEAD` (commits `3ed0100`, `f2327bd`, `0705654` and `b3d7ddb`), read together
with the Repair log below and decisions 19 and 20. The worktree is clean apart from this edit.

**Checklist: 13 of 15 passed, 1 failed, 1 not applicable.**
- `plans-scope`: "Every change is inside the plan" (41 declared, 62 changed). The hot files
  are not touched.
- `uv run just check`: green (4277 passed, 5 skipped, 1 xfailed; harness 685 passed).
- `uv run just test-integration`: green (1203 passed, 5 skipped).
- There is no new migration. `openapi.json` needs no update: the contracts did not change.
- Business rules: `needs_hold(now)` and `expired_on_arrival(now)` are in the domain. The cap
  (`min(expires_at, first)`) is in the handler, and the ticket cancel is in `CloseCheckout`.
  Both are application orchestration, which is acceptable.
- Wiring: `CloseCheckout` gets `payments=`, and one `SqlPaymentRepository` is shared with
  `ProcessPaymentNotification` (repair deviation 1). The container tests are green.
- Repair deviations: spot-checked 5 and 6 against the code. Deviation 5 matches
  `PERMANENT_REFUSALS = {400, 404, 409, 422}` in `mercadopago_gateway.py:30`, used at `:103`
  and `:131`. Deviation 6 matches `expires_at > held_at` in `sql_payment_repository.py:66`
  and in the in-memory fake. Step 8's own text still says "any 4xx except 404" and
  "4xx → `Ok(False)`". Deviation 5 records that this is superseded, so the record is honest.
- **FAILED, stale docs:** `docs/architecture.md:65` (the `payments` row) still says that
  `CloseCheckout` "expires the Mercado Pago preference and closes the stored checkout". It
  says nothing about cancelling the pending tickets (decision 20), the 48 hours counted from
  the first ticket (decision 19), or the 4xx classification. `payments/__init__.py` and
  `apps/api/README.md` were updated, but the architecture doc was left out of step 8's file
  list. This is finding R3 below.
- Not applicable: the PR body (no PR yet).

**The first review's findings, one by one**
- **M1: closed.** `_hold` caps `until` at `first_hold_expiry`
  (`process_payment.py:144-147`). The current payment is never its own "first", because its
  `held_at` is still null when the cap is read. The integration test
  `test_payment_hold_repair_flow.py` checks the order and the reservation. A ticket chain now
  stops at the first ticket's expiry. The overdue cancel then publishes
  `close_requested`, which cancels the later tickets (decision 20), so the units are released
  for good. The concurrency race that the main session accepted (repair deviation 4) behaves
  as before the repair and is not a regression.
- **L1: closed.** `needs_hold(now)` and `expired_on_arrival(now)` are disjoint and together
  cover every pending ticket that is not held (`payment.py:85-106`). An expired-on-arrival
  ticket gets `mark_held` with no call to orders, and deviation 6 keeps it from capping a
  later hold.
- **L2: closed.** `get_payment` drops a URL longer than 500 characters before it reaches the
  column (`mercadopago_gateway.py:202-212`). Exactly 500 characters is kept, which matches
  `String(500)`.
- **L3: closed in code.** 400, 409 and 422 are `Ok(False)` and logged, so they are no longer a
  24-hour retry loop. Whether Mercado Pago really answers like this is still for the
  verifier's sandbox run, and it is listed as NOT CONFIRMED in Test coverage.
- **Finding `payments-cancelled-order-keeps-live-ticket`: closed for the normal path.** The
  finding is now `planned` for 003. `CloseCheckout` cancels each pending cash ticket, also
  when there is no stored checkout (repair deviation 2). A refused cancel is permanent and
  logged. An `Err` raises, and the whole event rolls back, the checkout close included. That
  rollback is harmless: on retry the close is repeated, which is idempotent, and a ticket
  cancelled again gets a permanent refusal. Leftover gap: R2.

**New findings from the repair**

High and Medium: none.

- **R1 (Low): the order page shows a later deadline than the order really has.**
  `SqlPaymentQueries.pending_for_order` (`sql_payment_repository.py:134`) and
  `GetPendingTickets` (`pending_tickets.py:25`) return each ticket's own Mercado Pago
  `expires_at`. After decision 19, a second ticket's `expires_at` can be up to about 47 hours
  later than the order's `payment_due_at`.
  - Scenario: the first ticket expires at T+48 h, and a second ticket is issued at T+47 h with
    expiry T+95 h. `POST /payments/pending` lists the second ticket as payable until T+95 h.
    The overdue job then cancels the order at T+48 h and decision 20 cancels the ticket.
  - The instructions email shows the correct deadline, because it uses the event's `due_at`.
    The contract's description, "When Mercado Pago stops accepting the ticket.", is also no
    longer true, because we cancel the ticket at the order's deadline.
  - Nothing renders this field yet (phase 3), so no customer is affected today. A possible fix
    is to return `min(ticket.expires_at, order.payment_due_at)`; `PayableOrder` already has
    `payment_due_at`.
- **R2 (Low, a narrow race, record or accept): a ticket whose notification is processed after
  the close is never cancelled.**
  - `CloseCheckout` cancels only the rows already in `payments.payments` (`pending_cash`,
    `close_checkout.py:87-104`).
  - Scenario: the guest issues an OXXO ticket and, moments later, the order is paid by card,
    or the overdue cancel runs. If the ticket's webhook reaches the worker after
    `close_requested` has been handled, `_hold` gets `not_pending` and only calls `mark_held`
    (`process_payment.py:159-167`). The ticket stays payable until its own expiry, and paying
    it ends in `refund_due`. This is what happened before the repair, but now only in this
    narrow window.
  - Possible fix: when a hold answers `not_pending`, publish a request to cancel that ticket.
  - The user decides whether this deserves a change.
- **R3 (Low, docs): `docs/architecture.md:65` is stale.** It does not describe decisions 19
  and 20 or the 4xx classification. This is the failed checklist item above.
- **R4 (uncertain, for the verifier): a ticket the customer has already paid but Mercado Pago
  has not yet confirmed.** An owner may mark the order paid by hand while the ticket is still
  `pending` in Mercado Pago. Decision 20 then sends `PUT /v1/payments/{id}`
  `status: cancelled` for that ticket. How Mercado Pago treats a cancel of a ticket paid at
  the store but not yet credited needs checking in the sandbox or the docs. The design expects
  either a refusal (`Ok(False)`) or an `approved` notification that ends `refund_due`. No code
  change unless the sandbox shows otherwise.

**Result:** the five original items are closed. The repair introduced no regressions in
correctness. The checklist fails on stale docs (R3), and R1 is a small code fix, so the status
stays `review`. R2 and R4 need a decision or a verifier check, not necessarily code. If the
main session accepts R1 and R2 as they are, and R3 is fixed in `docs/architecture.md`, nothing
blocks `verify`.

### Re-review after step 9 (reviewer, 2026-10-27)

Diff `27962ee..HEAD` (commits `839bc99`, `a676882`, `82ee253` and `ba44aba`), read together
with "Step 9: re-review repair" in the Repair log.

**Checklist: 14 of 15 passed, 0 failed, 1 not applicable (PR body, no PR yet).**
- `plans-scope`: "Every change is inside the plan" (42 declared, 65 changed). The hot files
  are not touched.
- I ran both suites myself at `ba44aba`, and the main session's runs match:
  - `uv run just check`: green (4296 passed, 5 skipped, 1 xfailed; harness 685 passed).
  - `uv run just test-integration`: green (1205 passed, 5 skipped). This also covers the full
    integration run that the implementer skipped.
- Contracts: only the description of `PendingTicket.expires_at` changed.
  `fragancia_api.main.openapi --check` reports "openapi.json is up to date", and the
  one-line diff in `openapi.json` is the generated one.
- New event and subscriber:
  - `TicketCancelRequested` (`payments.ticket.cancel_requested`, payload `payment_id`) is
    published through the outbox inside the notification's transaction.
  - `CancelTicket` is wired at `payments/module.py:111`, and the event is documented in
    `payments/__init__.py`, the README and `docs/architecture.md`.
  - No migration, and no business rule in the router or the adapters.
- Docs: the previously failed item is fixed. Details are under R3 below.

**R1 to R3**
- **R1: closed.** `GetPendingTickets` (`pending_tickets.py:25-31`) returns
  `min(ticket.expires_at, order.payment_due_at)` per item, on a `model_copy` so the query's
  models are not mutated. Filtering and ordering are unchanged, and the contract description
  says "whichever is earlier".
- **R2: closed.** `_hold` publishes `TicketCancelRequested` only on `not_pending`
  (`process_payment.py:167-170`). It does so before `mark_held`, in the same transaction, so a
  re-delivery publishes nothing more (`held_at`). `not_found` publishes nothing.
  - I traced the race from the re-review. The order's status change commits before
    `close_requested` is published. A concurrent ticket notification takes the order row lock
    in `hold_for_payment`. Then one of two things happens:
    - the hold commits first, so its row is committed before `CloseCheckout` reads
      `pending_cash`;
    - or the hold sees the order no longer pending and publishes the cancel itself.
  - A ticket can therefore no longer slip through both paths. Duplicate cancels from
    `CloseCheckout` and `CancelTicket` are harmless, because the second gets a permanent
    refusal.
- **R3: closed.** The payments row of `docs/architecture.md` now covers decisions 19 and 20,
  the expired-on-arrival rule, the `/payments/pending` cap, `CancelTicket` and the 4xx
  classification. The README and `payments/__init__.py` match it.

**Regressions from step 9: none found.**

Notes, not findings. Neither needs a product-code change:
- **Missing wiring test (for the tester if wanted).** No test asserts that the container
  subscribes `CancelTicket` to `payments.ticket.cancel_requested`. `test_late_ticket_flow.py`
  builds `CancelTicket` by hand. I confirmed the wiring by reading `module.py:111`. If that
  line were removed, the relay would mark the event published with no handler and nothing
  would fail. A container wiring assertion like `test_payments_tickets_wiring.py` would close
  this.
- **Order page between the deadline and the overdue job.** In that window
  `/payments/pending` can list a ticket whose capped `expires_at` is already past. The cancel
  follows at the next overdue run. This is cosmetic and nothing renders the field until
  phase 3.

**Still open for the verifier:** R4 (cancelling a ticket the customer has paid but Mercado Pago
has not yet credited) and the sandbox rows marked NOT CONFIRMED in Test coverage.

**Result:** no findings need code changes, and the checklist has no failures. Status set to
`verify`.

### Review of step 10 (reviewer, 2026-10-28)

Diff `7067270..HEAD` (commits `3b9aecc`, `f69cf21`, `f22313c` and `b476bbd`), read together
with "Step 10: verification repair" in the Repair log and decisions 21 and 22.

**Checklist: 14 of 15 passed, 0 failed, 1 not applicable (PR body, no PR yet).**
- `plans-scope`: "Every change is inside the plan" (43 declared, 69 changed). The hot files
  are not touched.
- `uv run just check`: green (4312 passed, 5 skipped, 1 xfailed; harness 685 passed).
- `uv run just test-integration`: green (1207 passed, 5 skipped), run by the reviewer.
- No contract change and no migration (`openapi.json` untouched).
- Docs: both modules' `__init__.py` files, the README and `docs/architecture.md` describe
  `TICKET_GRACE`, `pay_by` and decision 21.
- New finding `payments-ticket-cancel-refused-with-test-credentials` (V2): it is linted and its
  evidence is concrete.

**Step 10 items**
- **V1: closed in code.** The close body is exactly `{"expires": true, "expiration_date_to": …}`
  (`mercadopago_gateway.py:93`). The sandbox re-run belongs to the verifier.
- **Decision 21:** docs only, which is correct, since the Mercado Pago expiry was already used
  as reported.
- **Decision 22: the order deadline is correct.** `until = expires_at + TICKET_GRACE`, capped at
  `first + TICKET_GRACE` (`process_payment.py:147-150`). That is `min(E, F) + 48 h`, with E the
  ticket's expiry and F the first held ticket's expiry. The reservation follows `until`. The
  stored expiry and the `/payments/pending` filter still use Mercado Pago's expiry, so a ticket
  stops being listed once it can no longer be paid.
- **Implementer note 2 (no fallback for queued events without `pay_by`): accepted.** The branch
  was never deployed, so only a local outbox could hold such a row. It would fail with
  `KeyError` and end in the failed-events panel. No change needed.
- **Implementer note 1 (`pay_by = min(ticket expiry, until)`): not confirmed. See S1.**

**Findings**

- **S1 (Medium, needs a code change): for a later ticket, the email can name a deadline that
  falls inside the credit margin, or exactly at the cancel time.**
  - Code: `process_payment.py:154` sets `pay_by=min(payment.expires_at, until)`, where `until`
    already includes `TICKET_GRACE`.
  - Scenario: the first ticket expires at F, so the order waits until F + 48 h. After that
    hold, the guest gets a new link and issues a second ticket expiring at E2 = F + 30 h.
    - The email says "paga … antes del F + 30 h".
    - If the guest pays at the store at F + 29 h, the payment takes one or two business days
      to be credited.
    - The overdue job cancels the order at F + 48 h. The payment arrives afterwards and ends
      `refund_due`, with no stock.
    - If E2 is later than F + 48 h, the email names F + 48 h itself: the cancel time, with no
      margin at all.
  - Why it matters: decision 22 exists so that the customer is told the ticket's deadline and
    the order then waits 48 more hours for the money to be credited. Deviation 1 removes that
    margin for every capped later ticket.
  - Consistent value: `pay_by = until - TICKET_GRACE`, which is `min(E2, F)`. For a single
    ticket this is still E, so the main path is unchanged.
  - The same applies to `/payments/pending` (`pending_tickets.py:29`). It caps items at
    `order.payment_due_at`, which now includes the margin. The consistent cap is
    `payment_due_at - TICKET_GRACE`; payments owns `TICKET_GRACE`.
  - The single-ticket path, which is the common case, is correct. This affects only orders with
    a second ticket issued after a hold.

**Still open for the verifier:**
- the V1 close in the sandbox;
- decision 22's reservation and overdue timing;
- R4;
- the NOT CONFIRMED sandbox rows.

V2 is tracked by its finding and needs a user decision; it does not block verify.

**Result:** S1 needs a code change, so the status stays `review`. No other regressions from
step 10.

## Repair log

Implementer, 2026-10-27. Step 8 (review repair: M1, L1, L2, L3 and the finding
`payments-cancelled-order-keeps-live-ticket`), decisions 19 and 20. The "Test coverage" and
"Review findings" sections above describe the code before this repair. They are superseded for
the changed behavior below and must be repeated (testing → review → verify).

**What changed**
- **M1 (decision 19).** `PaymentRepository.first_hold_expiry(order_id)` (SQL: `expires_at` of
  the row with `held_at` not null and `expires_at > held_at` (repair deviation 6), ordered by
  `held_at`, `payment_id`, limit 1; in-memory the same). `ProcessPaymentNotification._hold` passes `until = min(payment.expires_at, first)`.
  The current payment is never its own "first" (its `held_at` is still null when it is read).
- **L1.** `Payment.needs_hold(now)` now requires `expires_at > now`. New
  `Payment.expired_on_arrival(now)` (pending cash ticket, not held, `expires_at <= now`): the
  handler logs `payments.ticket_expired_on_arrival` and calls `mark_held(now)`, with no call to
  orders and so no email.
- **L2.** `get_payment` drops a `ticket_url` longer than `TICKET_URL_MAX_LENGTH = 500` (gateway
  constant, matching `String(500)` in `tables.py`) and logs `payments.ticket_url_too_long`
  with the payment id and length.
- **L3.** `close_checkout`: 404 → `Ok(False)` as before; 400, 409 and 422 → `Ok(False)` and
  `payments.checkout_close_refused` (warning, with the status); every other failure → `Err`
  (repair deviation 5).
  `CloseCheckout` then also logs `payments.checkout_missing` for any `Ok(False)` (unchanged
  code path).
- **Finding (decision 20).** `PaymentGateway.cancel_payment(payment_id)`: `PUT
  {api_url}/v1/payments/{id}` with `{"status": "cancelled"}`, same client settings and bearer
  as the other calls; 2xx `Ok(True)`, 400/404/409/422 `Ok(False)` +
  `payments.ticket_cancel_refused`, any other failure / transport / no token `Err` (repair
  deviation 5). `PaymentRepository.pending_cash(order_id)` (status `pending`,
  type in `CASH_TYPES`, any expiry, oldest first). `CloseCheckout` gains a `payments`
  dependency; it reads the checkout and the pending tickets in one transaction, closes the
  preference and the stored checkout when there is one, then cancels each ticket; an `Err`
  raises (the event retries). Rows are not edited. `FakeGateway.cancel_payment` records
  `cancelled` and answers False for `refused_cancels`.
- The finding is `status: planned`, `plan: payments-mercadopago/003`. Docs: `payments/__init__.py`,
  `apps/api/README.md`, and plan 003's row in the initiative README (decisions 19 and 20 were
  already recorded there).

**Deviations of the repair**
1. **`payments/module.py` (not in step 8's file list, but in steps 5 and 6)**: wires the new
   `payments=` argument of `CloseCheckout`, sharing one `SqlPaymentRepository` with
   `ProcessPaymentNotification`. A file-list omission; `plans-scope` is clean.
2. **Tickets are cancelled even when the order has no stored checkout.** Step 8 says "after the
   preference"; the old early return on "no checkout" would have skipped the tickets. Normally
   a ticket implies a checkout, so this changes nothing in practice.
3. **Existing tests adjusted, no coverage added** (the tester's job):
   - `unit/payments/test_payment_tickets_domain.py`: `needs_hold()` → `needs_hold(NOW)`.
   - `unit/payments/test_close_checkout.py` and `integration/payments/test_sql_tickets.py`:
     `CloseCheckout` gets `payments=` (in-memory / SQL).
   - `unit/payments/test_mercadopago_tickets.py`: the "failing status is unavailable" case now
     covers 401/403/408/429/500/502/503. 400 is now `Ok(False)` (repair deviation 5), and 408
     was added.
   - `unit/payments/test_process_payment_tickets.py::test_each_payment_of_the_order_holds_it_once`:
     the second hold's `until` is now the first ticket's expiry (decision 19).
4. **For the reviewer:**
   - (Superseded by repair deviation 5.) As first written, L3 and decision 20 treated every 4xx,
     401 and 429 included, as permanent.
   - **Accepted by the main session as is:** `first_hold_expiry` is read before orders locks the
     order row. Two tickets of the same order processed concurrently can both see no earlier
     hold. Each still passes through the order's `FOR UPDATE`, so the deadline only grows to the
     later of the two (the pre-repair behavior for that race).
   - (Superseded by repair deviation 6.) As first written, a ticket marked held by L1 counted as
     "held" for `first_hold_expiry`.
5. **4xx classification (main-session decision, 2026-10-27).** Only 400, 404, 409 and 422 are
   permanent (`Ok(False)`, logged) for both `close_checkout` and `cancel_payment`. They are
   `PERMANENT_REFUSALS` in `mercadopago_gateway.py`; `close_checkout` keeps its silent 404.
   401, 403, 408, 429, every other 4xx, 5xx and transport errors are `Err`, so the event
   retries and, if it never recovers, shows in the failed-events panel. Docs updated in
   `ports.py`, `payments/__init__.py` and the README.
6. **`first_hold_expiry` counts only holds that could have moved the order (main-session
   decision, 2026-10-27):** `held_at` not null and `expires_at > held_at`, in the SQL adapter
   and the in-memory fake (port docstring updated). An expired-on-arrival ticket therefore never
   caps a later legitimate hold with a past deadline. `not_pending`/`not_found` need no filter,
   because the order is no longer pending.

**Commands**
- `uv run just typecheck`: green (517 files).
- `uv run just test-integration`: 1184 passed, 5 skipped.
- `uv run just check`: green (4196 passed, 5 skipped, 1 xfailed; harness 685 passed; plans OK;
  adapters up to date).
- After repair deviations 5 and 6: `uv run just check` green again (4200 passed, 5 skipped,
  1 xfailed; harness 685 passed). The integration tests of `tests/integration/payments/` and
  `tests/integration/orders/test_payment_hold_flow.py` gave 112 passed. The full
  `test-integration` was not re-run after deviation 6.
- `uv run just plans-scope plans/payments-mercadopago/003-oxxo-spei.md`: "Every change is inside
  the plan" (41 declared, 58 changed).
- No contract change: `fragancia_api.main.openapi --check` says "openapi.json is up to date".
  No migration.
- `uv run just plans-lint`: plans OK (after setting `status: testing`).

### Step 9: re-review repair (R1, R2, R3)

Implementer, 2026-10-27. The test coverage and the re-review above describe the code before
this step. They are superseded for the behavior below and must be repeated (testing → review →
verify). R4 needs no code; it is left to the verifier.

**What changed**
- **R1.** `GetPendingTickets` (`application/queries/pending_tickets.py`) returns each item with
  `expires_at = min(ticket.expires_at, order.payment_due_at)`, through `model_copy`, using the
  order already resolved by `payable`. `PaymentQueries.pending_for_order` and its SQL and
  in-memory adapters are unchanged: they still filter on the ticket's own expiry.
  `PendingTicket.expires_at` now reads "When the ticket stops paying the order: its own expiry
  or the order's payment deadline, whichever is earlier." I ran `uv run just openapi`; the
  one-line description change is in `openapi.json`.
- **R2 (decision 20, late notifications).**
  - New event `TicketCancelRequested` (`payments.ticket.cancel_requested`, payload
    `payment_id`) in `domain/events.py`.
  - In `ProcessPaymentNotification._hold`, a `not_pending` answer publishes it through the
    handler's `EventPublisher`, inside the same transaction, before `mark_held`. `not_found`
    publishes nothing. The `payments.ticket_not_held` log is unchanged.
  - New subscriber `CancelTicket` (`application/handlers/cancel_ticket.py`) calls
    `gateway.cancel_payment(payment_id)`. `Err` raises `payments.gateway_unavailable`, so only
    that event retries. `Ok(False)` is already logged by the gateway.
  - Wired in `payments/module.py` with the shared gateway. The event is listed in
    `payments/__init__.py`.
- **R3.** The payments row of `docs/architecture.md` and the payments section of
  `apps/api/README.md` now describe:
  - the cap at the first ticket (decision 19);
  - the expired-on-arrival rule;
  - the `/payments/pending` expiry cap;
  - ticket cancellation on close and on late notifications (decision 20);
  - the permanent answers (400, 404, 409, 422) and the retried ones (other 4xx, 5xx,
    transport), which end in the failed-events panel if they never recover.

  `payments/__init__.py` also describes the cap and the new event.

**Deviations of step 9**
1. **Existing test adjusted, no coverage added.** In `unit/payments/test_pending_tickets.py`,
   the `query` helper builds the order with `due=timedelta(hours=48)`, as a held order has its
   deadline at its ticket's expiry. With the default 30-minute deadline, R1 capped the
   expected 48-hour expiries, and
   `test_a_live_oxxo_ticket_is_listed_with_its_label_amount_expiry_and_page` and
   `test_a_known_token_answers_200_with_the_items` failed. Nothing tests the cap itself,
   `TicketCancelRequested` or `CancelTicket` yet; that is for the tester.
2. None other. Every file is in step 9's list.

**Commands**
- `uv run just openapi`: wrote `openapi.json` (1 line changed).
- `uv run just check`: green (4277 passed, 5 skipped, 1 xfailed; harness 685 passed).
- Integration tests in `tests/integration/payments/`, `orders/test_payment_hold_flow.py` and
  `orders/test_payment_confirmation_flow.py`: 153 passed. I did not re-run the full
  `test-integration`; there is no schema change.
- `uv run just plans-scope plans/payments-mercadopago/003-oxxo-spei.md`: "Every change is
  inside the plan" (42 declared, 63 changed).

### Step 10: verification repair (V1, V2, decisions 21 and 22)

Implementer, 2026-10-27. The test coverage, the reviews and the first sandbox run above describe
the code before this step. They are superseded for the behavior below and must be repeated
(testing → review → verify).

**What changed**
- **V1.** `MercadoPagoGateway.close_checkout` sends only
  `{"expires": true, "expiration_date_to": at}`.
- **Decision 22.**
  - `TICKET_GRACE = timedelta(hours=48)` in `payments/domain/payment.py`.
  - `ProcessPaymentNotification._hold` sets `until = expires_at + TICKET_GRACE`. The decision 19
    cap is `first_hold_expiry + TICKET_GRACE`.
  - Orders extends the reservation to the same `until` (unchanged facade code), so the overdue
    job waits for the margin.
  - `payments.payments.expires_at` and `/payments/pending` items keep Mercado Pago's expiry.
    That query still caps each item at the order deadline, which now includes the margin.
- **The email shows the ticket's deadline.**
  - New `pay_by: datetime` parameter, after `until`, on:
    - `Order.await_payment(until, pay_by, method, instructions_url, now)`;
    - `OrderPaymentConfirmations.hold_for_payment(order_id, until, pay_by, method, instructions_url)`;
    - payments' `OrderConfirmations.hold` port and its `OrderPaymentsAdapter`, which passes
      both values.
  - `OrderPaymentPending` gains `pay_by`. `payment_instructions_email` takes `pay_by` instead of
    `due_at` and prints it, and `SendPaymentInstructionsEmail` reads `payload["pay_by"]`.
- **Decision 21.** No code change. It is described in the docs.
- **V2.** Finding `plans/findings/payments-ticket-cancel-refused-with-test-credentials.md`
  (status `open`).
- **Docs.** Both `__init__.py` files (orders: the facade signature and the event payload;
  payments: decisions 21 and 22), the README's payments section, and the orders and payments
  rows of `docs/architecture.md`.

**Deviations of step 10**
1. **`pay_by` is `min(ticket expiry, until)`, not always the ticket's own expiry.** For a capped
   later ticket, the ticket's own expiry can fall after the order's deadline. Example: the
   first ticket expires at F, so the order waits until F + 48 h. A link created after the hold
   can issue a ticket expiring later than F + 48 h. Telling the customer to pay before a date
   after the order is cancelled is the email form of R1, so the email shows the earlier of the
   two. For a single ticket, `pay_by` is the ticket's expiry, as the plan says ("antes del" T).
   The reviewer should confirm.
2. **No fallback for queued events without `pay_by`.** `SendPaymentInstructionsEmail` reads
   `payload["pay_by"]`. An `orders.order.payment_pending` row written before this change and
   still in the outbox would raise `KeyError` and retry until it fails. This branch has not
   been deployed, so only local development outboxes could hold such rows.
3. **Tests outside step 10's file list, adjusted so `check` and `test-integration` stay green,
   with no coverage added.** The step 7 file list covers the test directories.
   - `pay_by` passed with the same value as `until`, so the existing assertions hold:
     - `unit/orders/test_order_payment_pending_domain.py`, which also adds `pay_by` to the
       expected payload;
     - `unit/orders/test_hold_for_payment.py`;
     - `integration/orders/test_payment_hold_flow.py` (two calls);
     - `unit/payments/test_payments_tickets_wiring.py`;
     - `unit/orders/test_payment_instructions_email.py` (`pay_by=due_at` in the two event
       builders).
   - The fakes' `hold` signatures gain `pay_by` in `unit/payments/test_process_payment.py` and
     `test_process_payment_tickets.py`. The second still records `until`, and its expected
     `until` values become `EXPIRES + TICKET_GRACE`.
   - Expected `until` values or deadlines become `… + TICKET_GRACE` in
     `unit/payments/test_ticket_hold_repair.py`, `integration/orders/test_payment_hold_flow.py`
     (`test_a_pending_oxxo_payment_holds_the_order_and_stores_the_ticket_once`) and
     `integration/orders/test_payment_hold_repair_flow.py` (two tests).
   - `unit/payments/test_mercadopago_tickets.py`: the expected close body has no
     `date_of_expiration`.
   - Not tested yet: the email printing `pay_by` when it differs from `due_at`, the `min` of
     deviation 1, and the reservation reaching the margin. These are for the tester.

**Commands**
- `uv run just typecheck`: green (524 files).
- `uv run just check`: green (4297 passed, 5 skipped, 1 xfailed; harness 685 passed; plans OK,
  including the new finding).
- `uv run just test-integration`: the first run had 2 failures in
  `test_payment_hold_repair_flow.py`, whose expectations needed the margin. After that fix,
  the file gives 3 passed, and the full run's other 1203 passed (5 skipped).
- `uv run just plans-scope plans/payments-mercadopago/003-oxxo-spei.md`: "Every change is inside
  the plan" (43 declared, 66 changed).
- No contract change (`openapi.json` untouched). No migration.

### Step 11: step 10 review repair (S1)

Implementer, 2026-10-28. This supersedes step 10's deviation 1 (`pay_by = min(ticket expiry,
until)`). The test coverage and reviews of step 10 are superseded for the behavior below and
must be repeated (testing → review → verify).

**What changed**
- **The email's deadline leaves the credit margin.**
  - `ProcessPaymentNotification._hold` now passes `pay_by = until - TICKET_GRACE`. That is the
    earlier of this ticket's expiry and the first ticket's expiry.
  - A single ticket still gets its own expiry. A later ticket is never told a deadline inside
    the margin or at the cancel time.
  - The `_hold` docstring states the rule.
- **`/payments/pending` uses the same rule.**
  - `GetPendingTickets` caps each item at `min(ticket.expires_at, order.payment_due_at -
    TICKET_GRACE)`, importing `TICKET_GRACE` from `domain/payment.py`. The stored expiry is
    unchanged.
  - `PendingTicket.expires_at`'s description now reads: "The last moment to pay the ticket so
    that the order still waits for the payment to be credited: the ticket's own expiry, or the
    order's payment deadline minus the 48-hour credit margin, whichever is earlier."
  - I ran `uv run just openapi`, which changed only that description.
- **Docs.** The rule is described in `payments/__init__.py` (the hold paragraph and the
  `/payments/pending` paragraph) and in the README's payments section.

**Deviations of step 11**
1. **Existing tests adjusted to the new rule, with no coverage added.** They are outside step
   11's file list; step 7's list covers the test directories.
   - `unit/payments/test_pending_tickets.py`: the `query` helper's order deadline is now 96 h
     (the 48-hour ticket plus the margin) instead of 48 h, so the expected expiries are
     unchanged.
   - `unit/payments/test_ticket_late_repair.py`: the `query(due, …)` helper now builds the
     order with `due + TICKET_GRACE`. Its `due` keeps meaning "the cap", so every expectation is
     unchanged. The module docstring was rewrapped.
   - `unit/payments/test_ticket_grace.py`:
     - a later ticket's `pay_by` is now `EXPIRES` in both later-ticket tests. The second is
       renamed `test_a_later_ticket_expiring_inside_the_margin_tells_the_first_expiry`.
     - With a 96-hour deadline, the query caps at 48 h: one test is renamed
       `test_a_ticket_after_the_order_deadline_less_the_margin_is_capped_there`, and the http
       case `(100, 96)` became `(100, 48)`.
     - The module docstring was updated.
   - `integration/orders/test_ticket_grace_flow.py`: the later ticket's `pay_by` is `first`
     (was `cap`), and the docstring was updated.
2. **Note for the reviewer (literal plan).** `/payments/pending` now caps at
   `payment_due_at - TICKET_GRACE` for every pending order, including one whose ticket
   notification has not been processed yet. Such an order still has its checkout deadline (for
   example 30 minutes), so the listed expiry would be in the past. This window is short: the
   webhook normally arrives within seconds of issuing the ticket.

**Commands**
- `uv run just check`: green (4312 passed, 5 skipped, 1 xfailed; harness 685 passed).
- `uv run just openapi`, then `fragancia_api.main.openapi --check`: up to date.
- Integration tests, 158 passed in total:
  - `tests/integration/payments/`, `orders/test_payment_hold_flow.py`,
    `orders/test_payment_hold_repair_flow.py` and `orders/test_payment_confirmation_flow.py`:
    156 passed;
  - `orders/test_ticket_grace_flow.py`: 2 passed after the adjustment.

  I did not re-run the full `test-integration`. There is no schema or persistence change, and
  a grep found no other integration test using `pay_by`, `GetPendingTickets` or
  `/payments/pending`.
- `uv run just plans-scope plans/payments-mercadopago/003-oxxo-spei.md`: "Every change is inside
  the plan".

## Verification

**First sandbox run (2026-10-27, main session, at 57f8ed8; superseded for the behavior step 10
changes).**

Setup:
- The real Mercado Pago sandbox, through the `cloudflared` tunnel to the API on :8100;
- the worker restarted on the new code;
- the test-mode webhook secret;
- QA orders `LFI-1035` to `LFI-1037`.

Suites at 7f81bb1:
- `uv run just check`: 4296 passed;
- `uv run just test-integration`: 1205 passed;
- the wiring test at 57f8ed8 also passed.

- [x] **Preference.** Read back from Mercado Pago:
  - `excluded_payment_types [{"id":"atm"}]` and `installments 1`;
  - `expiration_date_to` equal to the order's deadline;
  - `date_of_expiration` 48 h after the link (`2026-10-29T21:34:39.566Z`).
  The checkout offered OXXO and "Transferencia SPEI".
- [x] **OXXO ticket (`LFI-1035`).**
  - Payment `185367296137`: `pending`, `ticket`/`oxxo`, its ticket URL stored, `held_at` set.
  - The order's `payment_due_at` and the reservation's `expires_at` both moved to the ticket's
    expiry, `2026-10-30T05:59:59Z`.
  - Mailpit: "Tu ficha de pago del pedido LFI-1035", with "$2,079.50 MXN con efectivo en
    tienda antes del 30/10/2026 a las 00:59" and the ticket link.
- [x] **SPEI (`LFI-1036`).** Payment `186412811742`: `bank_transfer`/`clabe`, held to
  `2026-10-29T21:34:39Z`, and the email says "transferencia SPEI".
- [x] **`/payments/pending`.**
  - `LFI-1035`'s token returned the OXXO ticket.
  - An unknown token got 404 `PAYMENTS_ORDER_NOT_FOUND`.
- [x] **A card payment while a ticket is pending (`LFI-1037`).**
  - The old link had expired with the original 30 minutes.
  - `POST /payments/checkout` gave a new link, because the deadline had moved.
  - Paid with a card: `186413391800` approved, `applied`, and the order `paid`.
- [ ] **V1: closing the link failed.** The close event's preference update answered
  `400 invalid date of expiration` (logged `payments.checkout_close_refused`).
  - A probe of the same update without `date_of_expiration` answered 200, and Mercado Pago then
    showed `expiration_date_to` = the close time. → step 10.
- [ ] **V2: cancelling the ticket failed.** `PUT /v1/payments/186412901784`
  `{"status":"cancelled"}` answered `401 Unauthorized use of live credentials`.
  - The close event retries (attempt 5 at the time).
  - The ticket stays `pending_waiting_payment` in Mercado Pago. → finding, step 10.
- **Decision 21.** Mercado Pago set the OXXO ticket's expiry to the end of the day in Mexico
  City (`05:59:59Z`), not the 48 h sent. SPEI kept the 48 h. → decisions 21 and 22, step 10.
- **NOT VERIFIABLE in the sandbox:**
  - paying a ticket at the store (the approved path is covered by integration tests);
  - R4.
