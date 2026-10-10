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
| 002  | Editable concentrations | 001 | `Concentration` list with a name and an abbreviation (create, update, archive, restore; public and admin lists), seeded with the five classic concentrations |
| 003  | Perfumes and presentations in the back office | 001, 002 | `Perfume` aggregate with its presentations (price, sale window, availability); admin create, edit, publish/hide, archive/restore, add/edit/archive presentations; admin list and detail |
| 004  | The public catalog | 003 | Storefront list with filters (brand, gender, family, text, effective price range), sorting, "from" price, and detail by slug |
| 005  | Bound the page number of every list | 004 | Shared `MAX_PAGE` with `le=` on the five paginated list routes, so a huge `page` is a 422 instead of an OFFSET overflow (finding `catalog-unbounded-page-offset-overflow`) |
| 006  | Perfume photos on S3 | 003 | Up to 3 photos per perfume in RustFS/S3: upload, order, remove; public URLs in the catalog responses |
| 007  | Excel import | 003, 006 | Downloadable `.xlsx` template; validate the whole file into a per-row report; all-or-nothing confirm that upserts perfumes and presentations and downloads image URLs to S3 |

## Dependency notes

003 references brands, families (001) and concentrations (002) by id. 004 reads what 003
writes. 006 creates perfumes, presentations and photos, so it needs the write side of 003 and
the photo storage of 005.

Plans 002–005 were renumbered on 2026-10-09 (decision 21), and plans 003–006 again on the
same day (decision 35). Photos and the import moved once more, to 006 and 007, on 2026-10-10
(decision 45). Each time, none of their files existed yet.

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
21. (2026-10-09) **Concentrations are an editable list** (a table with CRUD, like the
    families), not a fixed list in code, so new ones such as Body Mist need no deploy. This
    replaces the "fixed list in code" part of decision 4. They get their own plan, 002, before
    the perfumes: perfumes move to 003, photos to 004 and the import to 005.
22. (2026-10-09) When a **brand** is archived, its perfumes are hidden from the storefront
    without changing their own state, and they come back when the brand is restored. When a
    **family** is archived, its perfumes stay visible; only the family filter loses it.
23. (2026-10-09) Only **active** brands, families and concentrations can be assigned when
    creating or editing a perfume (422 otherwise). Perfumes that already have an archived one
    keep it.
24. (2026-10-09) A perfume can be **published only with at least one active presentation**.
    Photos are not required.
25. (2026-10-09) **Gender stays a fixed list in code**: women, men, unisex.
26. (2026-10-09) A concentration has a **name and an abbreviation**, both unique ("Eau de
    Toilette" / "EDT"). The storefront shows the name; the perfume URL uses the abbreviation
    (`/perfumes/versace-eros-edt`).
27. (2026-10-09) The concentration list starts **seeded** with the five classics: Eau de
    Cologne (EDC), Eau de Toilette (EDT), Eau de Parfum (EDP), Parfum (Parfum), Extrait de
    Parfum (Extrait).
28. (2026-10-09) Sale price window: **start and end are both optional**. No start means valid
    now; no end means valid until removed; with both, end > start. A sale price is always
    lower than the regular price.
29. (2026-10-09) The storefront text search covers **name, brand and notes**, ignoring accents
    and case ("vainilla" finds perfumes with a vanilla note).
30. (2026-10-09) Storefront sort options are **name (default: brand + name), price ascending,
    price descending and newest**. Each perfume shows a **"from" price**: the lowest effective
    price among its active presentations. The price range filter uses the same price, and the
    detail page shows every presentation.
31. (2026-10-09) A perfume has a **readable slug** in its storefront URL:
    brand + name + concentration abbreviation.
32. (2026-10-09) Validation limits:
    - ml: an integer from 1 to 1000.
    - Price: greater than 0, up to $100,000 MXN.
    - Delivery days: 1 to 90, with min ≤ max.
    - Notes: at most 10 per level, 40 characters each.
    - Description: up to 2,000 characters.
33. (2026-10-09) When a **concentration** is archived, its perfumes **stay visible**, as with
    families: archiving "EDT" must not hide half the catalog.
34. (2026-10-09) Plan 002 approved by the user.
35. (2026-10-09) The perfume plan is **split in two**:
    - **003** is the back office: perfumes and presentations.
    - **004** is the public catalog: filters, search, sorting, "from" price and detail.

    Photos move to 005 and the import to 006.
36. (2026-10-09) Archiving the **last active presentation of a published perfume** is
    refused (409): the perfume has to be hidden first. A published perfume always has
    something to sell (decision 24), and nothing changes behind the admin's back.
37. (2026-10-09) **Stable perfume URLs.** The slug is computed when the perfume is created or
    edited, and then stored. Renaming its brand or concentration does not change existing
    URLs, which keeps SEO and shared links intact. Editing the perfume recomputes the slug.
38. (2026-10-09) The storefront's **"newest" sort uses the first publication date**, not the
    creation date.
39. (2026-10-09) The user approved plan 003, including two rules it proposes:
    - An **archived perfume is read-only** except restore and hide.
    - The **description is optional**. An empty note is invalid, and duplicate notes are kept.
40. (2026-10-09) **Old perfume URLs redirect.** When a perfume's slug changes, the old slug is
    kept in a history table and still finds the perfume. The response carries the current
    slug, so the web answers a 301 to it. This protects SEO and links shared over WhatsApp.
41. (2026-10-09) Text search is a **"contains" match with PostgreSQL `unaccent`**: "lanc"
    finds "Lancôme" and "vain" finds "Vainilla". Typo tolerance (`pg_trgm`) can come later.
42. (2026-10-09) The public detail shows **only active presentations**: the current price,
    the regular price struck through while a sale is active (with its end date), the
    availability and the delivery days.
43. (2026-10-09) The public list shows **24 perfumes per page by default, 48 at most**. Both
    divide evenly into 2-, 3- and 4-column grids.
44. (2026-10-09) The user approved plan 004 as proposed, with two consequences:
    - **No concentration filter**, since decision 12 doesn't list one.
    - **No search or price indexes** until a measurement calls for them.
45. (2026-10-10) The finding `catalog-unbounded-page-offset-overflow` is fixed by a **mini-plan
    005** on the plan-004 branch, not through the fast lane: bounding `page` changes the
    OpenAPI contract and touches more than three files. Photos move to 006 and the import to
    007.
46. (2026-10-10) The user approved plan 005 (`MAX_PAGE = 10_000`).

## Delivered

## Considered and discarded

- **One row = one perfume (a single size)**: the shop sells several sizes of the same perfume
  (decision 2).
- **Availability per perfume**: one perfume can be in stock in 100 ml and made to order in
  200 ml (decision 9).
- **Families as a fixed list in code**: the user wants to edit them from the panel (decision 5).
- **Concentrations as a fixed list in code**: replaced by an editable list (decision 21).
- **Gender as an editable list**: three values that hardly change (decision 25).
- **One storefront card per presentation**: a perfume shows once, with its "from" price
  (decision 30).
- **Perfume URLs by id**: worse for SEO, and the storefront renders on the server (decision
  31).
- **Notes as a closed catalog**: more capture and import work; free text is enough for now.
- **Sale price without dates**: the user wants a validity window (decision 8).
- **Importing the valid rows and reporting the rest**: a half-applied price list is worse than
  none (decision 14).
- **Creating unknown brands during the import**: typos would create duplicate brands
  (decision 15).
- **CSV import**: accents and separators break when exported from Excel (decision 13).
- **Images in a ZIP next to the file**: more work for the client and for us (decision 16).
- **Deleting rows**: future orders will reference perfumes and presentations (decision 10).
