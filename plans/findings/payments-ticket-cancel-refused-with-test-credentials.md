---
status: open # open → deferred | planned | resolved | discarded (the user decides)
module: payments
found: 2026-10-27
---

# Cancelling a pending ticket is refused with the test seller account's credentials

## Found while

The first sandbox run of payments-mercadopago/003 (OXXO and SPEI), verification item V2.

## What

- Decision 20 cancels an order's pending cash and transfer tickets when the order is paid or
  cancelled. The call is `MercadoPagoGateway.cancel_payment`
  (`apps/api/src/fragancia_api/modules/payments/infrastructure/mercadopago_gateway.py`),
  `PUT /v1/payments/{id}` with `{"status": "cancelled"}`. It is used by `CloseCheckout` and
  `CancelTicket`.
- With the test seller account's credentials (`APP_USR-…`), Mercado Pago answered
  `PUT /v1/payments/186412901784` with `401 Unauthorized use of live credentials`.
- 401 is not a permanent refusal (plan 003 repair deviation 5), so the
  `payments.checkout.close_requested` event retried. It was at attempt 5 during the run, and it
  ends in the failed-events panel when retries run out. The ticket stayed
  `pending_waiting_payment` in Mercado Pago.
- Unverified: whether the same call works with the client's production credentials.

## Why it matters

- In development and in the sandbox, every paid or cancelled order with a pending ticket leaves
  a close event that retries and then fails. The failed-events panel fills with these events,
  and the ticket stays payable. Paying it ends in `refund_due` (plan 002).
- If production behaves the same, decision 20 never takes effect.

## Suggested next step

The user decides. Options:

- Try the cancel once with production credentials on a real ticket of a low amount, or ask
  Mercado Pago support whether test sellers can cancel payments.
- If it works in production, accept the sandbox limit and document it.
- Otherwise, revisit decision 20, for example by treating that 401 as permanent or by
  dropping the cancel and relying on the automatic refund (plan 004).
