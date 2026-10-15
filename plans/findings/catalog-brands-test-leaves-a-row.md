---
status: open # open → deferred | planned | resolved | discarded (the user decides)
module: catalog
found: 2026-10-15
---

# The catalog brand repository tests leave their last brand row in fragancia_test

## Found while

Re-reviewing repair round 2 of inventory-stock/003. It was seen twice: in the round-1 re-review
(2026-10-14), and again after a full `uv run just test-integration` on 2026-10-15.

## What

The autouse fixture `clean_brands` (`apps/api/tests/integration/catalog/test_sql_brands.py:20-23`)
runs `TRUNCATE catalog.brands CASCADE` before each test. Nothing cleans up after the test, so
the last test that runs leaves its rows.
- After a full integration run, `fragancia_test` had one `catalog.brands` row: slug `chanel`,
  `created_at` 2026-01-01 00:00 UTC.
- It comes from `::test_unique_violation_is_an_err_and_keeps_the_transaction_usable` (`:105`).
- Each time, the reviewer counted the row (1) and deleted it by slug and `created_at`.

## Why it matters

The test database should be empty after a run.
- A leftover brand can break another suite that assumes no brands or creates `chanel`. Whether
  it does depends on the test order.
- Every review or test phase has to clean it up by hand.

## Suggested next step

A test-only fast-lane fix: make `clean_brands` a yield fixture that truncates after the test as
well as before it, like the inventory `conftest.py` cleanup. Or accept it and close this finding.
