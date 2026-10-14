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
13. (2026-10-14) **A perfume whose presentations are all sold out stays in the public list**,
    marked "Agotado". This applies when it has nothing made to order.
14. (2026-10-14) **The "from" price and the price filters and sort count only what can be
    bought:**
    - presentations with available units, or made to order;
    - when a perfume has nothing buyable, its lowest price overall.
15. (2026-10-14) **The storefront learns the stock through an event plus a copy in the
    catalog:**
    - every stock change publishes `inventory.stock.changed` through the outbox;
    - the catalog keeps a copy on each presentation (available units and effective
      threshold), so filters and sorting stay in SQL;
    - the copy lags the panel by the outbox relay, about 2 s.
16. (2026-10-14) **The public list gains an optional "Solo disponibles" filter**
    (`in_stock=true`), which hides perfumes with nothing to buy.
17. (2026-10-14) **Plan 002 repair round.** The review found M1: a global threshold change
    racing a movement could leave the storefront copy stale. Following the standing choice to
    fix review findings, the settings change and the movements now serialize:
    - the settings row is locked `FOR UPDATE` by a settings change and `FOR SHARE` by
      movements and threshold changes;
    - stock items are locked in `presentation_id` order before their events are stamped.

    L3, the parallel-run guard on the migration test, is also added. L1 (wall-clock ordering)
    and L2 (the default 3 copied into the catalog) are accepted as is.
18. (2026-10-14) **A number in "Existencias" always sets the stock**, whether or not the
    presentation was counted before:
    - it records a physical-count movement;
    - an empty cell leaves the stock untouched;
    - the validation summary says how many counts the confirm will record.
19. (2026-10-14) **"Existencias" on a "Sobre pedido" row is a validation error** on that cell.
    Made-to-order presentations carry no units (decision 1).
20. (2026-10-14) **A count equal to the current stock is still recorded**, as a movement with
    no change. A count from the panel behaves the same way, and the history shows that the
    stock was verified.
21. (2026-10-14) **Confirming a file with counts also requires `inventory:manage`.**
    - Without it the confirm answers 403, and a file without counts needs only
      `catalog:manage`.
    - This keeps decision 11: the two permissions can be split later without code changes.
22. (2026-10-14) **The counts reach inventory through a `catalog.import.applied` event**
    (architecture, plan 005):
    - the confirm publishes it through the outbox, in its own transaction;
    - inventory subscribes and records the count movements;
    - the counts apply about 2 s after the confirm.

    A direct call would make the catalog and inventory depend on each other.
23. (2026-10-14) **Plan 005 repair round.** The review found M1: confirming during the
    validation could skip the `inventory:manage` check of decision 21. Following the standing
    choice to fix review findings:
    - the check now runs inside the confirm's transaction, with the import locked;
    - L1 (the 403's OpenAPI description) is fixed;
    - L2 (the in-memory import query dropping `stock_counts`, the tester's finding) is fixed.
24. (2026-10-14) **A reservation is all or nothing.**
    - If any presentation of a checkout lacks units, the whole reservation is refused, and the
      refusal lists every short presentation.
    - A reservation is never left half done.
25. (2026-10-14) **A payment approved after its reservation expired** is committed only if the
    units are still available.
    - If they are not, inventory refuses, and the order is left for the shop to resolve
      (refund, or fulfil later).
    - The same refusal applies when a loss left fewer units on hand than a reservation holds.
    - Stock never goes below 0.
26. (2026-10-14) **Orders can extend an active reservation**, for example when the customer
    switches from card to OXXO, or when Mercado Pago keeps a payment pending. An expired
    reservation cannot be extended.
27. (2026-10-14) **A committed reservation records a "sale" movement per presentation.**
    - The reason is the order reference, and the system is the actor.
    - Reserving and releasing record no movement: they don't change what is in the warehouse.
28. (2026-10-14) **A loss or count may leave the units on hand below the units reserved.** It
    reflects the physical reality. Available is then 0, never negative.
29. (2026-10-14) **The panel shows units on hand, reserved and available, plus the active
    reservations of each presentation**: reference, units and expiry. The low-stock list and
    flag use available.
30. (2026-10-14) **Plan 003 repair round.** The review found M1:
    - a two-size reserve could deadlock with an admin's perfume save, because the catalog
      wrote presentations in `ml` order while reservations lock them in id order;
    - following the standing choice to fix review findings, the catalog's perfume save now
      writes presentations in id order, inside plan 003.

    L1 (a race test pinning the item lock order) and L2 (the comment on a refused reserve
    inside a caller's transaction) are fixed too.
31. (2026-10-14) **Plan 003 repair round 2.**
    - **The finding (M2):** the re-review found that an import confirm saving several
      perfumes could still deadlock with a reservation.
    - **The fix:** the confirm now locks the perfume rows by identity, then every existing
      presentation in id order, before it writes.
    - This follows the standing choice to fix review findings.

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
