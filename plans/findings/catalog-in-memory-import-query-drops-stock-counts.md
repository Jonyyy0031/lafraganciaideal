---
status: resolved
plan: inventory-stock/005
module: catalog
found: 2026-10-14
---

# The in-memory import query drops `summary.stock_counts`

## Found while

Writing the layered tests of `inventory-stock/005-initial-stock-from-excel-import`.

## What

`InMemoryCatalogImports.get` builds `CatalogImportSummary` field by field and omits the new
`stock_counts` (`apps/api/src/fragancia_api/modules/catalog/infrastructure/in_memory_imports.py:146-153`),
so every unit-test response reports `stock_counts: 0` whatever the stored summary says. The SQL
query validates the stored JSON (`sql_catalog_import_queries.py:44`) and reports it correctly
(integration test `test_the_query_reports_stock_counts`). Pinned by a strict xfail:
`test_catalog_import_confirm_http.py::test_the_in_memory_query_reports_the_stock_counts_of_the_summary`.

## Why it matters

Production is not affected. Any unit or http test that goes through the in-memory query cannot see
the count, and the confirm route's 403 check (`import_router.py`, decision 21) reads
`summary.stock_counts` from that query: with the stock fake it never fires. The http tests of plan
005 work around it with a test-local subclass.

## Suggested next step

Fast-lane fix: pass `stock_counts=summary.stock_counts` in the in-memory adapter, then drop the
xfail marker and the test-local subclass `_StoreReportingCounts`.

## Resolution

Resolved by repair round 1 of plan `inventory-stock/005-initial-stock-from-excel-import`: `InMemoryCatalogImports.get` now passes `stock_counts=summary.stock_counts`.
