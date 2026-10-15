---
status: resolved
plan: platform-api-foundation/004
module: platform
found: 2026-10-15
---

# The outbox backoff overflows at about attempt 47, so the 295-attempt cap is never reached

## Found while

Writing the integration tests of platform-api-foundation/004: a subscriber that always fails,
retried attempt by attempt from 0.

## What

`OutboxRelay._record_failure` (`apps/api/src/fragancia_api/shared/infrastructure/outbox.py:159`)
computes `timedelta(seconds=2**attempts)` and only then caps it with `min(..., MAX_BACKOFF)`.
- `timedelta` raises `OverflowError` once `2**attempts` seconds exceeds its range (from about
  attempt 47; "Python int too large to convert to C int" for larger values).
- The error is raised after the handler failed and before the row is updated, so the failure is
  not recorded: `attempts` stays at 46, the row stays due and every relay cycle raises again.
- The relay loop (`relay_batch`) propagates it, so the worker run fails each time.
- The cap (`MAX_ATTEMPTS = 295`, `:43`) is only reached when `attempts` is already at 294, for
  example when the verifier seeds it. Retrying from 0 never reaches it.

Pinned by two strict xfail tests in
`apps/api/tests/integration/test_outbox_failed_events.py`
(`test_a_failure_recorded_far_below_the_cap_still_backs_off_at_the_maximum`,
`test_an_always_failing_subscriber_fails_the_event_after_exactly_max_attempts`).

## Why it matters

Plan 004's goal is not met in production: a permanently failing event still retries forever (and
now also breaks the relay batch). The live check in the plan (attempts set to 294) would pass and
hide it.

## Suggested next step

Fast-lane or in-plan fix: cap the exponent before building the delta, for example
`min(2 ** min(attempts, 20), MAX_BACKOFF_SECONDS)`, or compare in seconds. The two xfail tests
then XPASS (strict) and get promoted to normal tests.

## Resolution

Fixed in plan 004 repair round 1: the exponent is capped at 9 before the `timedelta` is built (`outbox.py`), so waits are unchanged (2 ... 256 s, then 300 s) and no overflow occurs.
