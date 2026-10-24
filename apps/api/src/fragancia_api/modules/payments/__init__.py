"""Payments: pays a pending order through Mercado Pago Checkout Pro (ADR 0004).

`POST /api/v1/payments/checkout` takes the order's private-link token (a secret, in the body)
and answers the Mercado Pago redirect URL. The API creates a *preference* for the order's
frozen total: items for the lines plus "Envío", cards only in one installment (decision 4),
`external_reference` = the order id, and an expiry equal to the order's `payment_due_at`, so a
stale link cannot pay an order the overdue job cancelled. The preference is stored in
`payments.checkouts` and a second request reuses it while it is valid. `auto_return` is sent
only when the back URL is https (decision 8).

Errors: 404 `PAYMENTS_ORDER_NOT_FOUND`, 409 `PAYMENTS_ORDER_NOT_PAYABLE` and 503
`PAYMENTS_GATEWAY_UNAVAILABLE` (Mercado Pago failed, timed out, refused or is not configured).

The browser redirect back from Mercado Pago never confirms a payment; only the verified webhook
does. `POST /api/v1/payments/webhooks/mercadopago` checks the `x-signature` header with
`MERCADOPAGO_WEBHOOK_SECRET` (401 `PAYMENTS_WEBHOOK_SIGNATURE_INVALID`; 503 when no secret is
configured) and queues a `type=payment` notification; other types are acknowledged and
ignored. The worker fetches the payment from Mercado Pago, stores it in `payments.payments`
and, when it is approved, applies it once through orders' `OrderPaymentConfirmations`: the
order moves to paid (payment `applied`); an amount or currency that differs leaves it pending
(`amount_mismatch`); units gone cancel it (`refund_due`); an order already paid or cancelled
only records `refund_due`. Other statuses are only stored.

Events (published through the outbox; other modules subscribe by name, never by import):

- `payments.notification.received`: a verified webhook named a payment. Payload: `payment_id`
  (string of digits). Payments itself processes it in the worker.
- `payments.payment.flagged`: an approved payment the owners must review. Payload:
  `payment_id` (string), `number` (int; the order is `LFI-<number>`), `reason`
  (`amount_mismatch` or `stock_gone`), `amount_cents` (int) and `currency` (string). Payments
  itself emails each active owner "Pago a revisar".

Public API for other modules: nothing yet.

The composition root wires `module.module`.
"""
