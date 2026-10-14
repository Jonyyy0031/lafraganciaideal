---
status: open # open → deferred | planned | resolved | discarded (the user decides)
module: catalog
found: 2026-10-14
---

# A perfume save locks its presentations in `ml` order, so it can deadlock with a multi-presentation reserve

## Found while

Reviewing inventory-stock/003 (reservations).

## What

- `SqlPerfumeRepository.get_for_update` loads the presentations ordered by `ml`
  (`apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_perfume_repository.py:224`).
  `save` upserts every one of them in that order with `ON CONFLICT DO UPDATE` (`:255`), which
  takes a row lock on each existing presentation row.
- `ReserveStock` share-locks the catalog presentations of a reservation one by one, in
  ascending `presentation_id` order
  (`apps/api/src/fragancia_api/modules/inventory/application/commands/reservations.py:89`). It
  goes through `SqlPresentationDirectory.find_for_update`, which is `FOR SHARE OF presentations`
  (`apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_presentation_directory.py:70`).
  `RecordImportedCounts` (plan 005) does the same, so the pattern predates plan 003. Checkout
  makes it a hot path.
- Two presentations of one perfume can have an `id` order that differs from their `ml` order.
  Ids are UUIDv7, so this happens when the larger size was created first. The two transactions
  then take the same two rows in opposite orders.

This is reasoned from PostgreSQL row-lock semantics: `FOR SHARE` conflicts with the lock that
an upsert's `DO UPDATE` takes. It was not reproduced by a test.

## Why it matters

Perfume P has a 100 ml presentation (id X, created first) and a 50 ml one (id Y > X). A checkout
reserves both while an admin saves P (any edit of the perfume or of a presentation):

1. The reserve share-locks X.
2. The save upserts Y (50 ml comes first) and then waits for X.
3. The reserve waits for Y.

PostgreSQL detects the deadlock after `deadlock_timeout` and aborts one side with an unexpected
error. Either the checkout fails (500) or the admin save does.

## Suggested next step

Options (the user decides):

- **Fast-lane fix in catalog:** upsert the presentations in `save` in `id` order (the read
  order of `get_for_update` can stay by `ml`), plus a race test of a perfume save against a
  two-line reserve.
- **Accept:** PostgreSQL resolves it by aborting one transaction.
