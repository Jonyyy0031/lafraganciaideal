---
status: open
module: orders
found: 2026-10-26
---

# The order history's `actor` is documented as "an admin user id or `system`", but `payments` now exists

## Found while

Implementing step 7 (review repair, L2) of `payments-mercadopago/002`.

## What

Step 7 adds `PAYMENTS_ACTOR = "payments"` (`apps/api/src/fragancia_api/modules/orders/domain/order.py`),
the actor of the stock-gone cancellation in `OrderPaymentConfirmations`. Two descriptions of the
`actor` value were not in the plan's file list and still name only the admin user id and
`system`:

- `apps/api/src/fragancia_api/modules/orders/contracts.py:240` (the `AdminOrderEvent` docstring,
  which feeds `apps/api/openapi.json`, so changing it needs `uv run just openapi`);
- `apps/api/src/fragancia_api/modules/orders/infrastructure/tables.py:131` (comment on
  `order_events`).

The customer page is unaffected: `cancelled_by` maps `payments` to `"system"`.

## Why it matters

The back-office web client (phase 3) is generated from the OpenAPI description; a developer
reading it would resolve every non-`system` actor as an admin user id and could try to look up
`payments` as a user.

## Suggested next step

A fast-lane doc fix (docstring + comment + `uv run just openapi`), or fold it into the next
orders plan.
