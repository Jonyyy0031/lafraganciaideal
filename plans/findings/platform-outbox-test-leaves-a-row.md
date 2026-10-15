---
status: open # open → deferred | planned | resolved | discarded (the user decides)
module: platform
found: 2026-10-15
---

# The outbox integration tests leave their last `platform.outbox` row in fragancia_test

## Found while

Reviewing inventory-stock/004. The tester reported it, and the reviewer reproduced it.

## What

The autouse fixture `clean_outbox` (`apps/api/tests/integration/conftest.py:27-30`) runs
`TRUNCATE platform.outbox` before each test. Nothing cleans up after the test, so the last test
that publishes an event leaves its row.
- Before a full `uv run just test-integration` on 2026-10-15, `platform.outbox` in
  `fragancia_test` had 0 rows. After it, it had 1 row named `tests.something.happened`.
- The row comes from `apps/api/tests/integration/test_transactions_and_outbox.py` (the
  `SomethingHappened` event).
- The reviewer counted it (1) and deleted it by name.

## Why it matters

The test database should be empty after a run. Every review or test phase that checks it finds
this row and has to tell it apart from a leak by the plan under review.

## Suggested next step

A test-only fast-lane fix: make `clean_outbox` a yield fixture that truncates after the test as
well as before it. Or accept it and close this finding.
