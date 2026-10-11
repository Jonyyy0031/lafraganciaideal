---
status: resolved
plan: catalog-perfumes/007
module: catalog
found: 2026-10-11
---

# Formatted empty rows make a small import fail with TOO_MANY_ROWS

## Found while

Verifying catalog-perfumes/007, after repair round 1. The reviewer had raised the case as a
non-blocking note.

## What

`OpenpyxlSheetReader` counts every physical row after the header against
`max_rows * 2` (`apps/api/src/fragancia_api/modules/catalog/infrastructure/xlsx_sheets.py:116-121`).
That count includes rows with no values at all. Repair item M2 of plan 007 asked for this,
to bound the scan of blank rows.

openpyxl yields a row for every row that has a `<c>` element, and a styled empty cell has one.

Reproduced live: an import with 3 data rows, plus a number format on column A down to row
5000, ended `invalid` with `CATALOG_IMPORT_TOO_MANY_ROWS`.

## Why it matters

Hand-edited workbooks often have formatting applied to a large range, for example a price
column formatted down to row 10,000. The client would be told the file has too many rows
when it has a few dozen, with no hint of the real cause. The ZIP limits of the same repair
(1,000 entries, 50 MB uncompressed) already bound memory and time, so the extra row-scan
limit buys little.

## Suggested next step

A small repair in plan 007, before it is marked done:
- Raise the scan limit to the Excel maximum (1,048,576 rows), or drop it and rely on the ZIP
  limits.
- Add a regression test with a styled range.

This needs a user decision because it changes repair item M2.
