---
status: resolved
module: platform
found: 2026-10-15
plan: platform-api-foundation/004
---

# The outbox relay retries a failing event forever, with no attempt cap or dead letter

## Found while

Reviewing inventory-stock/004. Its low-stock email handler sends one email per owner from one
event.

## What

`OutboxRelay._record_failure` (`apps/api/src/fragancia_api/shared/infrastructure/outbox.py:132-151`)
increments `attempts` and sets `available_at` to now + `min(2**attempts s, MAX_BACKOFF)`.
`MAX_BACKOFF` is 5 minutes (`:38`).
- There is no maximum number of attempts, no "failed" state and no alert.
- ADR 0006 only says that delivery is at-least-once, with retries.

## Why it matters

An event whose subscriber always fails is retried every 5 minutes, forever.
- **Repeated side effects.** If a subscriber does several side effects before the one that
  fails, each retry repeats them.
  - Example: `SendLowStockEmail` (`modules/inventory/application/handlers/low_stock_email.py`)
    sends to the owners in name order.
  - If the SMTP server permanently refuses the second owner's address, the first owner gets the
    same low-stock email every 5 minutes, indefinitely.
- **Silent poison rows.** The failing row stays pending forever, and nobody is told.

## Suggested next step

A platform plan:
- cap the attempts (or the age) of an outbox row;
- move a row past the cap to a failed state that is logged and visible;
- decide per handler whether partial side effects need their own idempotency.

Or accept it and close this finding.

## Resolution

Resolved by platform-api-foundation/004: retries stop after 295 attempts (about 24 h), the event
is marked failed (`failed_at`), kept with its error and logged, and owners can list and retry it
from the panel API.
