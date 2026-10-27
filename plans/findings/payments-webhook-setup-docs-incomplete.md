---
status: planned # open → deferred | planned | resolved | discarded (the user decides)
module: payments
found: 2026-10-27
plan: payments-mercadopago/003
---

# The webhook setup docs miss which panel tab, event and secret a test account needs

## Found while

Verifying `payments-mercadopago/002` against the real Mercado Pago sandbox with the developer's
own test account (decision 1).

## What

`apps/api/.env.example:32-42` (and the README section added by plan 002) say to use "the
TEST- credentials" and "the secret from the Webhooks page", with the event "Pagos". What the
live run showed on 2026-10-27:

- The test seller account's credentials start with `APP_USR-`, not `TEST-`, and its payments
  come back with `live_mode: true`.
- The panel's Webhooks page has two tabs ("Modo de prueba", "Modo productivo"), each with its
  own URL and its **own secret**. The payment event is listed as **"Pagos (legacy)"**; "Order
  (Mercado Pago)" is the Orders API and does not apply.
- Real notifications for this account's payments arrived signed with the **"Modo de prueba"**
  secret: with the "Modo productivo" secret in `MERCADOPAGO_WEBHOOK_SECRET`, Mercado Pago's two
  deliveries for payment `186361429202` got 401; with the test-mode secret every later delivery
  passed. The panel's "Simular notificación" signs with the secret of the tab it is sent from.
- The checkout's `sandbox_init_point` and the `TEST-` wording in the settings comment
  (`apps/api/src/fragancia_api/config.py:65-66`) carry the same outdated assumption.

## Why it matters

The next developer (or the move to the client's production credentials) follows the docs, puts
the other tab's secret in `.env`, and every real notification is refused with 401 while the
panel's simulation looks fine. Orders then stay pending until the overdue job cancels them.

## Suggested next step

A doc-only fast-lane fix (or a step in plan 003): describe both tabs, the "Pagos (legacy)"
event, that the secret must be the one of the tab whose credentials are in use (the test tab
for a test seller account), and drop the `TEST-` prefix claim.
