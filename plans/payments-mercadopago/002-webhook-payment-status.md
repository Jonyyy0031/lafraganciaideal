---
status: testing
module: payments
min_implementer: high
depends_on: ["001"]
---

# 002 — Webhook and payment status

## Context

Plan 001 sends the guest to Mercado Pago, but nothing confirms the payment: the order stays
`pending_payment` even after the card is approved (verified 2026-10-24 in the sandbox). This plan
adds the verified webhook (ADR 0004: "Payment confirmation comes only from verified webhooks")
and what each payment outcome does to the order. It first fixes the finding that blocks the
sandbox in development.

**What exists today:**
- **The finding.** `MercadoPagoGateway._body` always sends `"auto_return": "approved"`
  (`apps/api/src/fragancia_api/modules/payments/infrastructure/mercadopago_gateway.py:90-95`), and
  Mercado Pago refuses it unless the success URL is `https`
  (`plans/findings/payments-auto-return-needs-https-back-url.md`). Development's
  `STOREFRONT_URL` is `http://localhost:4200` (`apps/api/src/fragancia_api/config.py:75`).
- **The payments module** (plan 001): the `PaymentGateway` port with `create_checkout` only
  (`modules/payments/application/ports.py:68-73`), the adapter that opens one `httpx` client per
  call with `trust_env=False` and maps every failure to `PaymentGatewayUnavailable`
  (`infrastructure/mercadopago_gateway.py:33-59`), one table `payments.checkouts`
  (`infrastructure/tables.py:8-17`), one public route (`http/router.py:9-32`) and the wiring
  (`module.py:15-37`). Errors: `modules/payments/domain/errors.py:1-23`. The SQL repository
  shape to copy is `infrastructure/sql_checkout_repository.py:11-45` (upsert with
  `on_conflict_do_update`).
- **Marking an order paid.** `MarkOrderPaid` (`modules/orders/application/commands/lifecycle.py:84-93`)
  runs `OrderMove._apply` (`lifecycle.py:56-81`): lock the order row, `order.mark_paid(note,
  actor, now)` (`modules/orders/domain/order.py:345-356`, which records `OrderPaid`), then
  `StockHolds.commit(reservation_id)` (`modules/orders/application/ports.py:66-69`), save and
  publish. The order's events already send the customer's "pagado" email and the owners' email
  (`modules/orders/module.py:198-223`).
- **Inventory's commit** is idempotent and accepts a late commit (an expired or released
  reservation) when the units are still available; otherwise it refuses with
  `INVENTORY_STOCK_UNAVAILABLE` or `INVENTORY_STOCK_INSUFFICIENT` and changes nothing
  (`modules/inventory/application/commands/reservations.py:170-178`).
- **Cancelling an unpaid order** is `order.cancel_unpaid(reason, actor, now)`
  (`order.py:410-420`) plus `StockHolds.release` (`ports.py:71-73`), as the overdue job does
  (`lifecycle.py:221-252`). The actor `SYSTEM_ACTOR` (`order.py:44`) makes the customer's
  cancellation email say "porque no recibimos el pago a tiempo"
  (`modules/orders/application/handlers/order_emails.py:35-39`, `order.py:91-97`).
- **The overdue job's note.** It stops at an order whose release is refused, and that "can"
  happen once a webhook commits reservations (`lifecycle.py:189-208`, initiative README
  "Dependency notes"). It cannot happen here, because this plan commits the reservation and
  moves the order to paid in one transaction, like `MarkOrderPaid`.
- **The repository** loads an order for update only by number
  (`modules/orders/domain/repositories.py:36-39`; SQL at
  `modules/orders/infrastructure/sql_order_repository.py:113-122`, `FOR NO KEY UPDATE`;
  in-memory at `modules/orders/infrastructure/in_memory.py:107-109`). Mercado Pago gives back
  the order id (`external_reference`), so an id lookup is needed.
- **Outbox.** Subscribers run in the worker inside the relay's transaction
  (`apps/api/src/fragancia_api/shared/infrastructure/outbox.py:114-145`); a handler that raises
  rolls back and is retried with backoff for about 24 h (`outbox.py:41-45`). A nested
  `transactions.run` joins the outer transaction and does not roll back on `Err`
  (`shared/infrastructure/database.py:64-66`). The orders email handlers already make network
  calls there (`order_emails.py:1-9`).
- **Owners' emails** use identity's `OwnerDirectory` facade through an adapter
  (`modules/orders/infrastructure/identity_owners.py:1-15`); the handler to imitate is
  `SendPaidOrderToOwners` (`order_emails.py:256-288`).
- **Error categories:** `UnauthenticatedError` (401) and `ServiceUnavailableError` (503)
  exist (`apps/api/src/fragancia_api/shared/kernel/errors.py:43-55`).
- **Settings:** `mercadopago_*` and the production checks (`config.py:65-69`, `config.py:107-117`).
- **Mercado Pago**, checked on 2026-10-24 against the official SDK sources
  (`mercadopago/sdk-nodejs`):
  - Webhook signature (`src/utils/webhook/index.ts`): header `x-signature: ts=<seconds>,v1=<hex>`;
    the manifest is `id:<data.id>;request-id:<x-request-id>;ts:<ts>;`, each of the first two
    pairs only when present, with `data.id` taken from the **query string**; HMAC-SHA256 hex
    with the application's webhook secret, compared in constant time. The SDK's timestamp
    tolerance is optional and off by default.
  - Payment (`src/clients/payment/commonTypes.ts`, `PaymentResponse`):
    `GET https://api.mercadopago.com/v1/payments/{id}` answers `id` (number), `status`,
    `status_detail`, `external_reference`, `transaction_amount` (pesos, a number),
    `currency_id`, `date_approved`, `payment_type_id` and `live_mode`.
  - Statuses: `approved`, `pending`, `in_process`, `authorized`, `rejected`, `cancelled`,
    `refunded`, `charged_back` (plan 001 sources).

**Approach.** Two ways were compared:
- *Process in the request*: the route verifies, fetches the payment and moves the order. Simple,
  but a Mercado Pago or database hiccup becomes a webhook failure that only Mercado Pago retries,
  on its own schedule.
- *Accept and queue* (chosen): the route only verifies the signature and stores an outbox event,
  answering in milliseconds. The worker fetches the payment from Mercado Pago (the notification
  body is never trusted, only the payment id) and applies it. Our retries (24 h, visible in the
  failed-events panel) cover outages. It follows ADR 0006 and how orders already sends email.

Processing is idempotent by payment id, not by notification id: each delivery fetches the
payment's current state and converges, so a duplicate or replayed notification changes nothing.
A replay can only make us re-read Mercado Pago, which is why no timestamp tolerance is needed.

The webhook URL is configured in the Mercado Pago panel ("Webhooks", event "Pagos"), which also
issues the secret; preferences carry no `notification_url`. A development tunnel is set there.

**Outcomes** (decisions 2, 3 and 8 to 12):

| Payment status | Order state when processed | Result |
| --- | --- | --- |
| `approved`, amount and MXN match | `pending_payment`, units sellable (even past the deadline) | order → `paid` (commit + paid in one transaction); payment `applied` |
| `approved`, amount and MXN match | `pending_payment`, units gone | order cancelled (system), reservation released; payment `refund_due`; owners emailed |
| `approved`, amount or currency differ | `pending_payment` | nothing moves; payment `amount_mismatch`; owners emailed |
| `approved` | already paid by another payment or by hand, or later states | payment `refund_due` (recorded only) |
| `approved` | `cancelled` | payment `refund_due` (recorded only) |
| any other status | any | status recorded; order unchanged |

Plan 004 refunds `refund_due` payments and tells the customer.

**Imitated files:** the facade shape of `modules/orders/application/queries/order_payments.py:11-24`
and its adapter `modules/payments/infrastructure/order_payments.py`; the move shape of
`lifecycle.py:37-93`; the owners' email of `order_emails.py:256-288`; the gateway of
`mercadopago_gateway.py`; the migration `apps/api/migrations/versions/0023_payments_checkouts.py`.

## Out of scope

- Refunds of any kind, and telling the customer about one (plan 004).
- OXXO, SPEI and pending tickets extending the window (plan 003).
- Reacting to `refunded` or `charged_back` after an order is paid (only the status is stored).
- A panel or API listing payments (plan 004 or the web phase).
- The storefront's return page and showing payment state to the guest (decision 12, phase 3).
- Merchant-order (`topic=merchant_order`) and legacy IPN notifications: answered 200 and ignored.
- Changing the overdue job, the customer email copy or `cancelled_by`.
- The open finding `platform-client-ip-behind-proxy`.

## Dependencies

- `001` (done): the payments module, `PaymentGateway`, `MercadoPagoGateway`, `OrderPaymentsAdapter`
  and the wiring.

## Steps

1. **Fix: `auto_return` only with an https back URL (decision 8)**
   - Files: `apps/api/src/fragancia_api/modules/payments/infrastructure/mercadopago_gateway.py` (modify), `plans/findings/payments-auto-return-needs-https-back-url.md` (modify)
   - Do: in `_body`, add `"auto_return": "approved"` only when `request.back_url` starts with
     `https://`; keep the three back URLs always. Comment why (Mercado Pago answers
     `400 invalid_auto_return` otherwise). Set the finding's `status: planned` and
     `plan: payments-mercadopago/002`.
   - Observable result: with `STOREFRONT_URL=http://localhost:4200` the body has no
     `auto_return`; with an `https` URL it has `"auto_return": "approved"`.

2. **Orders: load by id, and the `OrderPaymentConfirmations` facade**
   - Files: `apps/api/src/fragancia_api/modules/orders/domain/repositories.py` (modify), `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_order_repository.py` (modify), `apps/api/src/fragancia_api/modules/orders/infrastructure/in_memory.py` (modify), `apps/api/src/fragancia_api/modules/orders/application/commands/confirm_payment.py` (create), `apps/api/src/fragancia_api/modules/orders/module.py` (modify), `apps/api/src/fragancia_api/modules/orders/__init__.py` (modify)
   - Do:
     - `OrderRepository.get_by_id_for_update(order_id: UUID) -> Order | None`, same lock as
       `get_by_number_for_update` (SQL: `.where(orders.c.id == order_id).with_for_update(key_share=True)`;
       in-memory: find by id, `deepcopy`).
     - In `confirm_payment.py`: a `StrEnum` `PaymentConfirmationOutcome` with `paid`,
       `not_pending`, `amount_mismatch`, `stock_gone`, `not_found`; a frozen dataclass
       `PaymentConfirmation(outcome, number: int | None, status: str | None)`; constants
       `PAID_NOTE = "Pagado con Mercado Pago (pago {payment_id})"` and
       `STOCK_GONE_REASON = "Pago recibido sin existencias (pago {payment_id})"`.
     - `class OrderPaymentConfirmations(OrderMove)` with
       `async def confirm(self, order_id: UUID, payment_id: str, amount_cents: int, currency: str) -> Result[PaymentConfirmation, DomainError]`,
       inside one `self._transactions.run` (it joins the caller's):
       1. `get_by_id_for_update`; None → `Ok(not_found)`.
       2. Status not `pending_payment` → `Ok(not_pending, number, status)`.
       3. `currency != "MXN"` or `amount_cents != order.total_cents` → `Ok(amount_mismatch, number)`;
          nothing changes.
       4. With a reservation, `self._stocks.commit(reservation_id)`. On `Err` whose `code` is
          `INVENTORY_STOCK_UNAVAILABLE` or `INVENTORY_STOCK_INSUFFICIENT`:
          `order.cancel_unpaid(STOCK_GONE_REASON…, SYSTEM_ACTOR, now)`, then
          `self._stocks.release(reservation_id)`, save, publish → `Ok(stock_gone, number)`.
          Any other `Err` from commit, cancel or release → return that `Err`.
       5. Otherwise `order.mark_paid(PAID_NOTE…, SYSTEM_ACTOR, now)`, save, publish →
          `Ok(paid, number)`.
       Docstring: the facade other modules (payments) use to apply an approved payment; the
       commit and the move happen in one transaction, so a committed reservation never sits on a
       pending order (initiative README, "Dependency notes").
     - Wire it in `module.py` with the existing `move(...)` helper
       (`services.add(OrderPaymentConfirmations, move(OrderPaymentConfirmations))`).
     - Export `OrderPaymentConfirmations`, `PaymentConfirmation`, `PaymentConfirmationOutcome`
       from `orders/__init__.py`, and describe it under "Public API for other modules".
   - Observable result: unit tests of the facade over the in-memory adapters reach every
     outcome; `uv run just arch` green.

3. **Payments domain: payment, signature, events and errors**
   - Files: `apps/api/src/fragancia_api/modules/payments/domain/payment.py` (create), `apps/api/src/fragancia_api/modules/payments/domain/webhook_signature.py` (create), `apps/api/src/fragancia_api/modules/payments/domain/events.py` (create), `apps/api/src/fragancia_api/modules/payments/domain/errors.py` (modify), `apps/api/src/fragancia_api/modules/payments/domain/repositories.py` (modify)
   - Do:
     - `payment.py`: `StrEnum PaymentOutcome` (`applied`, `refund_due`, `amount_mismatch`); a
       dataclass `Payment(payment_id: str, order_id: UUID | None, status: str, status_detail: str | None, amount_cents: int, currency: str, payment_type: str | None, approved_at: datetime | None, outcome: PaymentOutcome | None, created_at: datetime, updated_at: datetime)`
       with `refresh(status, status_detail, approved_at, now)` (updates the reported fields) and
       `settle(outcome, now)` (raises `ValueError` if `outcome` is already set). `APPROVED = "approved"`.
       A helper `amount_to_cents(value: Decimal | float | int) -> int` that rounds half-up to
       cents through `Decimal(str(value))`.
     - `webhook_signature.py`: pure
       `def signature_valid(secret: str, x_signature: str | None, x_request_id: str | None, data_id: str | None) -> bool`
       exactly as the SDK's `validate` without tolerance: parse `ts` and `v1` from the
       comma-separated `key=value` parts (keys trimmed and lower-cased); `False` when the header
       is missing or empty, `ts` is missing or not digits, or `v1` is missing; build the
       manifest; `hmac.compare_digest` of the hex digests.
     - `events.py`: `PaymentNotificationReceived` (`payments.notification.received`,
       `payment_id: str`) and `PaymentFlagged` (`payments.payment.flagged`, `payment_id: str`,
       `number: int`, `reason: str` (`amount_mismatch` or `stock_gone`), `amount_cents: int`,
       `currency: str`), shaped like `OrderPaid` (`order.py:218-225`).
     - `errors.py`: `PaymentWebhookSignatureInvalid(UnauthenticatedError)`, code
       `PAYMENTS_WEBHOOK_SIGNATURE_INVALID`, message "Firma inválida".
     - `repositories.py`: `PaymentRepository` with `get_for_update(payment_id) -> Payment | None`
       (`FOR UPDATE`) and `save(payment)` (upsert by `payment_id`).
   - Observable result: domain tests pass, including the SDK manifest rules (with and without
     `x-request-id`), a `v2`-only header, and a known HMAC vector computed in the test.

4. **Payments application: receive and process**
   - Files: `apps/api/src/fragancia_api/modules/payments/application/ports.py` (modify), `apps/api/src/fragancia_api/modules/payments/application/commands/receive_notification.py` (create), `apps/api/src/fragancia_api/modules/payments/application/handlers/__init__.py` (create), `apps/api/src/fragancia_api/modules/payments/application/handlers/process_payment.py` (create), `apps/api/src/fragancia_api/modules/payments/application/handlers/flagged_payment_email.py` (create)
   - Do:
     - Ports: `GatewayPayment` dataclass (`payment_id: str`, `status`, `status_detail`,
       `external_reference: str | None`, `amount_cents: int`, `currency: str`,
       `payment_type: str | None`, `approved_at: datetime | None`);
       `PaymentGateway.get_payment(payment_id) -> Result[GatewayPayment | None, PaymentGatewayUnavailable]`
       (None = Mercado Pago answered 404). `OrderConfirmations` protocol (payments' copy of the
       facade): `confirm(order_id, payment_id, amount_cents, currency) -> Result[Confirmation, DomainError]`
       with payments' own `Confirmation(outcome: str, number: int | None)`. `OwnerContacts`
       protocol: `active_owners() -> list[OwnerContact(name, email)]`.
     - `ReceivePaymentNotification.execute(x_signature, x_request_id, data_id, kind) -> Result[bool, DomainError]`:
       no secret configured → `Err(PaymentGatewayUnavailable())`; signature invalid →
       `Err(PaymentWebhookSignatureInvalid())` (log `payments.webhook_signature_invalid` with
       the request id only); `kind != "payment"` or `data_id` not 1–20 digits → `Ok(False)`
       (log `payments.webhook_ignored`); otherwise publish `PaymentNotificationReceived` inside
       `transactions.run` → `Ok(True)`.
     - `ProcessPaymentNotification` (subscriber of `payments.notification.received`):
       1. `get_payment`; `Err` → raise `RuntimeError("payments.gateway_unavailable")` so the
          relay retries; `None` → log `payments.payment_not_found` and return (e.g. the
          panel's test notification).
       2. `external_reference` not a UUID → log `payments.payment_unmatched`, return.
       3. In `transactions.run` (joins the relay's): `get_for_update`; create or `refresh`
          the row.
       4. If the status is `approved` and `outcome` is None: call `confirm`. Map:
          `paid` → `applied`; `not_pending` → `refund_due`; `stock_gone` → `refund_due` and
          publish `PaymentFlagged(reason="stock_gone")`; `amount_mismatch` →
          `amount_mismatch` and publish `PaymentFlagged(reason="amount_mismatch")`;
          `not_found` → log `payments.payment_unmatched`, keep `outcome` None. An `Err` from
          `confirm` → raise (the whole relay transaction rolls back and retries).
       5. Save. A payment whose `outcome` is set is never confirmed again.
     - `SendFlaggedPaymentToOwners` (subscriber of `payments.payment.flagged`): one plain-text
       email per active owner, like `SendPaidOrderToOwners`; log and skip when there are no
       owners; `EmailRejected` logged and skipped. Copy:
       - subject `Pago a revisar: LFI-{number}`;
       - body `Hola {name}:\n\nMercado Pago aprobó el pago {payment_id} por {money} del pedido LFI-{number}, pero {reason}.\n\nRevísalo en Mercado Pago; el reembolso automático llegará en una versión próxima.\n`
         with `{reason}` = `el monto no coincide con el total del pedido, así que el pedido sigue pendiente`
         or `llegó cuando ya no había existencias, así que el pedido se canceló`;
         money as `$1,234.50 MXN`.
   - Observable result: application tests over fakes cover every row of the outcome table,
     idempotent redelivery, and the raise paths.

5. **Infrastructure: gateway fetch, payments table, migration, adapters and settings**
   - Files: `apps/api/src/fragancia_api/modules/payments/infrastructure/mercadopago_gateway.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/tables.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/sql_payment_repository.py` (create), `apps/api/src/fragancia_api/modules/payments/infrastructure/order_payments.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/identity_owners.py` (create), `apps/api/src/fragancia_api/modules/payments/infrastructure/in_memory.py` (modify), `apps/api/migrations/versions/0024_payments_payments.py` (create), `apps/api/src/fragancia_api/config.py` (modify), `apps/api/.env.example` (modify), `apps/api/tests/unit/test_settings.py` (modify), `apps/api/tests/unit/test_http_platform.py` (modify), `apps/api/tests/unit/orders/test_cart_wiring.py` (modify) (the three tests added at implementation, Deviation 1)
   - Do:
     - `get_payment`: `GET {api_url}/v1/payments/{id}` with the same client settings and bearer
       header; 404 → `Ok(None)`; other non-2xx, transport errors or a body without `id`,
       `status`, `transaction_amount` or `currency_id` → log `payments.gateway_error` and
       `Err(PaymentGatewayUnavailable())`. `transaction_amount` → `amount_to_cents`;
       `date_approved` parsed with `datetime.fromisoformat` (None when absent). Never log the token.
     - Table `payments.payments`: `payment_id String(32)` PK, `order_id Uuid` nullable (no FK;
       index `ix_payments_order_id`), `status String(32)`, `status_detail String(100)` nullable,
       `amount_cents BigInteger`, `currency String(3)`, `payment_type String(32)` nullable,
       `approved_at`, `outcome String(20)` nullable, `created_at`, `updated_at` (timestamps with
       time zone). Migration `0024` creates it and drops it on downgrade.
     - `SqlPaymentRepository` like `SqlCheckoutRepository` (`select … with_for_update()`,
       `insert … on_conflict_do_update`).
     - `OrderPaymentsAdapter` also implements `OrderConfirmations` over
       `OrderPaymentConfirmations`, mapping the outcome by value.
     - `IdentityOwners` over identity's `OwnerDirectory`, like
       `modules/orders/infrastructure/identity_owners.py:7-15`.
     - In-memory `PaymentRepository` and a fake gateway `get_payment` for tests.
     - Settings: `mercadopago_webhook_secret: SecretStr | None = None`; in production it is
       required (`MERCADOPAGO_WEBHOOK_SECRET is required in production`). `.env.example` gets
       `MERCADOPAGO_WEBHOOK_SECRET=` with a comment: the secret from the panel's "Webhooks"
       page, where the URL `{public API}/api/v1/payments/webhooks/mercadopago` and the event
       "Pagos" are set.
   - Observable result: `uv run just db-migrate` applies `0024`; integration tests of the
     repository and the migration round trip pass; gateway tests over `httpx.MockTransport`.

6. **HTTP, wiring and docs**
   - Files: `apps/api/src/fragancia_api/modules/payments/http/router.py` (modify), `apps/api/src/fragancia_api/modules/payments/contracts.py` (modify), `apps/api/src/fragancia_api/modules/payments/module.py` (modify), `apps/api/src/fragancia_api/modules/payments/__init__.py` (modify), `apps/api/.importlinter` (modify), `apps/api/openapi.json` (modify), `apps/api/README.md` (modify), `plans/payments-mercadopago/README.md` (modify)
   - Do:
     - `POST /api/v1/payments/webhooks/mercadopago` on the public router: query
       `data.id` (alias) and `type`, headers `x-signature` and `x-request-id`; the body is not
       read. Answers 200 `WebhookAck {"received": true}` for accepted and ignored
       notifications; 401 `PAYMENTS_WEBHOOK_SIGNATURE_INVALID`; 503
       `PAYMENTS_GATEWAY_UNAVAILABLE` when no secret is configured.
     - `module.py`: build `ReceivePaymentNotification` with the secret; subscribe
       `ProcessPaymentNotification` and `SendFlaggedPaymentToOwners`; build `IdentityOwners`
       from `services.get(OwnerDirectory)`.
     - `.importlinter`: `payments.infrastructure.identity_owners -> identity` and
       `payments.module -> identity`.
     - `payments/__init__.py`: list both events and their payloads.
     - `uv run just openapi`. README: the webhook route, the new setting, and the tunnel setup
       for development. Initiative README: move the "Dependency notes" overdue item to resolved
       by this plan.
   - Observable result: `uv run just check` green; the route is in `openapi.json`.

## Acceptance criteria

- [ ] With `STOREFRONT_URL=http://localhost:4200`, `POST /api/v1/payments/checkout` answers 200
      and a sandbox URL against the real Mercado Pago sandbox.
- [ ] A webhook with a wrong or missing `x-signature` answers 401
      `PAYMENTS_WEBHOOK_SIGNATURE_INVALID` and stores no outbox row.
- [ ] A correctly signed `type=payment` webhook answers 200 and stores one
      `payments.notification.received` outbox row; `type=merchant_order` answers 200 with none.
- [ ] Paying an order in the sandbox with an `APRO` card moves it to `paid` within a minute of
      the notification: the reservation is `committed`, history shows "Pagado con Mercado Pago
      (pago …)", `payments.payments` has the row with `outcome = applied`, and Mailpit has the
      customer's and the owners' paid emails.
- [ ] Redelivering the same notification changes nothing and sends no email.
- [ ] An `OTHE` (rejected) payment leaves the order `pending_payment`, stores the status, and the
      guest can request the link again.
- [ ] An approved payment for a cancelled order, or a second one for a paid order, stores
      `outcome = refund_due` and leaves the order as it was.
- [ ] An approved payment past the deadline with units available marks the order paid; with the
      units gone, the order is cancelled, the payment is `refund_due`, and the owners get "Pago a
      revisar".
- [ ] An approved payment with a different amount leaves the order pending, stores
      `amount_mismatch`, and the owners get "Pago a revisar".
- [ ] Mercado Pago down while processing: the outbox row is retried, nothing half-applied.
- [ ] Production refuses to start without `MERCADOPAGO_WEBHOOK_SECRET`.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | signature rules and vectors; `Payment.settle` once; `amount_to_cents` rounding |
| application | yes     | every outcome row; idempotent redelivery; raise on gateway/confirm errors; receive: ignored kinds, bad ids, no secret; facade outcomes including stock gone and amount mismatch; flagged email copy and no-owner path |
| http        | yes     | webhook 200/401/503, alias `data.id`, body ignored; checkout body without `auto_return` on http |
| integration | yes     | migration 0024 round trip; `SqlPaymentRepository`; `get_by_id_for_update` lock; facade + inventory commit/release in one transaction against PostgreSQL |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

Implemented 2026-10-24, steps 6/6. Citations spot-checked (gateway `_body`, `OrderMove._apply`,
`get_by_number_for_update` SQL and in-memory, `SendPaidOrderToOwners`, outbox relay, nested
`transactions.run`, `config.py` checks): all matched.

1. **Out-of-scope test fixtures (scope).** The new production check
   (`MERCADOPAGO_WEBHOOK_SECRET is required in production`) broke 11 existing unit tests that
   build production settings without it. Each got only the extra
   `mercadopago_webhook_secret="webhook-secret"` argument (no assertion changed):
   `apps/api/tests/unit/test_settings.py` (`PROD_MP` and the sandbox test),
   `apps/api/tests/unit/test_http_platform.py`, `apps/api/tests/unit/orders/test_cart_wiring.py`.
   `plans-scope` lists these three files as out of scope; the main session decides whether to
   accept them here or move them to the testing phase.
2. **Flagged email money (cosmetic).** The plan writes the money as `$1,234.50 MXN`; the
   handler prints the payment's own `currency` (`$1,234.50 {currency}`), so a currency mismatch
   shows the currency Mercado Pago charged in. For MXN the text is exactly the plan's.
3. **Log key (cosmetic).** `payments.webhook_signature_invalid` and `payments.webhook_ignored`
   log Mercado Pago's id as `x_request_id`, not `request_id`, which would shadow the platform's
   own request-id log field.
4. **Signature comparison (cosmetic).** `hmac.compare_digest` compares the hex digests as
   bytes, so a non-ASCII `v1` is a mismatch (401) instead of a `TypeError` (500).
5. **Small additions not named in the plan (cosmetic):** `confirm_payment.CURRENCY = "MXN"`;
   `PaymentConfirmation.number`/`status` default to None; `OrderPaymentsAdapter` now takes both
   facades in its constructor; `WebhookAck.received` defaults to true; the in-memory
   `FakeGateway` takes `payments` and records `fetched`; `InMemoryPayments` is the in-memory
   `PaymentRepository`.

Not done here: the layered tests (testing phase). Only a throwaway smoke in the session
scratchpad exercised the facade outcomes, the handler (applied once, redelivery no-op, raise on
gateway failure), the receive command, the webhook route (401/503/200 ignored) and the gateway
fetch over `httpx.MockTransport`. The migration was applied to the development and test
databases; its downgrade was not run (development database must not be downgraded).

## Test coverage

## Review findings

## Verification
