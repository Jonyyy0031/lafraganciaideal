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
| 002  | Webhook and payment status | 001 | Fixes `auto_return` on http back URLs. A signed, idempotent webhook. The worker fetches the payment and moves the order to paid (approved), leaves it pending (rejected), or records it for review or refund |
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
  `pending_payment` (orders-checkout README, "Dependency notes"). **Resolved by plan 002:**
  orders' `OrderPaymentConfirmations` commits the reservation and moves the order to paid in
  one transaction, and a refused commit cancels the order and releases the reservation in that
  same transaction.
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
8. (2026-10-24) **`auto_return` is sent only when the back URL is `https`.** Mercado Pago
   refuses it otherwise (finding `payments-auto-return-needs-https-back-url`); in development
   the guest returns with the "Volver al sitio" button. Fixed as the first step of plan 002.
9. (2026-10-24) **An approved payment whose amount or currency differs from the order total
   does not mark the order paid.** The order stays pending, the payment is recorded for review,
   and the owners are emailed.
10. (2026-10-24) **A late approved payment marks the order paid if its units can still be
    sold**, even past the payment deadline. If they cannot, the order is cancelled, the payment
    is recorded for refund (plan 004), the customer gets the cancellation email and the owners
    are emailed.
11. (2026-10-24) **An approved payment for a cancelled order, or a second one for a paid order,
    is only recorded for refund** in plan 002, with no email; plan 004 refunds it and tells the
    customer and the owners.
12. (2026-10-24) **A rejected payment shows nothing new to the customer**: no email and no
    change to the order page. The order stays pending (decision 2).
13. (2026-10-26) **When a late payment finds no stock, the customer's cancellation email says
    so:** "Tu pedido … fue cancelado porque tu pago llegó cuando ya no había existencias. Te
    reembolsaremos el pago completo." (plan 002 review L2). The customer page still shows the
    cancellation as by the system.
14. (2026-10-27) **Cash in stores and SPEI are enabled**: every `ticket` method Mercado Pago
    offers (OXXO and other stores) and `bank_transfer` (SPEI). Bank counters and ATMs (`atm`)
    stay excluded.
15. (2026-10-27) **When a ticket is issued, we send our own instructions email**: amount,
    method, the deadline in Cancún time and the link to Mercado Pago's ticket. It is in addition
    to whatever Mercado Pago sends.
16. (2026-10-27) **The order page can show the pending ticket**: method, deadline and link,
    through a payments query that takes the order's token.
17. (2026-10-27) **With a ticket pending, the guest may still pay by card or issue another
    ticket.** The window extends to the ticket that expires latest (up to 48 hours), and a
    second payment is `refund_due` (plan 002).
18. (2026-10-27) **A paid or cancelled order no longer accepts payments from its old link.**
    When the order leaves `pending_payment`, payments expires its Mercado Pago preference. A
    payment that still gets through (the guest was already in the checkout) is only recorded,
    with no hold and no email.

## Delivered

## Considered and discarded

- **Interest-free installments now:** the shop pays the fee (decision 4).
- **Reviving a cancelled order when its payment arrives late:** cancelled stays final
  (decision 3).
- **Trusting the browser redirect to mark an order paid:** only verified webhooks confirm a
  payment (ADR 0004).
