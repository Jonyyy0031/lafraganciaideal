---
status: testing
module: orders
min_implementer: mid
depends_on: ["002"]
---

# 003 — Checkout → order

## Context

The guest turns the cart into an order: contact details, an address, a delivery method, optional
invoice data, the units reserved for 30 minutes, and a consecutive number like `LFI-1001`. It
applies README decisions 1, 3, 4, 8, 11, 13, 14, 16, 18, 20, 22 and the new 24–30.

**What exists today:**
- **The cart** (plan 002, done).
  - The aggregate and the pure view: `cart_view` gives each line a status, and the subtotal
    counts only `ok` lines (`apps/api/src/fragancia_api/modules/orders/domain/cart.py:137-211`).
  - Reading: the reader port `CartReader.by_token` (`modules/orders/application/ports.py:249-252`)
    and the write port `CartRepository.get_by_token_for_update`
    (`modules/orders/domain/repositories.py:24-40`).
  - The catalog offers are read **outside** any transaction, because the adapter takes its own
    pooled connection (`modules/orders/application/commands/cart.py:305-308`, the plan 002
    review repair).
  - The cookie `fragancia_cart` (`modules/orders/http/cart_cookie.py:1-27`) and the token port
    `CartTokens` (`ports.py:241-246`), implemented by `SecureCartTokens`
    (`modules/orders/infrastructure/cart_tokens.py`).
- **Delivery** (plan 001, done). `quote(settings, postal_code, subtotal)` returns the methods
  that apply, with their cost (`modules/orders/domain/delivery.py:226-243`); the settings are
  read through `DeliverySettingsReader.current()` (`ports.py:223-228`). `PostalCode` and
  `Subtotal` are value objects in the same file (`:27-54`).
- **Inventory's reservation facade** `StockReservations.reserve(reference, quantities,
  expires_at) -> Result[UUID, DomainError]` (`modules/inventory/application/stock_reservations.py:55-60`).
  - It is all or nothing, refusing with `INVENTORY_STOCK_UNAVAILABLE` and
    `details.lines = [{presentation_id, requested, available}]`
    (`modules/inventory/application/commands/reservations.py:94-131`).
  - It joins the caller's transaction (`stock_reservations.py:39`).
  - It refuses made-to-order presentations with `INVENTORY_PRESENTATION_NOT_STOCKED`
    (`reservations.py:96-99`), so only in-stock lines are reserved.
  - Its lock order is catalog presentations (shared), then settings, then stock items
    (`reservations.py:95-101`). It is registered in `modules/inventory/module.py:160-163`.
- **The transaction runner** commits on `Ok` and rolls back on `Err`. A nested call joins the
  outer transaction (`apps/api/src/fragancia_api/shared/application/transactions.py:8-13`).
- **Aggregates and events.** An aggregate records events with `record` and hands them over with
  `pull_events` (`shared/kernel/entity.py:10-13`); a command publishes them inside its
  transaction (`modules/catalog/application/commands/create_brand.py:41`).
- **The email rule** to copy is identity's `Email.create`: strip, lowercase, one `@`, a dot in
  the domain (`modules/identity/domain/user.py:27-45`). Orders may not import identity.
- **Clearing a cookie** follows identity's `clear` (`modules/identity/http/cookies.py:28-35`).
- **Cross-module calls** need a port, an adapter and two `.importlinter` ignore lines, like the
  catalog ones (`apps/api/.importlinter:108-109`).

**Approach.**
- **One public command, `PlaceOrder`, behind `POST /api/v1/checkout`.**
  1. It validates the input.
  2. It reads the cart and its offers outside any transaction.
  3. It refuses a cart with problems.
  4. It quotes the chosen delivery method and compares the total with the one the customer saw.
  5. In one transaction it:
     - locks the cart and checks it did not change;
     - reserves the in-stock units for 30 minutes;
     - takes the next order number;
     - stores the order with frozen prices, deletes the cart and publishes `orders.order.placed`.
- **The response** carries the number and the private-link token, and clears the cart cookie.
- **Order numbers are gapless.** They come from a counter row updated in the same transaction,
  so a refused checkout does not burn a number. A PostgreSQL sequence would skip numbers on
  every rollback.
- **Rejected:**
  - Reading the offers inside the transaction: this is the pool starvation fixed in plan 002.
  - Reserving made-to-order lines: inventory does not stock them.
  - Storing the order's lines as JSON: the panel (004) filters and lists them.

## Out of scope

- **Payment** (the `payments-mercadopago` initiative). The order stays `pending_payment`.
- **Plan 004:**
  - The automatic cancellation of an unpaid order when its reservation expires (decision 24),
    together with the other state changes and the release.
  - The customer's order page by private link, the lookup by number and email, and the panel's
    order list.
- **Plan 005:** the emails. This plan only publishes `orders.order.placed` for them.
- **Checking a tax regime against the CFDI use or the RFC type.** The shop checks this when it
  invoices by hand (decision 27).
- **Saved addresses and customer accounts** (decision 1).

## Dependencies

- **002 (done):** the cart, its reader and repository, `CatalogOffers`, `cart_view` and the
  cookie.
- **001 (done):** `quote`, `DeliverySettingsReader`, `PostalCode` and `Subtotal`.
- **inventory-stock/003 (done):** `StockReservations`.

## Steps

1. **Domain: customer data and the SAT catalogs**
   - Files: `apps/api/src/fragancia_api/modules/orders/domain/customer.py` (create),
     `apps/api/src/fragancia_api/modules/orders/domain/errors.py` (modify)
   - Do:
     - **One error.** `CustomerDataInvalid(InvalidValueError)` with code
       `ORDERS_CUSTOMER_DATA_INVALID`, message "Revisa los datos del pedido", and
       `details = {"field": "<path>"}`. The paths: `contact.name`, `contact.email`,
       `contact.phone`, `address`, `address.street`, `address.exterior_number`,
       `address.interior_number`, `address.neighborhood`, `address.postal_code`,
       `address.municipality`, `address.state`, `address.references`, `invoice.rfc`,
       `invoice.business_name`, `invoice.tax_regime`, `invoice.cfdi_use`,
       `invoice.postal_code`.
     - **`CustomerName.create(raw)`** collapses whitespace and keeps 2–120 characters
       (decision 29).
     - **`CustomerEmail.create(raw)`** applies identity's rule (`user.py:27-45`), with at most
       254 characters.
     - **`Phone.create(raw)`** (decision 29):
       - drop spaces, `-`, `.`, `(` and `)`;
       - drop a leading `+52`, or a leading `52` when 12 digits remain;
       - the result must be exactly 10 digits, and that is what is stored.
     - **`MX_STATES`** (decision 28): an ordered mapping from ISO 3166-2:MX code to name, for
       all 32 states. AGU Aguascalientes, BCN Baja California, BCS Baja California Sur, CAM
       Campeche, CHP Chiapas, CHH Chihuahua, CMX Ciudad de México, COA Coahuila, COL Colima,
       DUR Durango, MEX Estado de México, GUA Guanajuato, GRO Guerrero, HID Hidalgo, JAL
       Jalisco, MIC Michoacán, MOR Morelos, NAY Nayarit, NLE Nuevo León, OAX Oaxaca, PUE
       Puebla, QUE Querétaro, ROO Quintana Roo, SLP San Luis Potosí, SIN Sinaloa, SON Sonora,
       TAB Tabasco, TAM Tamaulipas, TLA Tlaxcala, VER Veracruz, YUC Yucatán, ZAC Zacatecas.
     - **`Address.create(...)`** (decision 28). Text fields have whitespace collapsed. An empty
       optional field becomes None.
       - `street`: 1–120 characters.
       - `exterior_number`: 1–20.
       - `interior_number`: optional, up to 20.
       - `neighborhood` (colonia): 1–120.
       - `postal_code`: a `PostalCode` from `delivery.py`.
       - `municipality`: 1–120.
       - `state`: a key of `MX_STATES`.
       - `references`: optional, up to 300.
     - **`TAX_REGIMES`**, the SAT catalog c_RegimenFiscal, code to description: 601 General de
       Ley Personas Morales; 603 Personas Morales con Fines no Lucrativos; 605 Sueldos y
       Salarios e Ingresos Asimilados a Salarios; 606 Arrendamiento; 607 Régimen de
       Enajenación o Adquisición de Bienes; 608 Demás ingresos; 610 Residentes en el Extranjero
       sin Establecimiento Permanente en México; 611 Ingresos por Dividendos (socios y
       accionistas); 612 Personas Físicas con Actividades Empresariales y Profesionales; 614
       Ingresos por intereses; 615 Régimen de los ingresos por obtención de premios; 616 Sin
       obligaciones fiscales; 620 Sociedades Cooperativas de Producción que optan por diferir
       sus ingresos; 621 Incorporación Fiscal; 622 Actividades Agrícolas, Ganaderas,
       Silvícolas y Pesqueras; 623 Opcional para Grupos de Sociedades; 624 Coordinados; 625
       Régimen de las Actividades Empresariales con ingresos a través de Plataformas
       Tecnológicas; 626 Régimen Simplificado de Confianza.
     - **`CFDI_USES`**, the SAT catalog c_UsoCFDI, without CP01 (Pagos) and CN01 (Nómina),
       which do not apply to a sale: G01 Adquisición de mercancías; G02 Devoluciones,
       descuentos o bonificaciones; G03 Gastos en general; I01 Construcciones; I02 Mobiliario y
       equipo de oficina por inversiones; I03 Equipo de transporte; I04 Equipo de cómputo y
       accesorios; I05 Dados, troqueles, moldes, matrices y herramental; I06 Comunicaciones
       telefónicas; I07 Comunicaciones satelitales; I08 Otra maquinaria y equipo; D01
       Honorarios médicos, dentales y gastos hospitalarios; D02 Gastos médicos por incapacidad
       o discapacidad; D03 Gastos funerales; D04 Donativos; D05 Intereses reales efectivamente
       pagados por créditos hipotecarios (casa habitación); D06 Aportaciones voluntarias al SAR;
       D07 Primas por seguros de gastos médicos; D08 Gastos de transportación escolar
       obligatoria; D09 Depósitos en cuentas para el ahorro, primas que tengan como base planes
       de pensiones; D10 Pagos por servicios educativos (colegiaturas); S01 Sin efectos
       fiscales.
     - **`Invoice.create(...)`** (decision 27):
       - `rfc`: trimmed and uppercased, and it must match `^[A-ZÑ&]{3,4}[0-9]{6}[A-Z0-9]{3}$`
         (12 characters for a company, 13 for a person);
       - `business_name`: whitespace collapsed, 1–254 characters;
       - `tax_regime`: a key of `TAX_REGIMES`;
       - `cfdi_use`: a key of `CFDI_USES`;
       - `postal_code`: a `PostalCode`.
     - Every refusal returns `CustomerDataInvalid` with the field path. `PostalCode` failures
       are re-wrapped with the path (`address.postal_code` or `invoice.postal_code`).
   - Observable result: unit tests accept and refuse each field at its bounds, normalize
     `+52 998-123-4567` to `9981234567`, and accept `XAXX010101000`.

2. **Domain: the order**
   - Files: `apps/api/src/fragancia_api/modules/orders/domain/order.py` (create),
     `apps/api/src/fragancia_api/modules/orders/domain/errors.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/domain/repositories.py` (modify)
   - Do:
     - **Constants:**
       - `ORDER_NUMBER_PREFIX = "LFI-"` and `FIRST_ORDER_NUMBER = 1001` (decisions 16 and 30);
       - `PAYMENT_WINDOW = timedelta(minutes=30)` (decision 18).
     - **Statuses.** `OrderStatus(StrEnum)` with only `PENDING_PAYMENT = "pending_payment"`.
       Plan 004 adds the others.
     - **`format_order_number(n) -> str`** returns `f"LFI-{n}"`.
     - **`OrderLine`,** frozen. It is the frozen copy of the offer:
       - `presentation_id` and `perfume_id`;
       - `label` and `ml`;
       - `availability`, `lead_time_min_days` and `lead_time_max_days`;
       - `quantity`;
       - `unit_price_cents`, `regular_price_cents` and `line_total_cents`.
     - **`Delivery`,** frozen:
       - `method` (`DeliveryMethod`) and `cost_cents`;
       - `address: Address | None`;
       - `pickup_address: str | None` and `pickup_hours: str | None`, a copy of the pickup
         point at checkout.
     - **`Order(AggregateRoot)`**:
       - fields: `id`, `number`, `status`, `access_token_digest`, `name`, `email`, `phone`,
         `delivery`, `invoice: Invoice | None`, `lines`, `subtotal_cents`, `total_cents`,
         `lead_time_max_days`, `reservation_id: UUID | None`, `payment_due_at`, `created_at`
         and `updated_at`;
       - `Order.place(...)` computes the subtotal (the sum of the line totals), the total
         (subtotal plus delivery cost) and `lead_time_max_days` (the maximum over the
         made-to-order lines, or None). The status is `PENDING_PAYMENT`.
       - It records `OrderPlaced`.
     - **`OrderPlaced(DomainEvent)`**: `name = "orders.order.placed"`, with `order_id` and
       `number: int`.
     - **New errors** (`ConflictError`; messages in Spanish because the storefront shows them):

       | Code | Message | `details` |
       | ---- | ------- | --------- |
       | `ORDERS_CHECKOUT_CART_EMPTY` | "Tu carrito está vacío" | — |
       | `ORDERS_CHECKOUT_CART_HAS_PROBLEMS` | "Revisa tu carrito: hay productos agotados o no disponibles" | `{"lines": [{"presentation_id", "status"}]}` for every line that is not `ok` |
       | `ORDERS_CHECKOUT_CART_CHANGED` | "Tu carrito cambió; revísalo e intenta de nuevo" | — |
       | `ORDERS_CHECKOUT_DELIVERY_UNAVAILABLE` | "Ese método de entrega no está disponible para tu pedido" | `{"method"}` |
       | `ORDERS_CHECKOUT_TOTAL_CHANGED` | "El total de tu pedido cambió" | `{"subtotal_cents", "delivery_cents", "total_cents"}` |
       | `ORDERS_CHECKOUT_STOCK_UNAVAILABLE` | "Ya no hay suficientes unidades de algunos productos" | inventory's `{"lines": [{"presentation_id", "requested", "available"}]}` |

     - **`OrderRepository`** (write side, joins the active transaction):
       - `next_number() -> int` takes the next gapless number;
       - `add(order) -> None` inserts the order and its lines.
     - **`CartRepository`** gains `delete(cart) -> None`.
   - Observable result: unit tests place an order, check the totals and the lead time, and see
     one `OrderPlaced`.

3. **Application: `PlaceOrder`**
   - Files: `apps/api/src/fragancia_api/modules/orders/application/commands/checkout.py` (create),
     `apps/api/src/fragancia_api/modules/orders/application/ports.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/contracts.py` (modify)
   - Do:
     - **Ports** (`ports.py`):
       - `StockHolds.hold(reference, quantities, expires_at) -> Result[UUID, DomainError]`, where
         a shortage comes back as `CheckoutStockUnavailable`;
       - `OrderAccessTokens` with `new()` and `digest(token)`. `SecureCartTokens` satisfies it
         structurally, so there is no new adapter.
     - **Contracts:**
       - `CheckoutContact`: `name`, `email` and `phone` (strings with `max_length` bounds).
       - `CheckoutAddress`: `street`, `exterior_number`, `interior_number`, `neighborhood`,
         `postal_code`, `municipality`, `state` and `references`.
       - `CheckoutInvoice`: `rfc`, `business_name`, `tax_regime`, `cfdi_use` and `postal_code`.
       - `CheckoutRequest`:
         - `contact`;
         - `delivery_method: Literal["home", "local", "pickup"]`;
         - `address: CheckoutAddress | None`;
         - `invoice: CheckoutInvoice | None`;
         - `expected_total_cents: int`.
       - `PlacedOrder`:
         - `number` (`"LFI-1001"`), `access_token`, `status`;
         - `subtotal_cents`, `delivery_cents`, `total_cents`;
         - `payment_due_at`, `lead_time_max_days`.

       Docstrings name the decisions.
     - **`PlaceOrder.execute(token, request) -> Result[PlacedOrder, DomainError]`,** in this
       order:
       1. **Validate** the contact, the address and the invoice through step 1. The address is
          required for `home` and `local`, and a `pickup` request must not send one (both give
          field `address`). The first error wins.
       2. **Read the cart.** With no token, an unknown cart or no lines, refuse
          `ORDERS_CHECKOUT_CART_EMPTY`. Otherwise use `CartReader.by_token` (outside any
          transaction).
       3. **Read the offers.** Take `now`, then `CatalogOffers.offers(ids, now)` outside any
          transaction, then `cart_view`. When `has_problems`, refuse `CART_HAS_PROBLEMS`.
       4. **Quote.** Read `DeliverySettingsReader.current()` and run
          `quote(settings, address.postal_code or None, Subtotal(view.subtotal))`. Pass the
          `Subtotal` error through. When the chosen method is missing, refuse
          `DELIVERY_UNAVAILABLE`.
       5. **Compare the total.** Compute subtotal plus cost. When it differs from
          `expected_total_cents`, refuse `TOTAL_CHANGED` with the real figures (decision 26).
       6. **Build the order.** Take `order_id = new_id()` and an access token. The lines are
          the view's lines joined with their offers (price, regular price, label, ml,
          availability, lead times, perfume id).
       7. **Run one `transactions.run(work)`:**
          1. Lock the cart with `get_by_token_for_update`. When it is gone, or its
             `(presentation_id, quantity)` pairs differ from the cart read in step 2, refuse
             `CART_CHANGED`.
          2. Take the in-stock lines as `{presentation_id: quantity}`. When there are any, call
             `stocks.hold(f"order:{order_id}", quantities, now + PAYMENT_WINDOW)`, which is
             all or nothing. A refusal returns `Err`, so everything rolls back. The reservation
             id is kept; it is None when every line is made to order.
          3. Take `number = orders.next_number()`. It comes last, so the counter row is held
             the shortest time.
          4. Run `Order.place(...)` with `payment_due_at = now + PAYMENT_WINDOW`, then
             `orders.add(order)`, `carts.delete(cart)` (decision 25) and
             `events.publish(order.pull_events())`.
       8. **Return** `PlacedOrder` with `format_order_number(number)` and the access token.
     - **Lock order:** the cart row, then inventory's own order (catalog presentations, settings,
       stock items), then the counter row. Nothing else locks orders rows, so no cycle is
       possible.
   - Observable result: application tests with the in-memory fakes cover every refusal, the
     happy path (cart deleted, one reservation of the in-stock lines only, the number 1001 and
     then 1002) and the rollback when stock is refused (no order, the cart kept, the number not
     consumed).

4. **Infrastructure: tables, migration, adapters**
   - Files: `apps/api/src/fragancia_api/modules/orders/infrastructure/tables.py` (modify),
     `apps/api/migrations/versions/0019_orders_orders.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_order_repository.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_cart_repository.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/stock_holds.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/in_memory.py` (modify)
   - Do:
     - **`orders.order_numbers`:** `id` (PK, always 1) and `last_number` (integer, not null).
       The migration seeds `(1, 1000)`.
     - **`orders.orders`:**
       - `id` (uuid, PK); `number` (integer, unique); `status` (varchar 30);
         `access_token_digest` (varchar 64, unique);
       - `customer_name` (varchar 120), `customer_email` (varchar 254, indexed),
         `customer_phone` (varchar 10);
       - `delivery_method` (varchar 10, check in home/local/pickup) and `delivery_cents`;
       - nullable address columns: `address_street` (120), `address_exterior_number` (20),
         `address_interior_number` (20), `address_neighborhood` (120), `address_postal_code`
         (5), `address_municipality` (120), `address_state` (3), `address_references` (300);
       - nullable `pickup_address` (300) and `pickup_hours` (200);
       - nullable `invoice_rfc` (13), `invoice_business_name` (254), `invoice_tax_regime` (3),
         `invoice_cfdi_use` (3), `invoice_postal_code` (5);
       - `subtotal_cents`, `total_cents`, nullable `lead_time_max_days`;
       - nullable `reservation_id` (uuid, no FK: inventory's schema);
       - `payment_due_at`, `created_at`, `updated_at` (timestamptz);
       - check `total_cents = subtotal_cents + delivery_cents`.
     - **`orders.order_lines`:**
       - `order_id` (FK `ON DELETE CASCADE`) and `position`, together the PK;
       - `presentation_id` and `perfume_id` (uuid, no FK);
       - `label` (varchar 300), `ml`, `availability` (varchar 20), and nullable lead times;
       - `quantity`, with check 1–10;
       - `unit_price_cents`, `regular_price_cents`, `line_total_cents`;
       - check `line_total_cents = unit_price_cents * quantity`.
     - **Migration `0019_orders_orders`:** autogenerate, then tidy by hand like `0018`. It
       creates the three tables and seeds the counter. The downgrade drops them.
     - **`SqlOrderRepository`:**
       - `next_number` runs
         `UPDATE orders.order_numbers SET last_number = last_number + 1 WHERE id = 1 RETURNING last_number`;
       - `add` inserts the order row and its lines in position order.
     - **`SqlCartRepository.delete(cart)`** deletes the cart row; the lines cascade.
     - **`StockHoldsAdapter(StockReservations)`:**
       - `hold` calls `reserve`;
       - an `Err` with code `INVENTORY_STOCK_UNAVAILABLE` becomes `CheckoutStockUnavailable`
         with the same `details`;
       - any other `Err` passes through unchanged.
     - **In-memory:**
       - `InMemoryOrders`, with a counter starting at 1000;
       - `InMemoryStockHolds`, configurable to refuse with given short lines and recording its
         calls;
       - `InMemoryCarts.delete`.
   - Observable result: the migration upgrades and downgrades on a scratch database, and the
     integration tests store an order and take numbers 1001 and 1002.

5. **HTTP, wiring and architecture**
   - Files: `apps/api/src/fragancia_api/modules/orders/http/router.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/http/cart_cookie.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/module.py` (modify),
     `apps/api/.importlinter` (modify),
     `apps/api/openapi.json` (modify)
   - Do:
     - **`CartCookie.clear(response)`,** like identity's `clear`, with the same path.
     - **`checkout = public_router(prefix="/checkout", tags=["orders · checkout"])`.**
       - `POST ""` answers 201 with `PlacedOrder`, reads the cart cookie, and clears it on
         success.
       - It declares 409 and 422 with `ErrorResponse`.
       - Its docstring lists every error code and decisions 24–26.
       - Add it to `routers`.
     - **Wiring:** `module.py` builds `PlaceOrder` from:
       - the cart reader and repository;
       - the `SecureCartTokens` instance (used for both cart and access tokens);
       - the catalog adapter and the delivery reader;
       - `SqlOrderRepository`;
       - `StockHoldsAdapter(services.get(StockReservations))`, importing `StockReservations`
         from `fragancia_api.modules.inventory`;
       - `platform.transactions`, `platform.events` and `platform.clock`.
     - **`.importlinter`:** add two ignore lines next to the catalog ones:
       - `fragancia_api.modules.orders.infrastructure.stock_holds -> fragancia_api.modules.inventory`
       - `fragancia_api.modules.orders.module -> fragancia_api.modules.inventory`
     - Run `uv run just openapi`.
   - Observable result: `uv run just arch` passes, and the route is in the OpenAPI document.

6. **Docs and tests**
   - Files: `docs/architecture.md` (modify),
     `apps/api/src/fragancia_api/modules/orders/__init__.py` (modify)
   - Do:
     - Extend the orders row of `docs/architecture.md` with the checkout, the order tables, the
       `orders.order.placed` event and the dependency on inventory's `StockReservations`.
     - Add the event to the orders `__init__.py` docstring, in the inventory style
       (`modules/inventory/__init__.py:1-20`).
     - The tester writes the layered tests (see the table below).
   - Observable result: `uv run just check` and `uv run just test-integration` pass.

## Acceptance criteria

Run with the API on the development database. Delivery options are set by an owner (home 18000,
free from 250000; local 9000 for 77500; pickup on), and the QA perfume from plan 002 is used.

- [ ] **Happy path, home delivery.** A cart with the 50 ml in stock ×2 and the made-to-order
  200 ml ×1. `POST /api/v1/checkout` with valid contact, address (postal code 01000), no
  invoice, `delivery_method: "home"` and the right `expected_total_cents` answers 201:
  - `number` is `LFI-1001` (or the next one), with `status: "pending_payment"`;
  - the totals equal the cart's subtotal plus the delivery cost;
  - `payment_due_at` is 30 minutes ahead and `lead_time_max_days` is 10.
  - The response clears `fragancia_cart`, and `GET /cart` with the old cookie is empty.
  - In SQL, one `orders.orders` row and two `order_lines` rows hold the frozen prices.
  - One active `inventory.reservations` row with reference `order:<id>` reserves only the 50 ml
    ×2, and `inventory.stock_items.reserved` rose by 2.
  - One `orders.order.placed` row is in the outbox.
- [ ] **Numbers.** A second order gets the next number. A checkout refused for stock does not
  consume a number.
- [ ] **Refusals (409):**
  - no cookie, or an empty cart: `ORDERS_CHECKOUT_CART_EMPTY`;
  - a sold-out line: `ORDERS_CHECKOUT_CART_HAS_PROBLEMS`, listing it;
  - `local` with a postal code off the list: `ORDERS_CHECKOUT_DELIVERY_UNAVAILABLE`;
  - a wrong `expected_total_cents`: `ORDERS_CHECKOUT_TOTAL_CHANGED` with the real figures;
  - more units than inventory has: `ORDERS_CHECKOUT_STOCK_UNAVAILABLE` with inventory's lines.
    To set this up, lower `on_hand` after adding to the cart, while the catalog copy still shows
    enough; or reserve the units through another order first.

  After each refusal the cart is intact and no order row exists.
- [ ] **Validation (422 `ORDERS_CUSTOMER_DATA_INVALID`):**
  - a 9-digit phone (`contact.phone`);
  - a bad email;
  - an unknown state (`address.state`);
  - `home` without an address (`address`);
  - `pickup` with an address;
  - an invoice with RFC `ABC123`, regime `999` or use `CP01`.

  `+52 (998) 123-4567` is stored as `9981234567`.
- [ ] **Pickup and invoice.** A pickup order with an invoice (a valid RFC, 601, G03) stores no
  address. It stores the pickup address and hours as they were, the invoice fields, and
  `delivery_cents` 0.
- [ ] **Made to order only.** A cart with only the 200 ml places an order with
  `reservation_id` null and creates no reservation.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | Every customer field at its bounds (name 1/2/120/121, phone forms, each state code, RFC 12/13 and invalid, every catalog boundary including CP01/CN01 refused); `Order.place` totals, lead time over made-to-order only, the event |
| application | yes     | `PlaceOrder`: each refusal in its order of precedence; address required/forbidden by method; the cart-changed check; stock refusal rolls back (no order, cart kept, number not consumed); reservation only for in-stock lines, none when all made to order; reference and expiry; cart deleted; event published |
| http        | yes     | 201 shape, the cookie cleared on success and kept on refusal, every status code and error code, OpenAPI |
| integration | yes     | `SqlOrderRepository` (gapless numbers, rows and checks), `SqlCartRepository.delete` cascade, `StockHoldsAdapter` against the real inventory (all or nothing, reserved count), the full `PlaceOrder` over real adapters, rollback leaving no rows, the migration round trip on a scratch database |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

None. (Notes: `CustomerDataInvalid` fields for `PostalCode` failures are re-wrapped in `customer.py`; `Subtotal` errors pass through as `ORDERS_DELIVERY_OPTIONS_INVALID`, documented in the route. The migration downgrade was not run on a scratch database by the implementer; it belongs to the integration tests.)

## Test coverage

## Review findings

## Verification
