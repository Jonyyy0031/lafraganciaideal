# payments-mercadopago — Paying orders with Mercado Pago

<!-- Initiative index. Rules: docs/harness/conventions/plans.md → "The initiative README".
     Status is NOT tracked here: run `uv run just plans-status payments-mercadopago`. -->

## Goal

A guest pays their order with Mercado Pago Checkout Pro (ADR 0004).
- **Card payments.** The guest is sent to Mercado Pago from their order. A verified webhook
  moves the order to paid, which sells the reserved units and sends the emails.
- **Rejected payments.** A rejected card leaves the order pending, so the guest can try again
  while the payment window lasts.
- **OXXO and SPEI.** They hold the units for 48 hours while the guest pays the ticket.
- **Refunds.** Cancelling a paid order refunds it automatically. So does a payment that
  arrives after the order was cancelled.

## Plans

| Plan | Title | Depends on | Purpose |
| ---- | ----- | ---------- | ------- |
| 001  | Payments module and checkout redirect | — | New `payments` module with the `PaymentGateway` port and its Mercado Pago adapter. From the order page, the guest gets a Checkout Pro link for a pending order (cards only, one installment), reused while it is valid |
| 002  | Webhook and payment status | 001 | A signed, idempotent webhook. The worker fetches the payment and moves the order to paid (approved), or leaves it pending so the guest can retry (rejected) |
| 003  | OXXO and SPEI | 002 | Cash and transfer enabled. A pending ticket extends the reservation and the payment window to its 48-hour expiry, and the customer gets the payment instructions |
| 004  | Automatic refunds | 002 | Cancelling a paid Mercado Pago order refunds it through the API. A payment approved for an order already cancelled is refunded, and the customer and owners are told |

## Dependency notes

- **`orders` registers before `payments` in `MODULES`.** Payments reads the order and moves it
  to paid only through facades exported by `orders` (`docs/recipes/new-module.md` §5). Orders
  never imports payments.
- **Orders' manual "mark paid" (plan 004 of orders-checkout) stays** for payments made outside
  Mercado Pago.
- **The overdue job (orders-checkout 004).** It stops its run at an order whose release is
  refused. Plan 002 must keep a payment-committed reservation from leaving an order
  `pending_payment` (orders-checkout README, "Dependency notes").
- **One order can collect more than one payment.** A Checkout Pro preference can be paid more
  than once, and two concurrent requests can create two preferences for one order.
  - Plan 002 matches payments by `external_reference` (the order id).
  - A second approved payment for an order that is already paid must be detected, and refunded
    by plan 004.

  (Plan 001 review, info.)
- **Live verification needs a Mercado Pago test account:**
  - test credentials (`TEST-…` access token, webhook secret);
  - a buyer test user;
  - a public tunnel to the local API for webhooks.

  The user is creating it (decision 1). Until then, every plan is verified against a local
  fake of the Mercado Pago API, and the sandbox run stays NOT VERIFIED.

## Decisions with the user

1. (2026-10-22) **Development uses the developer's own Mercado Pago test account.** The
   client's production credentials come later and only change the settings.
2. (2026-10-22) **A rejected payment leaves the order pending.** The guest can pay again from
   the order page, with another card or method, while the payment window lasts.
3. (2026-10-22) **A payment that arrives for an order already cancelled is refunded in full,
   automatically.** The order stays cancelled, and the customer and the owners are told.
4. (2026-10-22) **No interest-free installments (MSI) for now.** Payments are in one
   installment, and MSI may be configured later.
5. (2026-10-22) **An OXXO or SPEI ticket expires 48 hours after it is issued.** Until then the
   order stays pending, and its units stay reserved (orders-checkout decision 18).
6. (2026-10-22) **Four plans:** the module and redirect, the webhook, OXXO and SPEI, then
   refunds.
7. (2026-10-22) **Mercado Pago sends the guest back to `{STOREFRONT_URL}/pedido/pago`.**
   - Mercado Pago appends query parameters to that URL, so the private token cannot ride in it.
   - The web page keeps the token in `sessionStorage` before redirecting.
   - The return never confirms a payment; only the webhook does (ADR 0004).

## Delivered

## Considered and discarded

- **Interest-free installments now:** the shop pays the fee (decision 4).
- **Reviving a cancelled order when its payment arrives late:** cancelled stays final
  (decision 3).
- **Trusting the browser redirect to mark an order paid:** only verified webhooks confirm a
  payment (ADR 0004).
