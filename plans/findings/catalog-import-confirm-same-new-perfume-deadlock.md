---
status: open
module: catalog
found: 2026-10-12
---

# Two confirms that create the same new perfume can still deadlock

## Found while

Re-reviewing repair round 1 of catalog-perfumes/008, which fixed the lock order.

## What

`ConfirmCatalogImport` applies planned perfumes in ascending `perfume_id` order
(`apps/api/src/fragancia_api/modules/catalog/application/commands/catalog_imports.py:393`), so
row locks on *existing* perfumes are taken in one global order. A *new* perfume, however,
gets its id during validation, so two imports that create the same perfume identity give it
different ids. Each import therefore sees that perfume at a different place in its order.

The deadlock needs two ready imports that both:
- create the same new perfume (same brand, name and concentration), and
- update the same existing perfume.

Depending on the ids, one confirm can wait on the other's lock of the existing perfume while
the other waits on the first one's uncommitted insert into `uq_perfumes_identity`. PostgreSQL
then aborts one of them. This was reasoned by the reviewer, not reproduced.

## Why it matters

The aborted confirm answers 500 instead of the 409 `CATALOG_IMPORT_STALE` it would have got
anyway, since the identity is taken once the other one commits. The transaction rolls back,
the import stays `ready`, and a retry answers 409. No data is lost or half-written. It needs
two admins confirming overlapping files at the same moment.

## Suggested next step

Either accept it and close it, or take a small fix later. One option is to order perfumes by
their identity, `(brand_id, name_slug, concentration_id)`, instead of `perfume_id`. Another
is to map a PostgreSQL deadlock in the confirm to 409 `CATALOG_IMPORT_STALE`. The user
decides.
