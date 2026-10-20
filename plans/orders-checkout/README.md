# orders-checkout — Cart, checkout and the order lifecycle

<!-- Initiative index. Rules: docs/harness/conventions/plans.md → "The initiative README".
     Status is NOT tracked here: run `uv run just plans-status orders-checkout`. -->

## Goal

A customer can buy without an account.
- **The cart.** It lives on the server, and keeps showing current prices and availability.
- **The checkout.**
  - The customer leaves contact details and an address.
  - They choose home delivery, local delivery or pickup, and can ask for an invoice.
  - The order gets a number like `LFI-1001`.
  - The units are reserved while the customer pays.
- **The order.** The shop moves it through its states in the panel. The customer follows it
  through a private link and gets an email on every change. The owners get an email for every
  paid order.

Payment itself (Mercado Pago Checkout Pro, webhooks, refunds) is the next initiative,
`payments-mercadopago`.

## Plans

| Plan | Title | Depends on | Purpose |
| ---- | ----- | ---------- | ------- |
| 001  | Delivery options | — | New `orders` module. The owners configure home delivery (flat rate, free from an amount), local delivery (postal codes and a price) and one pickup point. A public quote says which methods apply to a postal code and subtotal, and at what cost |
| 002  | Server-side cart | 001 | A cart identified by a cookie: add, change and remove lines, at most 10 units per presentation. It shows current prices and availability, flags changed prices, and is purged after 30 days |
| 003  | Checkout → order | 002 | Guest details, address, delivery method and cost, optional invoice data, totals and an `LFI-` number. A private link; units reserved for 30 minutes; the order starts "pending payment" |
| 004  | Order lifecycle in the panel | 003 | The panel lists and opens orders and moves them through their states, with history and notes; owners mark an order paid by hand; cancelling releases the stock (or returns it, leaving the refund pending), and an unpaid order cancels itself when its payment window ends |
| 005  | Customer order page | 004 | The customer's order page, by private link or by number and email (throttled) |
| 006  | Order emails | 005 | The customer gets a confirmation and every state change; the owners get every paid order |

## Dependency notes

- **The catalog** (prices, availability, names) and **inventory** (`StockReservations`) are
  read through ports and facades. They register before `orders` in `MODULES`.
- **Payment.**
  - Nothing moves an order to "paid" automatically until `payments-mercadopago` exists.
  - That initiative will also commit, extend or release the reservation (inventory-stock
    decisions 4 and 26).
  - It will add the refund on cancelling a paid order (decision 10).
  - **A committed reservation must not block the overdue job.** When a webhook commits the
    reservation of an order that is still pending, its release is refused.
    - Plan 004's job then stops its run at that order, and the order is the oldest one, so every
      run claims it again first (plan 004 review L1 and re-review N1).
    - The payments initiative must therefore move a committed order out of `pending_payment`, or
      make the job exclude refused orders, before webhooks commit reservations.
- **Branch base.** Plan 001 builds on the `outbox:manage` permission line of
  platform-api-foundation/004, which is not merged yet. The branch starts from it to avoid a
  conflict in `identity/domain/user.py`.

## Decisions with the user

1. (2026-10-15) **Customers buy as guests, without an account.**
   - They leave a name, an email, a phone and an address.
   - Customer accounts may come later without breaking anything.
2. (2026-10-15) **The cart lives on the server,** identified by a cookie.
   - It is validated when adding and again at checkout.
   - It never reserves stock: only the checkout does.
3. (2026-10-15) **Three delivery methods:**
   - home delivery by parcel service, anywhere in Mexico;
   - local delivery within one city;
   - pickup at one point.
4. (2026-10-15) **An order can mix in-stock and made-to-order presentations, and ships in one
   go.** The checkout shows the estimated date from the longest lead time.
5. (2026-10-15) **Home delivery has a flat rate, and is free from a minimum subtotal.** Both
   values are editable in the panel. Quotes from a carrier (the `shipping` module) may replace
   this later.
6. (2026-10-15) **Local delivery and pickup.**
   - Local delivery serves an editable list of postal codes, at one editable price.
   - Pickup is at one point, with an editable address and hours, and is free.
7. (2026-10-15) **Payment by card, OXXO cash, or SPEI transfer,** all through Mercado Pago
   (ADR 0004). There is no cash on delivery.
8. (2026-10-15) **The invoice (CFDI).**
   - The checkout has "Quiero factura", with RFC, business name, tax regime, CFDI use and
     fiscal postal code.
   - The order keeps that data, and the shop issues the invoice outside the system.
9. (2026-10-15) **Order states:**
   - Pendiente de pago → Pagado → En preparación → Enviado or Listo para recoger → Entregado;
   - Cancelado, from pending or paid.

   The panel moves the states by hand; the payment webhook moves pending → paid.
10. (2026-10-15) **Cancelling a paid order.** An owner cancels it from the panel, with a
    reason. The system then:
    - requests a full refund from Mercado Pago (payments initiative);
    - returns the units to stock;
    - emails the customer.
11. (2026-10-15) **The customer pays the price in force at checkout,** including active sales.
    - The cart always shows the current price, and flags a price that changed since the line
      was added.
    - The order freezes the price.
12. (2026-10-15) **Emails.**
    - To the customer: order received (with the OXXO or SPEI instructions), payment confirmed,
      and every state change (shipped with the tracking number, ready for pickup, delivered,
      cancelled).
    - To the owners: every paid order.
13. (2026-10-15) **The customer follows the order through a private link** in the confirmation
    email. They can also look it up with the order number and their email.
14. (2026-10-15) **At most 10 units of one presentation per order.**
15. (2026-10-15) **An abandoned cart lasts 30 days.**
16. (2026-10-15) **Order numbers are consecutive with a prefix,** like `LFI-1001` and `LFI-1002`,
    easy to read out on the phone or WhatsApp.
17. (2026-10-15) **Two initiatives, in order:** orders-checkout 001–005, then
    payments-mercadopago (Checkout Pro with an idempotent webhook, then automatic refunds).
18. (2026-10-15) **The payment method is chosen inside Mercado Pago.**
    - The checkout always reserves for 30 minutes.
    - When Mercado Pago reports the payment as pending (OXXO or SPEI), the payments initiative
      extends the reservation up to the ticket's expiry. This applies inventory-stock
      decisions 4 and 26.
19. (2026-10-15) **Permissions.**
    - The new `delivery:manage` is for owners only, because it changes what customers pay.
    - The new `orders:manage` (plan 004) is for owners and staff.
20. (2026-10-15) **The three delivery methods start switched off.** An owner switches each one
    on after setting its price. With none on, the checkout refuses to place an order.
21. (2026-10-16) **Adding more units than are available is refused, with a message.** For
    example: "solo hay 2 disponibles".
    - The customer chooses how many to put in the cart.
    - Made-to-order presentations have no such limit, only the maximum of 10.
22. (2026-10-16) **A cart line whose presentation sells out, is archived or is hidden stays in
    the cart.**
    - It is marked "Agotado" or "Ya no disponible", and does not count in the subtotal.
    - The checkout asks for it to be removed or adjusted.
    - It becomes available again on its own when the stock comes back.
23. (2026-10-16) **A changed price stays flagged until the customer confirms it.** The flag shows
    "antes $X, ahora $Y".
    - It clears when the customer taps "Entendido" or changes the quantity.
    - The checkout requires nothing more, because its total already uses the new price.
24. (2026-10-16) **An unpaid order cancels itself when its reservation expires.**
    - A job moves it to Cancelado with the reason "no pagado a tiempo" and releases the
      reservation; the customer cannot pay it afterwards.
    - The payments initiative extends the reservation for OXXO and SPEI (decision 18), so only
      genuinely abandoned orders expire. The job belongs to plan 004, with the other state
      changes.
25. (2026-10-16) **Placing the order empties the cart.** If the payment fails, the customer
    retries from the order, not from the cart.
26. (2026-10-16) **The checkout refuses a total that changed.** The customer sends the total they
    saw; when the real total (prices plus delivery) differs, the checkout answers with the new
    figures and creates no order.
27. (2026-10-16) **Invoice data is checked against the SAT catalogs.**
    - The RFC must have a valid format (12 characters for a company, 13 for a person).
    - The tax regime and the CFDI use are chosen from the SAT lists, and the fiscal postal code
      has 5 digits.
    - Whether a regime fits the RFC type or the CFDI use is not checked: the shop reviews it
      when it invoices by hand.
28. (2026-10-16) **Addresses use the Mexican format:**
    - street, exterior number, optional interior number;
    - colonia, postal code, municipality or alcaldía;
    - state, chosen from the 32;
    - optional references.
29. (2026-10-16) **One full-name field and a 10-digit Mexican phone.** Spaces, dashes and a
    leading +52 are ignored.
30. (2026-10-16) **Orders are numbered from LFI-1001, without gaps.** A refused checkout does not
    use up a number, and a cancelled order keeps its own.
31. (2026-10-16) **The private link is derived, and a retried checkout gets its order back.**
    - The link token is an HMAC of the order id with a server secret. It is not stored, so the
      confirmation email (plan 005) can include it.
    - If the checkout response is lost, retrying with the same cart cookie within 30 minutes
      answers the same order instead of "carrito vacío".
32. (2026-10-17) **Owners can mark an order paid by hand, with a note.**
    - This works today, and later for payments outside Mercado Pago.
    - Marking it paid sells the reserved units. When the reservation already expired, it sells
      only if the units are still available; otherwise it is refused.
33. (2026-10-17) **Shipping a home delivery requires the carrier and the tracking number.**
    - Local delivery is marked shipped without them.
    - Pickup uses "Listo para recoger" instead of shipped.
34. (2026-10-17) **Who can cancel.** Staff and owners cancel an unpaid order. Only owners cancel a
    paid one, because it moves money.
35. (2026-10-17) **States only move forward.** A mistake is explained with an internal note, and
    every change stays in the history with who made it and when.
36. (2026-10-17) **Cancelling a paid order returns its units to stock and leaves the refund
    pending.**
    - The units go back with return movements.
    - An owner marks the refund done by hand until the payments initiative refunds
      automatically.
37. (2026-10-17) **"En preparación" is optional.** A paid order can go straight to shipped or to
    ready for pickup.
38. (2026-10-17) **The rest of the initiative is split in three.**
    - 004: the lifecycle in the panel.
    - 005: the customer's order page.
    - 006: the emails.
39. (2026-10-20) **Looking up an order by number and email opens it at once.**
    - When they match, the lookup answers the private link.
    - It is limited per IP and per order number.
    - A wrong email and an unknown number answer the same, so the lookup does not reveal which
      numbers exist.
40. (2026-10-20) **The customer's page shows everything except internal data.**
    - It shows the status timeline, products, totals, the full address, tracking, invoice data,
      the payment deadline and the estimated lead time.
    - It hides internal notes, who made each change, and the cancel reason the shop typed. It
      says only whether the customer, the system or the shop cancelled.
41. (2026-10-20) **The customer can cancel an unpaid order from its page.** The units are
    released at once, and the history records the customer as the actor.
42. (2026-10-20) **The order token never appears in a URL path.**
    - The private link carries it in the URL fragment, `/pedido#token=…`.
    - The page sends it in a POST body, as identity's links do (ADR 0010).
    - So it never reaches server or proxy logs, nor the `Referer` header.
43. (2026-10-20) **The customer's page also shows the customer's name and phone,** as decision 40
    meant: everything except internal data.

## Delivered

## Considered and discarded

- **Mandatory customer accounts:** friction on the first purchase (decision 1).
- **A cart in the browser:** prices and stock go stale until checkout (decision 2).
- **Shipping in parts, or refusing mixed carts:** two shipments and possibly two costs
  (decision 4).
- **Cash on delivery:** the user chose Mercado Pago only (decision 7).
- **Automatic CFDI with a PAC:** another integration (decision 8).
- **Keeping the price from when the line was added:** the user prefers the current price
  (decision 11).
- **Random order codes:** harder to read out (decision 16).
