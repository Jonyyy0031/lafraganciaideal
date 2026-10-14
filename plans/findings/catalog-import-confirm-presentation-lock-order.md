---
status: open # open → deferred | planned | resolved | discarded (the user decides)
module: catalog
found: 2026-10-14
---

# A catalog import confirm writes presentations of several perfumes out of id order, so it can deadlock with a multi-perfume reserve

## Found while

Re-reviewing repair round 1 of inventory-stock/003 (reservations), which fixed
`catalog-perfume-save-presentation-lock-order.md` for a single perfume save.

## What

- `SqlPerfumeRepository.save` now upserts one perfume's presentations in `id` order
  (`apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_perfume_repository.py:255-257`).
  That orders the rows *within* one perfume only.
- `ConfirmCatalogImport` saves every updated perfume of the import in ONE transaction, in
  identity order `(brand, name slug, concentration)`
  (`apps/api/src/fragancia_api/modules/catalog/application/commands/catalog_imports.py:422-430`,
  `_update` → `self._perfumes.save` at `:601`). Every save upserts all presentations of its
  perfume, which row-locks each existing one. Across perfumes the presentation writes therefore
  follow identity order, not `presentation_id` order.
- `ReserveStock` share-locks the presentations of all its lines in ascending `presentation_id`
  order, whatever perfume they belong to
  (`apps/api/src/fragancia_api/modules/inventory/application/commands/reservations.py:89-95`).
- The comments added by the repair state the opposite: "every writer of several presentation
  rows must use the same order" (`sql_perfume_repository.py:255-256`) and "it cannot form a
  wait cycle with a multi-line reserve" (`reservations.py:7-8`). The plan's repair spec also
  says no other catalog writer updates several presentation rows.

Reproduced on PostgreSQL by the reviewer with a scratch test (not committed): one transaction
saving perfume P2 then P1 through `SqlPerfumeRepository` (as the confirm loop does), against a
reserve of `{a (P1), b (P2)}` with `b < a`, paused after its first share lock. Result:
`psycopg.errors.DeadlockDetected`. With the saves in the other order, both finish.

## Why it matters

A checkout reserving two different perfumes races an admin confirming an import that updates
both of them. When the identity order of the two perfumes disagrees with the id order of the
two presentations (about half the time), the confirm takes the higher presentation, waits on the
lower one the reserve share-locked, and the reserve waits on the higher one. PostgreSQL aborts
one side after `deadlock_timeout`: the checkout or the confirm answers 500. Both roll back
cleanly and a retry works. It needs an import confirm at the same moment as a checkout of
perfumes in that import, so it is rare, but imports touch many perfumes at once.

## Suggested next step

The user decides:

- **Fix in catalog:** make the confirm write all its presentation rows in one global
  `presentation_id` order. One shape: lock the perfume rows in identity order as now, then
  upsert every presentation of every applied perfume sorted by id at the end (or lock them
  `FOR NO KEY UPDATE` in id order before the per-perfume saves), plus a race test like the
  scratch reproduction above. Keep the perfume-row lock first, so an admin perfume save
  (perfume row, then its presentations) keeps the same order.
- **Accept:** PostgreSQL resolves it by aborting one transaction; correct the two comments so
  they do not promise more than the single-perfume save gives.
