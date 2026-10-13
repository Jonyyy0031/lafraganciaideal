# inventory-stock — Stock per presentation, movements, reservations and low-stock alerts

<!-- Initiative index. Rules: docs/harness/conventions/plans.md → "The initiative README".
     Status is NOT tracked here: run `uv run just plans-status inventory-stock`. -->

## Goal

The shop knows how many units of each in-stock presentation it has, and why the number
changed.
- **Staff** record stock movements from the back office: restock, adjustment, loss, return
  and physical count. Each has a reason and a history.
- **The storefront** shows a presentation as available, "¡Últimas piezas!" or "Agotado",
  without revealing the exact count.
- **Checkout** (the orders initiative) reserves units while a payment is in progress, then
  commits or releases them, so the last bottle is never sold twice.
- **Owners** get an email when a presentation drops to its low-stock threshold, and the panel
  lists everything that is low.
- **The initial counts** come in through the catalog Excel import.

## Plans

| Plan | Title | Depends on | Purpose |
| ---- | ----- | ---------- | ------- |
| 001  | Stock ledger in the back office | — | New `inventory` module: stock per in-stock presentation, movements with reason and history, low-stock thresholds (global + per presentation) and the low-stock list; `inventory:manage` permission; catalog facade for presentation lookups |
| 002  | Stock status on the storefront | 001 | Public catalog responses show `available` / `last_units` / `sold_out` for in-stock presentations; a sold-out presentation cannot be bought |
| 003  | Reservations | 001 | Inventory public API to reserve, commit and release units with an expiry; a job releases expired reservations; available = on hand − reserved |
| 004  | Low-stock email to owners | 001 | One email to active owners when a presentation crosses its threshold downwards (not repeated until it goes back up) |
| 005  | Initial stock from the Excel import | 001 | Optional "Existencias" column in the catalog import; confirming records a physical-count movement |

## Dependency notes

- **001 comes first:** every other plan reads the ledger it creates.
- **002–005 depend only on 001**, so they can go in any order. The orders initiative needs 003
  before checkout exists.
- **Cross-module calls** go through ports and facades, never table reads:
  - inventory → catalog, to look up a presentation (001);
  - catalog → inventory, for the storefront stock status (002) and for the import's counts
    (005).
- **Recipients:** plan 004 needs the owners' emails from identity, through an identity facade.

## Decisions with the user

1. (2026-10-13) **Units are counted per presentation**, only for presentations sold "in
   stock". Made-to-order presentations have no unit count.
2. (2026-10-13) **At 0 available units the storefront shows the presentation as "Agotado".**
   It stays visible with its price but cannot be bought. A perfume stays listed while another
   of its presentations can be bought.
3. (2026-10-13) **Stock is reserved when the checkout is created, with a timer.**
   - An approved payment deducts the units.
   - An expired or rejected payment releases them.
4. (2026-10-13) **Reservation timers depend on the payment method:**
   - 30 minutes for card payments;
   - up to 72 hours for cash (OXXO) or transfer payments, while Mercado Pago keeps the payment
     pending, which is when its ticket expires.

   Inventory takes the expiry from the caller; the orders initiative applies this rule.
5. (2026-10-13) **Stock changes only through movements**, each with a kind, a quantity, a
   reason, who made it and when:
   - kinds: restock, adjustment, loss, return and physical count;
   - the on-hand number follows from the movements;
   - there is no free "edit the number" field.
6. (2026-10-13) **Customers never see the exact count.**
   - At or below the low-stock threshold the storefront says "¡Últimas piezas!".
   - Above it, the storefront says "Disponible".
7. (2026-10-13) **Initial counts come from an optional "Existencias" column** in the catalog
   Excel import. Confirming records a physical-count movement that sets the stock to that
   number. Movements can also be recorded one by one from the panel.
8. (2026-10-13) **Low stock is shown in a panel list and sent by email.**
9. (2026-10-13) **The low-stock threshold is global, and each presentation can override it.**
   It is the same threshold for the alert, the list and "¡Últimas piezas!".
10. (2026-10-13) **The low-stock email:**
    - it goes to active users with the owner role;
    - it is sent once, when a presentation's stock drops to or below its threshold;
    - it is not repeated until the stock goes back above the threshold.
11. (2026-10-13) **A new `inventory:manage` permission** controls stock. Owner and staff both
    get it today, and it can be separated from `catalog:manage` later without code changes.
12. (2026-10-13) **Plan 001 repair round.** The user chose to fix all three advisory Low
    findings from the review, plus the recipe finding, before `done`:
    - **One low-stock rule:** there is a single low-stock rule in the domain, and queries use
      it.
    - **App clock:** the app clock stamps the placeholder stock row.
    - **Locked catalog check:** the catalog check happens inside the movement's transaction,
      with the presentation row locked `FOR SHARE`.
    - **Recipe:** the new-module recipe documents the full facade wiring.

## Delivered

## Considered and discarded

- **Editing the stock number directly:** no history to audit discrepancies (decision 5).
- **Hiding sold-out presentations:** customers lose the price and the reference (decision 2).
- **Switching sold-out presentations to made to order:** it would invent delivery times.
- **Reserving only when the payment is approved:** two customers could pay for the last unit
  (decision 3).
- **A single 30-minute timer for every payment:** a customer paying at OXXO would lose the
  bottle before paying (decision 4).
- **Showing the exact count:** the user prefers "¡Últimas piezas!" (decision 6).
- **A daily summary email, or a fixed address:** one email on crossing the threshold, to the
  owners (decision 10).
