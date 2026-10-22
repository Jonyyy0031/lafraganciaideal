"""Payments: pays a pending order through Mercado Pago Checkout Pro (ADR 0004).

`POST /api/v1/payments/checkout` takes the order's private-link token (a secret, in the body)
and answers the Mercado Pago redirect URL. The API creates a *preference* for the order's
frozen total: items for the lines plus "Envío", cards only in one installment (decision 4),
`external_reference` = the order id, and an expiry equal to the order's `payment_due_at`, so a
stale link cannot pay an order the overdue job cancelled. The preference is stored in
`payments.checkouts` and a second request reuses it while it is valid.

Errors: 404 `PAYMENTS_ORDER_NOT_FOUND`, 409 `PAYMENTS_ORDER_NOT_PAYABLE` and 503
`PAYMENTS_GATEWAY_UNAVAILABLE` (Mercado Pago failed, timed out, refused or is not configured).

The browser redirect back from Mercado Pago never confirms a payment; nothing here moves an
order to paid (the webhook is a later plan).

Public API for other modules: nothing yet.

The composition root wires `module.module`.
"""
