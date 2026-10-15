---
status: resolved
plan: inventory-stock/004
module: catalog
found: 2026-10-15
---

# The catalog row in docs/architecture.md still lists the Excel import as "Next"

## Found while

Re-reviewing inventory-stock/004 after repair round 2 (its R-L1 fixed the same kind of stale
"Next:" in the inventory row of the same table).

## What

`docs/architecture.md:62` ends the `catalog` row with "Next: Excel import". The import is
built: `plans/catalog-perfumes/007-excel-import-template-and-validation.md` and
`008-excel-import-confirm.md` are `done`, and
`apps/api/src/fragancia_api/modules/catalog/application/commands/catalog_imports.py` exists.

## Why it matters

Docs only. A reader (or an agent planning work) takes the import for unbuilt and may plan it
again or not look for the existing code.

## Suggested next step

A docs-only edit of that row (replace "Next: Excel import" with the import as built), in the
next catalog plan or a small docs commit. The user decides.

## Resolution

Fixed on 2026-10-15 by the main session while closing the plan 004 review (docs only): the
catalog row now lists the Excel import as built. The same edit fixed R2-L1 on the inventory row.
