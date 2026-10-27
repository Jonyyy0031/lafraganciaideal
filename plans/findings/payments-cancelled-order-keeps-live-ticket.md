---
status: open # open → deferred | planned | resolved | discarded (the user decides)
module: payments
found: 2026-10-27
---

# Cancelling an order leaves its issued OXXO or SPEI ticket payable in Mercado Pago

## Found while

Reviewing payments-mercadopago/003 (OXXO and SPEI).

## What

- When an order leaves `pending_payment`, `RequestCheckoutClose` and `CloseCheckout`
  (`apps/api/src/fragancia_api/modules/payments/application/handlers/close_checkout.py:29-103`)
  expire only the Mercado Pago *preference* (the link). A ticket already issued from that link
  is a separate Mercado Pago payment in `pending`. Nothing cancels it.
- So a cash or transfer ticket stays payable at the store or by SPEI until its own
  `date_of_expiration` (up to 48 hours). This happens when the customer cancels the order
  (`POST /api/v1/orders/cancel`), when an owner cancels it, and when the order is paid by card
  (decision 17).
- Decision 18 states this limit only for a guest who is already inside the checkout. It does
  not cover tickets issued before the cancel.

## Why it matters

A customer may cancel and then pay the printed OXXO ticket anyway. Or a customer may pay by card
and later pay the ticket too. Either way the money has to be refunded (`refund_due`, plan 002),
and until plan 004 that refund is manual. The instructions email (decision 15) still points at a
ticket that works. Nothing in the API tells the customer the ticket is no longer valid.

## Suggested next step

The user decides. One option: cancel the order's live pending tickets through Mercado Pago in
the same `payments.checkout.close_requested` subscriber, as part of plan 004. That would be
`PUT /v1/payments/{id}` with `status: cancelled`; check the SDK and the docs for pending cash
payments first. The other option: accept the risk and rely on the automatic refund.
