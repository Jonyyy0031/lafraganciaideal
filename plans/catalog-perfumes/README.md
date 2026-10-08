# catalog-perfumes — Perfumes, presentations, photos and the Excel import

<!-- Initiative index. Rules: docs/harness/conventions/plans.md → "The initiative README".
     Status is NOT tracked here: run `uv run just plans-status catalog-perfumes`. -->

## Goal

The shop can publish its real catalog. Each perfume (brand + name + concentration) carries its
description, gender, olfactory family, top/heart/base notes and up to three photos. It is sold
in one or more presentations (ml), each with its own price, an optional sale price with a
validity window, and its availability: in stock, or made to order with a delivery time range.
Customers browse and filter the published catalog. Staff maintain it from the back office or
in bulk with an Excel file shaped like the sheet the shop already uses. Inventory counts, carts
and orders come in later initiatives.

## Plans

| Plan | Title | Depends on | Purpose |
| ---- | ----- | ---------- | ------- |
| 001  | Olfactory families, and renaming and archiving brands | — | Editable `OlfactoryFamily` list (create, rename, archive, restore; public and admin lists); brands gain rename, archive and restore; catalog admin routes require `catalog:manage` |
| 002  | Perfumes, presentations and the public catalog | 001 | `Perfume` aggregate with presentations (price, sale window, availability); admin CRUD, publish/hide, archive; public list with filters (brand, gender, family, text, effective price range) and detail |
| 003  | Perfume photos on S3 | 002 | Up to 3 photos per perfume in RustFS/S3: upload, order, remove; public URLs in the catalog responses |
| 004  | Excel import | 002, 003 | Downloadable `.xlsx` template; validate the whole file into a per-row report; all-or-nothing confirm that upserts perfumes and presentations and downloads image URLs to S3 |

## Dependency notes

002 references families and brands by id, so 001 must exist first. 004 creates perfumes,
presentations and photos, so it needs the write side of 002 and the photo storage of 003.

Open for plan 002 (not yet decided with the user): what an archived brand or family means for
the perfumes that use it (hidden from the storefront? blocked for new perfumes?).

## Decisions with the user

1. (2026-10-08) This initiative follows identity and comes before inventory, orders and
   payments: everything else needs perfumes and presentations to point at.
2. (2026-10-08) The client's current sheet has the columns Marca, Nombre, ML, Precio, Imagen
   1–3, Notas de salida, Notas de corazón, Notas de fondo, Familia olfativa (a dropdown) and
   Descripción, with **one row per presentation**: a perfume sold in 50 ml and 100 ml takes two
   rows that repeat the perfume data. The catalog is not yet kept that way, so the import
   template may add columns.
3. (2026-10-08) A perfume is identified by **brand + name + concentration** ("Versace Eros EDT"
   and "Versace Eros EDP" are two perfumes, each with its own notes and photos). A presentation
   is identified by its perfume + ml.
4. (2026-10-08) Perfume fields beyond the sheet: **gender** (fixed list in code: women, men,
   unisex), **concentration** (fixed list in code, e.g. EDP, EDT, Parfum, Cologne, Extrait) and
   **published/hidden**.
5. (2026-10-08) The **olfactory family** comes from a closed list that the back office edits
   (a table, not a code enum). Top, heart and base notes are free text lists.
6. (2026-10-08) Up to **3 photos per perfume** (not per presentation).
7. (2026-10-08) Prices are final prices to the customer in **MXN with VAT included**, stored
   as integer cents. No tax breakdown for now.
8. (2026-10-08) A presentation may have an optional **sale price with a start and end date**;
   the storefront shows the regular price struck through while the window is active.
9. (2026-10-08) **Availability is per presentation**: in stock (the inventory initiative
   counts units later) or made to order with a delivery time **range in days (min–max)**, e.g.
   "entrega en 7 a 10 días".
10. (2026-10-08) Nothing in the catalog is deleted: perfumes, presentations, brands and
    families are **archived**.
11. (2026-10-08) Brands can be **renamed and archived** (part of plan 001).
12. (2026-10-08) The public catalog filters by **brand, gender, olfactory family, text
    (accent-insensitive: "lancome" finds "Lancôme") and price range**. The price that counts is
    the **effective** one: the sale price while its window is active, the regular price
    otherwise.
13. (2026-10-08) The catalog is also loaded from an **`.xlsx`** file (the shop works in
    Excel/Sheets), with a downloadable template, one row per presentation, and extra columns
    for gender, concentration, availability, delivery days, sale price and window, and
    published.
14. (2026-10-08) The import **validates the whole file first** and returns a per-row report;
    if any row has an error nothing is imported; with no errors the admin confirms. It
    **upserts**: new rows create, existing ones (brand + name + concentration + ml) update, so
    the file also serves for bulk price changes.
15. (2026-10-08) A brand or family in the file that does not exist is an **error**, not
    created on the fly, to catch typos.
16. (2026-10-08) Images in the file are **public URLs** that the import downloads to S3.
17. (2026-10-08) **Owner and staff** both manage the whole catalog, including the import
    (`catalog:manage`, which both roles already have).
18. (2026-10-08) Four plans: 001 families + brand rename/archive, 002 perfumes +
    presentations + public catalog, 003 photos, 004 import.
19. (2026-10-08) The family list starts **seeded** with nine families: Amaderada, Floral,
    Oriental, Cítrica, Aromática, Gourmand, Acuática, Chipre, Fougère. The owner adds or
    archives the rest from the panel.
20. (2026-10-08) Plan 001 approved by the user, with the seed.

## Delivered

## Considered and discarded

- **One row = one perfume (a single size)**: the shop sells several sizes of the same perfume
  (decision 2).
- **Availability per perfume**: one perfume can be in stock in 100 ml and made to order in
  200 ml (decision 9).
- **Families as a fixed list in code**: the user wants to edit them from the panel (decision 5).
- **Notes as a closed catalog**: more capture and import work; free text is enough for now.
- **Sale price without dates**: the user wants a validity window (decision 8).
- **Importing the valid rows and reporting the rest**: a half-applied price list is worse than
  none (decision 14).
- **Creating unknown brands during the import**: typos would create duplicate brands
  (decision 15).
- **CSV import**: accents and separators break when exported from Excel (decision 13).
- **Images in a ZIP next to the file**: more work for the client and for us (decision 16).
- **Deleting rows**: future orders will reference perfumes and presentations (decision 10).
