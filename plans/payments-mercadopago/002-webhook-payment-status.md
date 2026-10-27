---
status: done
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
   - Files: `apps/api/tests/unit/payments/` (modify), `apps/api/tests/integration/payments/` (modify), `apps/api/tests/unit/orders/test_confirm_payment.py` (create), `apps/api/tests/integration/orders/test_payment_confirmation_flow.py` (create) (the layered tests, written in the testing phase)
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

7. **Review repair (added 2026-10-26 after the first review: H1, L1, L2, L3)**
   - Files: `apps/api/src/fragancia_api/modules/payments/domain/repositories.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/sql_payment_repository.py` (modify), `apps/api/src/fragancia_api/modules/payments/infrastructure/in_memory.py` (modify), `apps/api/src/fragancia_api/modules/payments/application/handlers/process_payment.py` (modify), `apps/api/src/fragancia_api/modules/payments/application/commands/receive_notification.py` (modify), `apps/api/src/fragancia_api/modules/orders/domain/order.py` (modify), `apps/api/src/fragancia_api/modules/orders/application/commands/confirm_payment.py` (modify), `apps/api/src/fragancia_api/modules/orders/application/handlers/order_emails.py` (modify), `docs/architecture.md` (modify)
   - Files: `apps/api/src/fragancia_api/modules/orders/__init__.py` (modify), `apps/api/src/fragancia_api/modules/orders/contracts.py` (modify), `apps/api/src/fragancia_api/modules/orders/infrastructure/tables.py` (modify), `apps/api/openapi.json` (modify) (re-review R-L1 and the actor finding, docs only), `apps/api/tests/unit/orders/test_order_emails.py` (modify), `apps/api/tests/unit/orders/test_order_domain.py` (modify), `apps/api/tests/integration/orders/test_payment_confirmation_flow.py` (modify) (regression tests, written in the testing phase)
   - Do:
     - **H1 (the row lock must exist before the read).** Replace
       `PaymentRepository.get_for_update` with `claim(payment: Payment) -> Payment`: insert the
       given row with `on_conflict_do_nothing(index_elements=[payments.c.payment_id])`, then
       `select … with_for_update()` and return the stored row (the existing one when it was
       already there, so its `outcome` is kept). In `ProcessPaymentNotification._stored`, build
       the fresh `Payment` from the fetched one (as today's `None` branch does), `claim` it,
       then `refresh` it with the fetched fields. A second delivery of the same payment now
       waits on the row lock and sees `outcome = applied`, so it never confirms again.
       `save` stays the upsert. In-memory: `claim` stores the row when absent and returns a
       copy of the stored one.
     - **L2 (decision 13).** In `order.py` add `PAYMENTS_ACTOR = "payments"`; `cancelled_by`
       maps it to `"system"` (the customer page contract keeps its three values). The
       stock-gone cancel in `confirm_payment.py` uses `PAYMENTS_ACTOR` instead of
       `SYSTEM_ACTOR`. In `order_emails.py`, when `cancelled_actor(order) == PAYMENTS_ACTOR`
       the cancellation email says `Tu pedido {number} fue cancelado porque tu pago llegó cuando
       ya no había existencias.\nTe reembolsaremos el pago completo.\n`; every other case is
       unchanged.
     - **L3.** In `ReceivePaymentNotification.execute`, check the kind and the id first:
       `kind != "payment"` or a bad `data_id` → `Ok(False)` (200, ignored) before the secret
       and the signature are looked at; they do nothing, so no signature is needed. Then the
       secret (503) and the signature (401) as today.
     - **L1.** `docs/architecture.md`: the payments module is active with the webhook; replace
       the `payments.payment.approved` description with the real flow (webhook →
       `payments.notification.received` → worker fetches the payment → orders'
       `OrderPaymentConfirmations`) and list `payments.payment.flagged`.
   - Observable result: two concurrent deliveries of the same approved payment leave
     `outcome = applied` and one paid move; a `type=merchant_order` webhook without a signature
     answers 200; the stock-gone email has the new copy; `uv run just check` green.

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

6. **Step 7 (2026-10-26): existing tests edited by the implementer (scope/role).** The repair
   reverses behaviors that six existing tests asserted (check order, `get_for_update`, the
   stock-gone actor, in-memory "saves nothing"); each got the minimal edit listed in
   `## Repair log` so `check` stays green. All are files the plan declares (steps 6 and 7);
   the tester should review them. `STOCK_GONE_TEXT` is a new constant in `order_emails.py`
   (cosmetic).

Not done here: the layered tests (testing phase). Only a throwaway smoke in the session
scratchpad exercised the facade outcomes, the handler (applied once, redelivery no-op, raise on
gateway failure), the receive command, the webhook route (401/503/200 ignored) and the gateway
fetch over `httpx.MockTransport`. The migration was applied to the development and test
databases; its downgrade was not run (development database must not be downgraded).

## Test coverage

Tested 2026-10-24 by the tester. Baseline before writing: `uv run just check` green (3760 passed,
3 skipped) and `uv run just test-integration` green (1074 passed, 5 skipped). Every behavior
below was confirmed against the code at the cited line or by running the test; the Mercado Pago
API itself is faked (`httpx.MockTransport`, `FakeGateway`), so nothing here proves the real
sandbox answers as the fixtures say (that is the verifier's job).

| Behavior (plan / code) | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| `auto_return` only with an https back URL; the three back URLs always; rest of the body unchanged | `mercadopago_gateway.py:176-180` | application (adapter over `MockTransport`) | `unit/payments/test_mercadopago_get_payment.py::test_without_an_https_back_url_there_is_no_auto_return`, `::test_with_an_https_back_url_auto_return_is_approved`, `::test_the_rest_of_the_body_is_the_same_with_or_without_auto_return` | CONFIRMED |
| Signature: manifest with/without `x-request-id` and `data.id`, key case/order, `v2` only, missing/empty header, non-digit `ts`, missing `v1`, wrong secret/id/ts, non-ASCII `v1`, no tolerance | `webhook_signature.py:22-40` | domain | `unit/payments/test_payment_domain.py` (signature block) | CONFIRMED |
| `Payment.refresh` takes reported fields only; `settle` once, raises on the second | `payment.py:42-55` | domain | `test_payment_domain.py::test_refresh_*`, `::test_settle_*` | CONFIRMED |
| `amount_to_cents` rounds half-up through `Decimal(str(v))` | `payment.py:18-21` | domain | `test_payment_domain.py::test_amount_to_cents_rounds_half_up_through_the_decimal_text` | CONFIRMED |
| Events names/payloads; signature error is a 401 category with its code | `events.py`, `errors.py:20-23` | domain | `test_payment_domain.py::test_the_events_*`, `::test_the_invalid_signature_error_*` | CONFIRMED |
| Receive: other kinds and malformed/absent ids -> `Ok(False)` before the secret and the signature are looked at (step 7, L3); then no secret -> 503 error and nothing stored; bad signature -> 401 error, logs request id only; 1 to 20 digit ids accepted; one event | `receive_notification.py` (check order as of step 7) | application | `unit/payments/test_receive_notification.py` (`test_the_kind_is_checked_before_the_signature`) | CONFIRMED (re-confirmed after step 7) |
| Webhook route: 200 `{"received": true}` accepted and ignored, 401 code/message, 503 without secret, `data.id` alias (the snake-case `data_id` is ignored with 200, no event), body never read, public, GET 405, OpenAPI parameters/responses | `router.py:42-64` | http | `unit/payments/test_webhook_http.py` | CONFIRMED |
| An unsigned notification of another type (`merchant_order`, `topic_merchant_order_wh`, `plan`) answers 200 with no event; an unsigned `payment` is still 401 (step 7, L3) | `receive_notification.py` | http | `test_webhook_http.py::test_an_unsigned_notification_of_another_type_answers_200_not_401`, `::test_an_unsigned_payment_notification_is_still_401` | CONFIRMED |
| A notification signed without `x-request-id` is accepted | plan step 3 ("request-id only when present") | http | `test_webhook_http.py::test_a_notification_without_a_request_id_is_accepted_when_signed_that_way` | GAP (strict xfail): the platform middleware injects a generated `X-Request-ID` (`shared/http/middleware.py:56-69`); finding `plans/findings/payments-webhook-request-id-injected-by-platform.md` |
| Outcome table: `paid` -> `applied`; `not_pending` -> `refund_due`; `stock_gone` -> `refund_due` + flag; `amount_mismatch` -> `amount_mismatch` + flag; `not_found` -> outcome unset + log; any other status recorded, order untouched | `process_payment.py:54-134` | application | `unit/payments/test_process_payment.py` (first block) | CONFIRMED |
| Idempotent redelivery; a settled payment is never confirmed again; later `refunded` keeps `applied`; refresh keeps first amount/order; pending then approved applies | `process_payment.py:73,88-106` | application | `test_process_payment.py::test_a_redelivered_*`, `::test_a_payment_stored_with_an_outcome_*`, `::test_a_later_refund_*`, `::test_a_refresh_*`, `::test_a_payment_that_was_pending_*` | CONFIRMED |
| Unknown payment (404) and non-UUID reference are logged and ignored; gateway down and refusals from orders raise (retry); flag without number raises; fetch outside the transaction, apply inside. Superseded by step 7: with `claim` the in-memory repository stores the row before confirming and its fake runner does not roll back, so the refusal test asserts only "the stored row has no outcome"; "saves nothing" is proven over PostgreSQL (`test_payment_confirmation_flow.py::test_a_refusal_from_inventory_rolls_back_the_payment_row_and_the_order`, `::test_a_gateway_failure_raises_and_stores_nothing`) | `process_payment.py` | application / integration | `test_process_payment.py` (nothing to apply, failures, transaction shape) | CONFIRMED |
| `InMemoryPayments.claim` stores an unknown payment and returns it, keeps a stored row and its outcome over the one offered, hands out its own copy (step 7, H1) | `in_memory.py:35-38` | application | `test_process_payment.py::test_claim_*` | CONFIRMED |
| Flagged email: exact subject/body for both reasons, `$1,234.50 MXN`, one email per active owner, no-owner log, `EmailRejected` skipped, send failure raises | `flagged_payment_email.py:32-66` | application | `unit/payments/test_flagged_payment_email.py` | CONFIRMED |
| `PAYMENTS_ACTOR = "payments"` is shown as `cancelled_by == "system"`; customer and shop mapping unchanged (step 7, L2) | `order.py:48,94-100` | domain | `unit/orders/test_order_domain.py::test_the_payments_actor_cancels_as_the_system`, `::test_cancelled_by_still_tells_the_customer_and_the_shop_apart` | CONFIRMED |
| Cancelled email for the `payments` actor: exact stock-gone copy with the refund line once, whatever the refund mark, typed reason never shown, "no recibimos el pago a tiempo" absent; other actors unchanged (step 7, L2) | `order_emails.py:40,160-171` | application | `unit/orders/test_order_emails.py::test_a_payments_cancel_*`, `::test_the_stock_gone_copy_*` | CONFIRMED |
| Facade `confirm`: paid (commit + move, SYSTEM actor, note, events), no reservation, past deadline, by id; not found; not pending for every later status; amount and currency mismatch; stock gone (both inventory codes) cancels + releases with actor `payments` (step 7; was `SYSTEM`); other commit refusals and a refused release pass through unsaved; one transaction | `confirm_payment.py:46-97` | application | `unit/orders/test_confirm_payment.py` | CONFIRMED |
| `get_by_id_for_update` (in-memory, SQL) finds by id, `None` for unknown | `in_memory.py:111`, `sql_order_repository.py:124` | application / integration | `test_confirm_payment.py::test_each_order_is_found_by_its_own_id`, `integration/orders/test_payment_confirmation_flow.py::test_get_by_id_for_update_finds_the_order_*` | CONFIRMED |
| `get_by_id_for_update` locks the order against a concurrent payment; two payments (or a manual one plus a webhook one) sell the units once | `sql_order_repository.py:124-131` | integration | `test_payment_confirmation_flow.py::test_get_by_id_for_update_locks_*`, `::test_two_payments_at_once_*`, `::test_a_manual_payment_and_a_webhook_payment_at_once_*` | CONFIRMED |
| Facade + real inventory in one transaction: commit sells units; expired reservation still pays while units exist; units gone cancel and leave no sale; mismatch and unknown order change nothing; made-to-order only touches no inventory | `confirm_payment.py` | integration | `integration/orders/test_payment_confirmation_flow.py` (facade block) | CONFIRMED |
| Handler end to end over PostgreSQL: applied once, redelivery no-op, rejected stored, pending then approved, cancelled/paid order -> `refund_due`, mismatch and stock-gone flags in the outbox, unmatched order keeps outcome unset, gateway failure and an inventory refusal leave nothing half-applied | `process_payment.py` | integration | `test_payment_confirmation_flow.py` (handler block) | CONFIRMED |
| `get_payment`: GET with bearer, no body, 10 s timeout, own client without proxy env; field mapping, cents; 404 -> `Ok(None)`; every bad body, non-2xx, transport error and missing token -> `PaymentGatewayUnavailable`, logged without the token | `mercadopago_gateway.py:69-134` | application (adapter over `MockTransport`) | `unit/payments/test_mercadopago_get_payment.py` | CONFIRMED |
| Real Mercado Pago answers `GET /v1/payments/{id}` with the fixture shape | plan "Mercado Pago" notes | integration | none | NOT CONFIRMED: needs the sandbox and a real payment (verifier); fixtures follow the SDK `PaymentResponse` type only |
| Migration 0024: columns/types/limits, PK `payment_id`, non-unique `ix_payments_order_id`, no FK, nulls and limits refused, `checkouts` and orders untouched, downgrade drops only the table, round trip | `0024_payments_payments.py` | integration (scratch DB) | `integration/payments/test_migration_0024_payments_payments.py` | CONFIRMED |
| `SqlPaymentRepository`: whole round trip, enum outcome, upsert by id keeps `created_at`, rollback stores nothing, limits | `sql_payment_repository.py` | integration | `integration/payments/test_sql_payments.py` | CONFIRMED |
| `SqlPaymentRepository.claim` (step 7, H1; replaces `get_for_update`): inserts the row when the payment is unknown, keeps a stored row and its outcome, a rolled-back claim leaves no row, locks the existing row until the transaction ends, and a claim of a new id blocks a second claim of the same id until the first commits (the second then reads the stored `outcome`) | `sql_payment_repository.py:19-50` | integration | `test_sql_payments.py::test_claim_*`, `::test_a_rolled_back_claim_leaves_no_row`, `::test_a_claim_of_a_new_payment_blocks_a_second_claim_of_the_same_id` | CONFIRMED |
| Two deliveries of the same new approved payment at once over PostgreSQL: one paid move, one sale movement, one payment row, `outcome = applied` (step 7, H1) | `process_payment.py` `_stored` + `claim` | integration | `test_payment_confirmation_flow.py::test_two_deliveries_of_the_same_new_payment_at_once_apply_it_once` | CONFIRMED |
| Wiring: container resolves `ReceivePaymentNotification` with the secret, subscribes both handlers, shares the checkout's gateway; `OrderPaymentsAdapter.confirm` maps outcomes; `IdentityOwners` keeps active owners only | `module.py:56-82`, `order_payments.py:44-53`, `identity_owners.py` | application | `unit/payments/test_payments_wiring.py` | CONFIRMED |
| Production refuses to start without `MERCADOPAGO_WEBHOOK_SECRET`; the secret is not printed | `config.py:115-117` | application | `unit/test_settings.py::test_production_requires_a_mercadopago_webhook_secret`, `::test_the_mercadopago_webhook_secret_is_never_printed` | CONFIRMED |
| A real signed webhook from Mercado Pago's panel; `APRO`/`OTHE` cards in the sandbox; Mailpit emails | acceptance criteria | e2e | none | NOT CONFIRMED: no e2e infrastructure and needs the sandbox and a tunnel (verifier) |

Notes:
- Deviation 1 (the three out-of-scope test fixtures) is accepted here: they are test files and
  the testing phase owns them; `test_settings.py` also gained the missing production test for the
  webhook secret.
- Closing runs: `uv run just check` green (4002 passed, 3 skipped, 1 xfailed = the GAP above;
  baseline 3760 passed, 3 skipped, so +242 unit tests, +1 xfail); `uv run just test-integration`
  green (1133 passed, 5 skipped; baseline 1074, so +59 integration tests). `plans-scope` was not
  run by the tester (the main session does it at commit).

Step 7 repair, retested (tester, 2026-10-26). Rows above marked "step 7" replace the ones that
described `get_for_update`, signature-before-kind and the `SYSTEM` stock-gone actor. The
`## Review findings` checklist still describes the old code; the next review supersedes it.
Baseline: `uv run just check` green (4002 passed, 3 skipped, 1 xfailed), `uv run
just test-integration` 1133 passed, 5 skipped. Closing: `check` green (4014 passed, 3 skipped,
1 xfailed; harness 685; import-linter all KEPT), `test-integration` 1138 passed, 5 skipped.
Added: unit +12 (`test_order_domain` 2, `test_order_emails` 3, `test_webhook_http` 4,
`test_process_payment` 3), integration +5 (`test_sql_payments` 4, `test_payment_confirmation_flow`
1). No GAP and no NOT CONFIRMED added; every item the Repair log left for the tester is covered.

## Review findings

### First review (2026-10-24, at c010927; superseded where the re-review below says so)

Reviewed 2026-10-24 to 2026-10-26, diff `main...HEAD` (c010927), tree clean apart from this
section. `check` and `plans-scope` were run on 2026-10-24 and `test-integration` on 2026-10-26,
all at the same HEAD.

**Checklist: 13 of 14 applicable items passed. 1 failed (stale doc). 1 n/a (PR body).**

- [x] `plans-scope`: "Every change is inside the plan" (41 declared, 52 changed, all covered).
  Hot files: `.importlinter` gains exactly the two planned ignore lines, appended; `container.py`
  and `docs/modules.json` are untouched.
- [x] `uv run just check` green: lint, types, 7 import-linter contracts KEPT (14 ignored
  imports), 4002 passed, 3 skipped, 1 xfailed (the tester's strict xfail), harness 685 passed.
- [x] `uv run just test-integration` green (live run 2026-10-26 at c010927, the first run was
  cut off when the session was interrupted): 1133 passed, 5 skipped.
- [x] Business rules: `Payment.settle` (set once) is in the domain. The amount/currency rule
  and the outcome mapping live in the orders facade (`confirm_payment.py:59`) and the
  handler (`process_payment.py:119-133`), as steps 2 and 4 prescribe, following the existing
  `OrderMove` shape (commit/release orchestration is application code there too). Accepted.
- [x] CQRS-lite: `ReceivePaymentNotification`, `OrderPaymentConfirmations.confirm` and the
  handler's apply all run inside `transactions.run` and return `Result`; the repositories have
  no screen-specific methods.
- [x] Contracts: `WebhookAck` in `payments/contracts.py`; `openapi.json` regenerated (no drift,
  `test_openapi.py` green).
- [x] Errors: `PAYMENTS_WEBHOOK_SIGNATURE_INVALID` (401) and `PAYMENTS_GATEWAY_UNAVAILABLE`
  (503); the handler raises only for the unexpected (gateway down, refusal from orders), which
  is the relay's retry contract.
- [x] Money as integer cents (`amount_to_cents` half-up through `Decimal(str(v))` at the
  gateway boundary only); datetimes from `Clock`; payment ids are Mercado Pago's.
- [x] Migration 0024: new table only in the existing `payments` schema, no FK, no drops,
  downgrade drops only the index and table (round trip tested on a scratch DB).
- [x] Route on `public_router` (Mercado Pago calls it unauthenticated; the signature is the
  gate). No bare `APIRouter`.
- [x] Wiring resolves (`test_payments_wiring.py`, `test_container.py` green); adapters built
  only in `module.py`; orders and identity register before payments.
- [x] No secrets: `.env.example` value empty; tests use placeholder secrets; the token and
  webhook secret are never logged (`receive_notification.py` logs only `x_request_id`/`kind`).
- [x] Deviations honest. Spot check of Deviation 3: `receive_notification.py` logs
  `x_request_id=` in both warnings, as stated. Deviation 1 files are listed in step 5.
- [ ] **Docs: `docs/architecture.md` is stale for this behavior.** Line 65 (`payments` row)
  still says "Planned: idempotent webhooks", and lines 197-200 describe the flow as
  `payments.payment.approved` → `orders` subscriber, whereas the built flow is
  `payments.notification.received` → worker → orders' `OrderPaymentConfirmations` facade in
  the same transaction. The orders row (line 64) does not list the new public facade either.
  `docs/architecture.md` is not in the plan's file list, so the fix needs a deviation line.
- n/a PR body: no PR exists yet (the main session opens it).

**Focus areas checked and found correct:**
- **Signature.** Manifest built exactly as the cited SDK rules (`id:` from the query
  `data.id`, `request-id:` only when present, `ts:`), HMAC-SHA256 hex, constant-time compare;
  a missing/empty header, non-digit `ts` or missing `v1` is invalid. The check runs before
  the kind/id filter, so an unsigned request can never enqueue anything.
- **Idempotency (sequential).** A redelivery locks the existing `payments.payments` row
  (`FOR UPDATE`) and sees `outcome` set; nothing reaches orders. A pending-then-approved
  payment applies on the second delivery.
- **Order transitions.** The facade locks the order (`FOR NO KEY UPDATE`, same as the
  manual mark-paid and the overdue job), so a webhook vs. manual payment vs. overdue
  cancellation cannot both win; tested against PostgreSQL. Commit + `mark_paid`, or refused
  commit + `cancel_unpaid` + release, happen in one transaction; any `Err` raises in the
  handler and the relay rolls the whole transaction back (the nested `transactions.run` joins
  the relay's and the handler converts `Err` to an exception).
- **Gateway fetch.** Bearer header only, `trust_env=False`, 10 s timeout, 404 → `Ok(None)`,
  every malformed body → `PaymentGatewayUnavailable`; the id in the URL is validated as 1-20
  digits at receipt.
- **auto_return fix.** Only with an `https://` back URL; the other body fields are unchanged.
- **Production validator.** Missing or empty `MERCADOPAGO_WEBHOOK_SECRET` refuses to start.

### Findings

**High**

- **H1 — Two concurrent deliveries of a payment seen for the first time can overwrite
  `applied` with `refund_due`** (`process_payment.py:88-106` with
  `sql_payment_repository.py:39-55`; the race sits in `_stored`/`save`).
  `get_for_update` is `SELECT … FOR UPDATE`, which locks nothing when the row does not exist
  yet. Scenario: Mercado Pago sends `payment.created` and `payment.updated` for the same
  approved card payment seconds apart, so two `payments.notification.received` outbox rows
  exist. The relay is an arq cron every 2 s with no per-process lock
  (`main/worker.py:84`; arq's cron `unique` only dedupes the same tick, and `max_jobs` is
  10), so two relay runs overlap whenever a batch takes over 2 s (one Mercado Pago GET plus
  SMTP sends is enough), and `SKIP LOCKED` hands each run one of the two rows. Run A: no
  payment row → `confirm` → order paid → inserts the row with `outcome=applied` → commits.
  Run B: also saw no row → `confirm` waits on the order lock, then gets `not_pending` →
  `settle(REFUND_DUE)` → `INSERT … ON CONFLICT DO UPDATE` (waits for A's commit, then updates)
  sets `outcome=refund_due`. End state: the order is paid by this payment, but the payment is
  recorded `refund_due`, so plan 004 would refund the payment that paid the order (the
  customer gets the goods and the money back). Not covered: the integration tests race two
  *different* payments (`test_two_payments_at_once_*`), never the same payment twice.
  Possible directions (the implementer/architect decide): take the row lock before reading
  (e.g. `INSERT … ON CONFLICT DO NOTHING` then `SELECT … FOR UPDATE`, or a transaction-level
  advisory lock on the payment id), or make the upsert never replace a non-null `outcome`.

**Low**

- **L1 — `docs/architecture.md:65,197-200` stale** (checklist item above). Scenario: the next
  plan or module author reads that orders subscribes to `payments.payment.approved`, and builds
  against an event that does not exist.
- **L2 (uncertain; design, not a code change) — the stock-gone cancellation email tells a
  customer who paid that "no recibimos el pago a tiempo"**
  (`confirm_payment.py:67-68` with `SYSTEM_ACTOR`; copy at
  `orders/application/handlers/order_emails.py:37`). The plan states this behavior (Context,
  "Cancelling an unpaid order"; decision 10; Out of scope "customer email copy"), so it is
  not a deviation. Recorded only so the user consciously accepts the wording until plan 004
  tells the customer about the refund.
- **L3 (uncertain) — legacy IPN notifications get 401, not "200 and ignored"**
  (`receive_notification.py:51-56`). Out of scope says merchant-order and legacy IPN
  notifications are "answered 200 and ignored", but the signature is checked before the kind,
  and IPN requests (`?topic=…&id=…`) do not carry `x-signature` as far as the cited SDK shows,
  so they would get 401 and Mercado Pago would retry them. Only matters if IPN is ever
  enabled; preferences send no `notification_url`, so today none arrive. Step 4's ordering
  (signature first) is what the code follows.
- Already recorded by the tester, not repeated as a review finding: the platform-injected
  `X-Request-ID` (`plans/findings/payments-webhook-request-id-injected-by-platform.md`).

**Result:** H1 requires a code change (and a regression test racing the same payment id
twice), L1 a doc change outside the file list. Status stays `review`.

### Re-review (2026-10-26, at e37f388; repair diff `d7bf6c4..HEAD`)

This replaces the first review's checklist and the "focus areas" items it names below. The
first review's findings are kept above as history; each one's status is given here. Runs
were live at e37f388 with a clean tree: `plans-scope`, `check` and `test-integration`.

**Checklist: 13 of 14 applicable items passed. 1 failed (one stale docstring, R-L1). 1 n/a
(PR body).**

- [x] `plans-scope`: "Every change is inside the plan" (46 declared, 58 changed). The repair
  files are all in step 7's list. `.importlinter` is unchanged since the first review.
- [x] `uv run just check` green: lint, types, 7 contracts KEPT (14 ignored imports), 4014
  passed, 3 skipped, 1 xfailed (the known request-id xfail), harness 685 passed.
- [x] `uv run just test-integration` green: 1138 passed, 5 skipped.
- [x] Business rules: unchanged from the first review. `PAYMENTS_ACTOR` and its
  `cancelled_by` mapping are in the domain (`order.py:48,94-100`).
- [x] CQRS-lite: `claim` is a write-side method used inside the handler's `transactions.run`.
  It is not screen-specific.
- [x] Contracts and `openapi.json`: no contract changed in the repair.
- [x] Errors: same codes. Only the order of the checks changed (L3).
- [x] Money, Clock and ids: unchanged.
- [x] Migrations: none in the repair. `claim` uses the existing PK `payment_id`.
- [x] Routes: unchanged.
- [x] Wiring: unchanged, and `test_payments_wiring.py` / `test_container.py` are green.
- [x] No secrets in the repair or the new tests.
- [x] Deviations honest. Spot check of Deviation 6: the existing tests named in the Repair
  log were edited exactly as described. The refusal unit test now asserts "no outcome" rather
  than "nothing saved", and the integration rollback tests still cover "nothing saved".
- [ ] **Docs: one stale docstring (R-L1 below).** `docs/architecture.md` is fixed (first
  review L1).
- n/a PR body: no PR yet.

**First-review findings, closed by the diff:**

- **H1: closed.** `SqlPaymentRepository.claim` (`sql_payment_repository.py:17-50`) runs
  `INSERT … ON CONFLICT (payment_id) DO NOTHING` first, then `SELECT … FOR UPDATE`.
  `_stored` (`process_payment.py:88-109`) claims before it reads.
  - Traced under PostgreSQL's default READ COMMITTED (no isolation override in
    `database.py`). Run A inserts the row but has not committed. Run B's insert waits on A's
    unique-index entry until A commits, then does nothing. B's `SELECT … FOR UPDATE` is a new
    statement, so it sees A's committed row with `outcome = applied`. B then skips `confirm`,
    and its `save` rewrites the same outcome.
  - A rolled-back A leaves no row, so B inserts its own and proceeds normally.
  - Lock order is payment row then order row on every path; no path takes them the other way,
    so there is no new deadlock.
  - Covered by `test_sql_payments.py::test_a_claim_of_a_new_payment_blocks_a_second_claim_of_the_same_id`
    (the SQL-level serialization) and
    `test_payment_confirmation_flow.py::test_two_deliveries_of_the_same_new_payment_at_once_apply_it_once`.
  - **Uncertain:** the handler-level test overlaps the two runs through `asyncio.gather`,
    with a `FakeGateway` that never suspends. Its interleaving is likely but not forced, and,
    as the coordinator notes, it was never run against the pre-fix code, so it is not proven
    red. The SQL-level test is the stronger evidence. I did not run the old code (that would
    mean editing or checking out product code).
- **L1: closed.** `docs/architecture.md:64-65,197-205` now describes the built flow, both
  facades and the flagged event. It matches the code.
- **L2: closed by decision 13 (initiative README).**
  - The stock-gone cancel uses `PAYMENTS_ACTOR` (`confirm_payment.py:68`), and the paid move
    keeps `SYSTEM_ACTOR`.
  - `cancelled_email` (`order_emails.py:161-171`) sends the stock-gone copy with the refund
    line.
  - The customer page still says "system", through `cancelled_by`.
  - `order_events.actor` is `String(64)`, so "payments" fits.
  - Nothing in orders parses the actor as a user id (`sql_order_queries.py:181` passes it
    through).
- **L3: closed.** The kind and the id are checked first (`receive_notification.py:51-53`);
  an ignored notification stores nothing, so skipping the signature for it is safe. An
  unsigned or badly signed `payment` is still 401. The one behavior change: with no secret
  configured, non-payment kinds now get 200 instead of 503 (harmless).

**Regression hunt of the repair:** I found no correctness regression.
- `claim` inserts the fetched values, and `refresh` then `save` keep the first amount and
  order, as before.
- A non-UUID `external_reference` still returns before any row is written.
- A `not_found` order still leaves `outcome` unset.
- A refusal from orders or inventory raises, and the relay rollback removes the claimed row
  (integration `test_a_refusal_from_inventory_rolls_back_the_payment_row_and_the_order`).

**Findings**

**Low**

- **R-L1: `apps/api/src/fragancia_api/modules/orders/__init__.py:44-45` still says the
  stock-gone order "was cancelled by `system`".** Since the repair the actor is `payments`
  (`confirm_payment.py:68`). Scenario: the next module author (plan 004, refunds) reads the
  public-API docstring, looks for a `system` cancel in the history to find stock-gone orders,
  and finds none. The file is in the plan's file list (step 2), but not in step 7's, so the
  fix needs a repair-log line. The related admin-contract and table comments are already in
  `plans/findings/orders-history-actor-docs-omit-payments.md`; this docstring is not listed
  there.
- Carried over, unchanged: the platform-injected `X-Request-ID` finding.

**Result:** only R-L1 is open. It is a one-line docstring change, but the stale-doc checklist
item fails, so status stays `review`. After the fix, a re-review of that line alone is enough.
No code or test change is needed for H1, L1, L2 or L3.

### Second re-review (2026-10-26, commit 4de770a only)

Scope: only commit 4de770a, which is docs only (`orders/__init__.py`, the `AdminOrderEvent`
docstring, the `order_events` comment, the regenerated `openapi.json`, the finding
`orders-history-actor-docs-omit-payments` and this plan). Runs were live at 4de770a with a
clean tree.

- **R-L1: closed.** `orders/__init__.py:45` now says "cancelled by `payments`", which matches
  `confirm_payment.py:68`.
- **The actor finding: resolved correctly.**
  - `contracts.py:240` and `tables.py:131` now list an admin user id, `system`, `customer`
    and `payments`. That is every actor the code writes: `SYSTEM_ACTOR`, `CUSTOMER_ACTOR`,
    `PAYMENTS_ACTOR` (`order.py:44-48`) and the admin ids from the router.
  - The `openapi.json` change is the one-line `AdminOrderEvent` description, made by the
    generator. `check` is green, so the committed file matches the generator.
  - The finding is `status: resolved` with `plan: payments-mercadopago/002`, and
    `plans-lint` passes.
- **Scope:** `plans-scope` reports "Every change is inside the plan" (48 declared, 60
  changed). The new files are declared on step 7's second `Files:` line, and the Repair log
  has an entry for them.
- **Runs:**
  - `uv run just check` green: 4014 passed, 3 skipped, 1 xfailed; harness 685; 7 contracts
    kept.
  - `test-integration` was not re-run. The `tables.py` change is a comment only, with no
    schema or SQL change; the last integration run (e37f388) was green with 1138 passed.
- I checked for other stale descriptions of `actor`: none remain. `AdminOrderNote`'s "the
  admin user id" is right, because notes are written only by admins.

**Checklist (superseding the re-review's): 14 of 14 applicable items passed, 1 n/a (PR
body).** Nothing is open. The only finding carried over is the tester's request-id finding,
which is a separate finding and does not block. Status → `verify`.

## Repair log

**2026-10-26 — step 7 (review repair: H1, L1, L2, L3), by the implementer.** Citations of
step 7 spot-checked (`get_for_update` in the port, SQL and in-memory adapters; `_stored` in
`process_payment.py`; the check order in `receive_notification.py`; `SYSTEM_ACTOR` and
`cancelled_by` in `order.py`; `cancelled_actor`/`cancelled_email` in `order_emails.py`;
`docs/architecture.md` payments row and example flow): all matched.

- **H1.** `PaymentRepository.get_for_update` is replaced by `claim(payment) -> Payment`
  (`payments/domain/repositories.py`). SQL (`sql_payment_repository.py`): `INSERT … ON
  CONFLICT (payment_id) DO NOTHING`, then `SELECT … FOR UPDATE` (`.one()`), mapping the stored
  row; the column values are shared with `save` through a `_values` helper, and `save` is still
  the upsert. In-memory: stores the row when absent and returns a copy of the stored one.
  `ProcessPaymentNotification._stored` builds the fresh `Payment` from the fetched one, `claim`s
  it, then `refresh`es it. A concurrent delivery of the same id now blocks on the insert (or the
  row lock) until the first transaction ends, then reads `outcome` already set.
- **L2.** `PAYMENTS_ACTOR = "payments"` in `orders/domain/order.py`; `cancelled_by` maps it to
  `"system"`. The stock-gone cancel in `confirm_payment.py` uses it (the paid move keeps
  `SYSTEM_ACTOR`). `order_emails.cancelled_email` sends `STOCK_GONE_TEXT` ("Tu pedido {number}
  fue cancelado porque tu pago llegó cuando ya no había existencias.\nTe reembolsaremos el pago
  completo.\n") when the cancelling actor is `payments`; the subject and every other case are
  unchanged.
- **L3.** `ReceivePaymentNotification.execute` checks the kind and the id first (`Ok(False)`,
  logged `payments.webhook_ignored`), then the secret (503), then the signature (401). Docstring
  updated.
- **L1.** `docs/architecture.md`: the payments row lists the webhook flow and
  `payments.payment.flagged` (the "Planned: idempotent webhooks" item removed); the orders row
  lists the `OrderPayments` and `OrderPaymentConfirmations` facades; the example flow is the
  built one (`payments.notification.received` → worker fetch → `OrderPaymentConfirmations` in
  the same transaction).
- **Existing tests that encoded the replaced behavior** (minimal edits to keep `check` green,
  no new coverage; the regression tests stay with the tester):
  - `tests/integration/payments/test_sql_payments.py`: the `get` helper reads through `claim`
    (after a `count(*)` so an unknown id still reads as None, since `claim` would insert); the
    lock test renamed `test_claim_locks_the_row_until_the_transaction_ends` and calls `claim`.
  - `tests/unit/payments/test_receive_notification.py`:
    `test_the_signature_is_checked_before_the_kind` became
    `test_the_kind_is_checked_before_the_signature` (an unsigned `merchant_order` is `Ok(False)`).
  - `tests/unit/payments/test_webhook_http.py::test_the_data_id_is_read_from_the_dotted_query_name`:
    the snake-case `data_id` now answers 200 (ignored, no event) instead of 401.
  - `tests/unit/payments/test_process_payment.py::test_a_refusal_from_orders_raises_with_its_code_and_saves_nothing`:
    the in-memory `claim` stores the row before confirming and the fake runner does not roll
    back, so the assertion is now "the stored row has no outcome"; the PostgreSQL rollback stays
    covered by the integration handler tests.
  - `tests/unit/orders/test_confirm_payment.py` (stock-gone history actor is `PAYMENTS_ACTOR`;
    `test_a_cancelled_for_stock_order_is_cancelled_by_the_system` asserts
    `cancelled_by(actor) == "system"`) and
    `tests/integration/orders/test_payment_confirmation_flow.py` (stock-gone history actor
    `"payments"`).
- **Not done here (testing phase):** the regression tests step 7 names: two concurrent
  deliveries of the same new approved payment over PostgreSQL (one paid move, `outcome =
  applied`); `claim` inserting when absent and keeping a stored `outcome`; the stock-gone email
  copy (`test_order_emails.py`); `cancelled_by(PAYMENTS_ACTOR)` (`test_order_domain.py`); an
  unsigned `type=merchant_order` webhook answering 200. Those two declared files are unchanged.
- **Superseded evidence.** The `## Test coverage` rows for the signature-before-kind order, the
  `FOR UPDATE` lock via `get_for_update`, the stock-gone `SYSTEM` actor and "saves nothing" on a
  refusal (in-memory) describe the code before this repair; the `## Review findings` checklist
  and focus areas likewise. Both need the next testing and review passes.
- **Finding filed:** `plans/findings/orders-history-actor-docs-omit-payments.md` (the
  `AdminOrderEvent` docstring and the `order_events` comment still say the actor is an admin
  id or `system`; not in the file list, and the docstring feeds `openapi.json`).
- **Runs:** `uv run just check` green (4002 passed, 3 skipped, 1 xfailed; harness 685 passed;
  import-linter all KEPT); `uv run just test-integration` first run 1 failed (the stock-gone
  actor assertion in `test_payment_confirmation_flow.py`, fixed above), 1132 passed, 5 skipped;
  after the fix the affected files (`tests/integration/orders/test_payment_confirmation_flow.py`,
  `tests/integration/payments/`) pass; `plans-scope`: every change inside the plan.

**2026-10-26 — re-review R-L1 and the actor finding, by the main session.** Docs only:
`orders/__init__.py` now says the stock-gone order is cancelled by `payments`; the
`AdminOrderEvent` docstring (`orders/contracts.py`) and the `order_events` comment
(`orders/infrastructure/tables.py`) list the actors `system`, `customer` and `payments` besides an
admin user id; `uv run just openapi` regenerated (one description line). The finding
`orders-history-actor-docs-omit-payments` is resolved by this plan. Declared on step 7's second
`Files:` line.

## Verification

2026-10-26/27, main session, live against the development database and the **real Mercado
Pago sandbox** (the developer's test seller account, decision 1), through a `cloudflared` quick
tunnel to the API on :8100, with the worker running.

**Suites (once, at 9087f77):** `uv run just check` green (4014 passed, 3 skipped, 1 xfailed;
harness 685). `uv run just test-integration`: 1138 passed, 5 skipped. `uv run just db-migrate`
clean (0024 applied).

**Setup notes.** The panel's Webhooks page has a test and a production tab, each with its own
secret; the event is "Pagos (legacy)". This account's real notifications are signed with the
**test-mode** secret: with the production secret, Mercado Pago's two deliveries for payment
`186361429202` got 401 (`payments.webhook_signature_invalid`); with the test secret every later
delivery passed. A capture on :8100 confirmed the manifest Mercado Pago signs is exactly
`id:<query data.id>;request-id:<x-request-id>;ts:<ts>;` (match=True), as the code builds it.
Recorded as `plans/findings/payments-webhook-setup-docs-incomplete.md`.

**Payments and notifications** (QA orders `LFI-1028` to `LFI-1034` stay in the database):

- [x] **Link with an http storefront.** `POST /api/v1/payments/checkout` for `LFI-1028`, with
  `STOREFRONT_URL=http://localhost:4200`, answered 200 with a real
  `https://sandbox.mercadopago.com.mx/…?pref_id=3791681536-…` URL (before step 1 it was 503
  after Mercado Pago's `400 invalid_auto_return`). Every later order got its link the same way.
- [x] **Wrong or missing signature.** Through the tunnel, an unsigned `type=payment` and a forged
  `x-signature` both answered 401 `PAYMENTS_WEBHOOK_SIGNATURE_INVALID`; no outbox row.
- [x] **Signed `type=payment` → one outbox row; `merchant_order` ignored.** Signed notifications
  answered 200 `{"received":true}` and stored one `payments.notification.received`; an unsigned
  `type=merchant_order` answered 200 with no row. The panel's "Simular notificación"
  (id `123456`) answered 200 and the worker logged `payments.payment_not_found`.
- [x] **Approved payment → paid.** `LFI-1030`, paid with a credit card (holder APRO); Mercado
  Pago's own notification arrived and within seconds:
  - payment `185317326417` approved, `outcome = applied`;
  - the order `paid`, reservation `committed`, history `system`
    "Pagado con Mercado Pago (pago 185317326417)";
  - Mailpit: "Confirmamos el pago de tu pedido LFI-1030" to the customer and
    "Pedido pagado: LFI-1030 — $2,079.50 MXN" to each of the 3 active owners.
  `LFI-1029` (paid before the webhook URL existed in the panel) was applied the same way from a
  notification signed by hand with the configured secret.
- [x] **Redelivery changes nothing.** The same notification for `185313629663` resent with the same
  request id: published, no new move (one `paid` event), no new email.
- [x] **Rejected payment.** `186362152098` (`LFI-1030`, before its approval) and `186362309796`
  (`LFI-1032`) arrived from Mercado Pago as `rejected` / `cc_rejected_other_reason`, stored with no
  outcome; the orders did not move, and `LFI-1030` was paid afterwards from the same link.
- [x] **Approved for a cancelled order.** `LFI-1032` cancelled by the owner, then paid:
  `185317147487` `outcome = refund_due`, the order still `cancelled`, no email (decision 11).
- [x] **Second approved payment for a paid order.** `LFI-1030` paid again from its link:
  `186363091326` `outcome = refund_due`, still one `paid` move.
- [x] **Late payment with units available.** `LFI-1031`'s reservation was made to expire
  (`expires_at` moved to the past; `inventory_expire_reservations` expired it), then paid:
  `186363234846` `applied`, the order `paid`, reservation `committed` (late commit).
  The order's own `payment_due_at` was not moved past: the overdue job would cancel it before
  the payment lands, so "past the deadline" is exercised through the expired reservation, which
  is what the commit depends on (`confirm` itself never reads the deadline).
- [x] **Late payment with the units gone.** `LFI-1033`'s reservation expired as above, and a
  `count` movement left the 50 ml with `on_hand 1`, `reserved 1` (0 free). Paid:
  `185317998889` `outcome = refund_due`; the order `cancelled` by `payments` with
  "Pago recibido sin existencias (pago 185317998889)"; stock unchanged (1/1). Mailpit: the
  customer got "Tu pedido LFI-1033 fue cancelado porque tu pago llegó cuando ya no había
  existencias. Te reembolsaremos el pago completo." (decision 13); each owner got "Pago a
  revisar: LFI-1033" with "…pero llegó cuando ya no había existencias, así que el pedido se
  canceló."
- [x] **Different amount.** `LFI-1034`'s shipping and total raised by 100 cents in the database
  after its link was made, then paid (2079.50): `186363091748` `outcome = amount_mismatch`, the
  order still `pending_payment` with its reservation `active`, one `payments.payment.flagged`
  (`reason amount_mismatch`), and each owner got "Pago a revisar: LFI-1034" with "…pero el monto
  no coincide con el total del pedido, así que el pedido sigue pendiente."
- [x] **Mercado Pago down while processing.** The worker was run with `MERCADOPAGO_API_URL`
  pointed at a closed port (a launcher setting it in code) and a signed notification sent for
  the unprocessed `186361429202`: `payments.gateway_error ConnectError`, the outbox row at
  `attempts 2` with `RuntimeError('payments.gateway_unavailable')` and backing off, no
  `payments.payments` row. With the normal worker back, the same row was published and the
  payment stored (`rejected`).
- [x] **Production needs the secret.** `Settings` with production values and no webhook secret
  fails with "MERCADOPAGO_WEBHOOK_SECRET is required in production"; with one it passes.

**NOT VERIFIED / limits:**
- The handler-level H1 race test was never run red against the pre-fix code (the reviewer's
  caveat); the race was not reproduced live either.
- A real `refunded` or `charged_back` status after payment (out of scope).
- A notification without `x-request-id` (the open finding
  `payments-webhook-request-id-injected-by-platform`); Mercado Pago sent the header on every
  delivery seen.

**Left behind:** QA orders `LFI-1028` to `LFI-1034` and their payments; the 50 ml presentation's
stock at `on_hand 1` after the stock-gone setup (a `count` movement, restorable from the panel).
