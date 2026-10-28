"""Payments: pays a pending order through Mercado Pago Checkout Pro (ADR 0004).

`POST /api/v1/payments/checkout` takes the order's private-link token (a secret, in the body)
and answers the Mercado Pago redirect URL. The API creates a *preference* for the order's
frozen total: items for the lines plus "Envío", cards in one installment (decision 4), cash in
stores (`ticket`) and SPEI (`bank_transfer`); only `atm` is excluded (decision 14).
`external_reference` = the order id, the link expires with the order's `payment_due_at`, so a
stale link cannot pay an order the overdue job cancelled, and a cash or transfer ticket issued
from it expires 48 hours after the link was created (`date_of_expiration`, decision 5). For
OXXO, Mercado Pago moves that to the end of the day in Mexico City time (up to about 56 hours,
decision 21), and payments uses the expiry Mercado Pago reports. The
preference is stored in `payments.checkouts` and a second request reuses it while it is valid.
`auto_return` is sent only when the back URL is https (decision 8). When the order leaves
`pending_payment` (paid or cancelled), payments expires the preference in Mercado Pago and
closes the stored checkout (decision 18).

`POST /api/v1/payments/pending` takes the same token and answers the order's cash and transfer
tickets that can still be paid (method, amount, expiry, instructions page), newest first
(decision 16); each expiry is the earlier of the ticket's own and the order's
`payment_due_at`, since the ticket stops paying the order at the order's deadline; none when
the order is not waiting for payment; 404 `PAYMENTS_ORDER_NOT_FOUND`.

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
only records `refund_due`. A `pending` cash or transfer ticket (with its method, expiry and
instructions page) holds its order once, through `OrderPaymentConfirmations.hold_for_payment`:
the order's deadline and reservation move to the ticket's expiry plus `TICKET_GRACE` (48
hours, decision 22: a store payment can take one or two business days to be credited), and
orders emails the customer the instructions with the ticket's own expiry as the deadline
(`held_at` keeps a re-delivery from doing it again). The 48 hours count from the first ticket
(decision 19): a later ticket never moves the deadline past the earliest-held ticket's expiry
plus `TICKET_GRACE`, though it is still recorded and its email shows its own expiry, never
later than the order's deadline. A ticket already expired when its notification is processed is
marked held with no hold and no email. A pending ticket whose order was already paid or
cancelled is marked held and publishes `payments.ticket.cancel_requested` (decision 20). A
ticket page longer than its column (500 characters) is stored as none. Other statuses are only
stored.

Events (published through the outbox; other modules subscribe by name, never by import):

- `payments.notification.received`: a verified webhook named a payment. Payload: `payment_id`
  (string of digits). Payments itself processes it in the worker.
- `payments.payment.flagged`: an approved payment the owners must review. Payload:
  `payment_id` (string), `number` (int; the order is `LFI-<number>`), `reason`
  (`amount_mismatch` or `stock_gone`), `amount_cents` (int) and `currency` (string). Payments
  itself emails each active owner "Pago a revisar".

- `payments.checkout.close_requested`: an order moved from `pending_payment` to `paid` or
  `cancelled`, so its Mercado Pago link must stop taking payments (decision 18). Payload:
  `order_id` (UUID string). Payments itself handles it in the worker: it expires the
  preference in Mercado Pago, closes the stored checkout and cancels each of the order's
  `pending` cash and transfer tickets in Mercado Pago (decision 20; the row is refreshed by
  the cancelled-status notification, and a ticket paid before the cancel is `refund_due`). A
  400, 404, 409 or 422 from Mercado Pago is permanent and only logged; any other answer that
  is not a success (401, 403, 408, 429, 5xx) or a transport error retries only this event.
- `payments.ticket.cancel_requested`: a pending cash or transfer ticket was notified after its
  order was paid or cancelled, so it must be cancelled in Mercado Pago (decision 20, late
  notifications). Payload: `payment_id` (string). Payments itself handles it in the worker
  (`PUT /v1/payments/{id}` with `status: cancelled`), with the same permanent and retried
  answers as above; only this event retries.

Payments subscribes to orders' `orders.order.status_changed` (by name) only to publish
`payments.checkout.close_requested`; it makes no network call there, so it never makes orders'
emails retry.

Public API for other modules: nothing yet.

The composition root wires `module.module`.
"""
