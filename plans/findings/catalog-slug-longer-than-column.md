---
status: resolved
module: catalog
found: 2026-10-08
plan: catalog-perfumes/001
---

# A valid catalog name can produce a slug longer than the 100-character slug column

## Found while

Reviewing catalog-perfumes/001 (olfactory families and brand maintenance).

## What

`clean_name` (`apps/api/src/fragancia_api/modules/catalog/domain/naming.py:59-64`, moved from
`brand.py` unchanged) bounds the **name** to 80 characters, but `slugify` applies NFKD, which
expands compatibility characters into several ASCII letters (U+2167 "Ⅷ" becomes "VIII";
"ﬃ" becomes "ffi"). The slug columns are `String(100)`
(`apps/api/src/fragancia_api/modules/catalog/infrastructure/tables.py:13,26`). The domain
never bounds the slug length, and the repositories catch only `IntegrityError`, not
`DataError`. Reasoned from the Unicode decomposition tables; not reproduced against the
running app.

## Why it matters

An admin who submits 80 "Ⅷ" characters as a brand or family name passes validation. The slug
then has 320 characters, PostgreSQL rejects the INSERT/UPDATE with
`StringDataRightTruncation`, and the API answers 500 instead of 422
`CATALOG_*_NAME_INVALID`. This affects brand creation, which predates plan 001. Plan 001
adds the same gap to brand rename, family create and family rename. Only admins can hit it,
and only with unusual input.

## Suggested next step

Fast-lane fix: `clean_name` should also reject a name whose slug is longer than the column
(100), with a regression test in the domain tests. Or accept and close.

## Resolution

2026-10-08, fast-lane fix after plan 001 was done.

- **Reproduced** against the running API: an 80 × "Ⅷ" name gave 500 `INTERNAL_ERROR` on
  `POST /admin/brands` and on `POST /admin/olfactory-families`.
- **Fix:** `clean_name` now also rejects a name whose slug is longer than `SLUG_MAX_LENGTH =
  100`, so the name is refused before it reaches the database.
- **Regression test:**
  `tests/unit/catalog/test_olfactory_family_domain.py::test_clean_name_rejects_a_name_whose_slug_outgrows_the_slug_column`.
- **Verified:** the same requests, and a family rename, now return 422
  `CATALOG_*_NAME_INVALID`. `uv run just check` is green (706 passed).
