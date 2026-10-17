---
status: review
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
   - Files (tests): `apps/api/tests/unit/orders/` (modify), `apps/api/tests/integration/orders/` (modify)
   - Observable result: `uv run just check` and `uv run just test-integration` pass.

7. **Review repair (added 2026-10-16 after the first review; decision 31)**
   - Files: `apps/api/src/fragancia_api/config.py` (modify),
     `apps/api/.env.example` (modify),
     `apps/api/src/fragancia_api/modules/orders/application/ports.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/application/commands/checkout.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/domain/order.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/domain/repositories.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/order_links.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/stock_holds.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_order_repository.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/tables.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/in_memory.py` (modify),
     `apps/api/migrations/versions/0020_orders_cart_token.py` (create),
     `apps/api/src/fragancia_api/modules/orders/module.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/http/router.py` (modify),
     `apps/api/openapi.json` (modify),
     `docs/architecture.md` (modify),
     `apps/api/tests/unit/test_settings.py` (modify),
     `apps/api/tests/unit/test_http_platform.py` (modify),
     `apps/api/tests/unit/orders/` (modify), `apps/api/tests/integration/orders/` (modify)
   - Do:
     - **B1 (scope):** the test directories are now listed on step 6's `Files (tests):` line.
       Nothing to do in code.
     - **L2, a lost response (decision 31).**
       - **Settings.** Add `order_link_secret: SecretStr | None = None` to `Settings`
         (`config.py`), next to the session settings.
         - A `model_validator` requires it in production, with at least 32 characters.
         - Outside production, when it is unset, the module uses the fixed development value
           `"dev-order-link-secret-not-for-production"`.
         - Add `ORDER_LINK_SECRET=` with a comment to `.env.example`. The comment says: never
           rotate it, because rotating breaks every private link already sent.
       - **Tokens derived, not random.** Replace the `OrderAccessTokens` port with
         `OrderLinks`, which has `token(order_id) -> str` and `digest(token) -> str`.
         - `HmacOrderLinks(secret)` (`infrastructure/order_links.py`) computes the token as
           base64url, without padding, of HMAC-SHA256(secret, `b"order-link:" + order_id.bytes`).
         - `digest` is SHA-256 hex, as before.
         - The order still stores only `access_token_digest`, and plan 004 looks orders up by
           it. Plan 005 regenerates the link from the order id.
       - **The cart a retry used.** `Order` gains `cart_token_digest: str`, the digest of the
         cart cookie it was placed from. Migration `0020_orders_cart_token` adds
         `orders.orders.cart_token_digest` (varchar 64, nullable for existing rows, indexed).
         `SqlOrderRepository` writes it.
       - **Replay.** Add `OrderRepository.placed_from_cart(cart_token_digest, since) -> Order | None`
         (or a reader-port method with the same shape). It returns the newest order with that
         cart digest, `status = pending_payment` and `created_at >= since`.
         - In `PlaceOrder`, when there is a token but the cart is missing or empty, call it with
           `since = now - PAYMENT_WINDOW` before refusing `ORDERS_CHECKOUT_CART_EMPTY`.
         - When an order is found, return its `PlacedOrder` again, with
           `access_token = links.token(order.id)`. This happens before input validation is
           checked against it: a replay ignores the request body.
         - Document the replay in the route docstring. The router still clears the cookie.
     - **L1, inventory refusals.** In `StockHoldsAdapter`, also translate
       `INVENTORY_PRESENTATION_NOT_STOCKED` and `INVENTORY_PRESENTATION_NOT_FOUND` into
       `CheckoutCartChanged`. This happens when the owner switches a presentation to made to
       order, or removes it, during the checkout. Other errors still pass through.
     - **L3, business rules in the domain.**
       - Move the rule "address required for home and local, forbidden for pickup" into the
         domain, as `Delivery.create(method, cost_cents, address, pickup)` returning
         `Result[Delivery, CustomerDataInvalid]` with field `address`.
       - Compute an `OrderLine`'s `line_total_cents` in a factory `OrderLine.create(...)` from
         the unit price and quantity, instead of in `checkout.py`.
     - **L4.** `PlacedOrder.status` takes `order.status.value`.
     - Update `docs/architecture.md` (orders row: the order-link secret) and regenerate
       `openapi.json`.
   - Observable result:
     - Unit and integration tests cover:
       - the derived token: stable for one id, different across ids, its digest stored;
       - the replay within 30 minutes (same number and token, no second order or reservation);
       - no replay after 30 minutes or for another cart;
       - the two new inventory translations;
       - `Delivery.create`;
       - `OrderLine.create`;
       - the production validator for the secret;
       - migration 0020 round trip.
     - `uv run just plans-scope` is clean.

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

Step 7 (2026-10-17): the first review's evidence (Test coverage, Review findings) predates the
repair and is superseded by it; testing, review and verify repeat. Deviations of the repair:

- The production validator for `ORDER_LINK_SECRET` broke three existing settings tests that
  build production settings. Their files (`tests/unit/test_settings.py`,
  `tests/unit/test_http_platform.py`) were added to step 7's file list (cosmetic: they now pass
  the secret).
- The replay lookup is a read-side port, `OrderReader.placed_from_cart` (`SqlOrderReader`), not
  a method of the write-side `OrderRepository`: `Database.session` raises outside a transaction,
  and the plan allows "a reader-port method with the same shape". `InMemoryOrders` implements
  both. `domain/repositories.py` therefore did not change.
- `PlaceOrder` now reads the cart and tries the replay BEFORE validating the body (the replay
  ignores the body); a cart with a token but no lines and no recent order still validates the
  body first and then answers `ORDERS_CHECKOUT_CART_EMPTY`, as before. The rule "address
  required for home/local, forbidden for pickup" is checked in validation through
  `Delivery.create` with placeholders (to keep the error precedence), and the real
  `Delivery.create` runs again with the cost and the pickup point. One consequence: a pickup
  request with an INVALID address now reports the address field's own error instead of `address`.

## Repair log

- 2026-10-17, step 7 (review repair; decision 31), implementer:
  - **B1:** plan-only; test directories already on step 6's `Files (tests):` line.
  - **L2:** `Settings.order_link_secret` (+ production validator, `.env.example`);
    `OrderLinks` port replaces `OrderAccessTokens`; `HmacOrderLinks`; `Order.cart_token_digest`;
    migration `0020_orders_cart_token`; `OrderReader.placed_from_cart` and the replay in
    `PlaceOrder`; route docstring.
  - **L1:** `StockHoldsAdapter` maps `INVENTORY_PRESENTATION_NOT_STOCKED` and
    `INVENTORY_PRESENTATION_NOT_FOUND` to `ORDERS_CHECKOUT_CART_CHANGED`.
  - **L3:** `Delivery.create`, `OrderLine.create`. **L4:** `status=order.status.value`.
  - `docs/architecture.md` and `openapi.json` updated.
  - Tests updated: unit and integration for the renamed port, `PlaceOrder` arguments and
    `Order.place(cart_token_digest=...)`; the old "same cookie finds no cart" and "made to order
    passes through" tests now assert the replay and `CART_CHANGED`; the email-index test now
    expects two indexes. Tests added: `test_order_links.py`, replay (unit, http, integration),
    no replay after 30 minutes or for another cart, the two inventory translations,
    `Delivery.create`, `OrderLine.create`, the settings validator, `SqlOrderReader`, and
    `test_migration_0020_orders_cart_token.py` (upgrade, downgrade, re-upgrade on a scratch
    database).
- 2026-10-17, re-review R1, main session: when the transaction refuses
  `ORDERS_CHECKOUT_CART_CHANGED`, `PlaceOrder` tries `placed_from_cart` once and answers that
  order. A retry that waited on the cart lock behind the first checkout now gets the replay
  instead of the refusal (`application/commands/checkout.py`, the final `match`). A cart that
  still exists cannot have an order placed from its digest, so a genuine change still refuses.
  `uv run just check` green (3081 passed). R2 (the cookie holder can fetch the link for 30
  minutes) is accepted with decision 31 and noted for plan 004; R3 is the logged deviation 3.
  The regression test for R1 and T1–T3 go to the tester.

## Test coverage

### Re-test after the step 7 repair (2026-10-17), tester

Baseline before writing: `uv run just check` green (3081 passed, 3 skipped; harness 685) and
`uv run just test-integration` green (882 passed, 5 skipped). Closing: `uv run just check` green
(3097 passed, 3 skipped; harness 685) and `uv run just test-integration` green (883 passed, 5
skipped). Tests added by the tester: 16 unit (T2: 7 parametrized wiring cases; T3: 5 + 1; R1: 3)
and 1 integration (R1). No GAP and no NOT CONFIRMED. Product code untouched. The rows below
replace every row of the first baseline that touches `checkout.py`, `stock_holds.py`, `order.py`,
`order_links.py`, `SqlOrderReader`, `config.py` or migration 0020 (the rows of the first baseline
that say "validation precedes the cart", "empty cart: ... unknown token" and "StockHoldsAdapter
translates only ..." are superseded by them; line references of the old table into `checkout.py`
are stale).

| Behavior (plan / code) | Source | Layer | Test | State |
| ---------------------- | ------ | ----- | ---- | ----- |
| Replay: a token whose cart is missing or empty and has a `pending_payment` order from that cart digest within 30 minutes gets the same `PlacedOrder` (same number, same derived token), with no second order, hold or event; the body is ignored | `checkout.py:198-201` | application | `test_checkout.py::test_a_retry_within_30_minutes_answers_the_same_order_without_a_second_one` | CONFIRMED |
| Replay window and cart identity: none after 30 minutes, none for another cart | `checkout.py:199`, `in_memory.py:83-91` | application | `test_checkout.py::test_no_replay_after_30_minutes`, `test_no_replay_for_another_cart` | CONFIRMED |
| **T3** The replay happens before validation: a retry whose body fails `ORDERS_CUSTOMER_DATA_INVALID` (phone, email, `address` missing, pickup with address, `address.state`) still gets its order; the same body is refused by a world without the order | `checkout.py:198-207` | application | `test_checkout.py::test_the_replay_happens_before_validation_so_an_invalid_body_still_gets_its_order` (5 cases) | CONFIRMED |
| A cart that still has lines never replays, and its body is validated | `checkout.py:198,203` | application | `test_checkout.py::test_a_cart_that_still_has_lines_never_replays_and_validates_the_body` | CONFIRMED |
| Empty cart (no token, unknown token, no lines, no recent order) still validates first and then answers `ORDERS_CHECKOUT_CART_EMPTY` | `checkout.py:203-210` | application | `test_checkout.py::test_no_cookie_*`, `test_an_unknown_token_*`, `test_a_cart_without_lines_is_empty`, `test_an_invalid_phone_is_refused_before_the_cart_is_even_read` | CONFIRMED |
| Link token is the derived one; only its digest and the cart digest are stored; `status` comes from the aggregate | `checkout.py:284-285,314-324` | application | `test_checkout.py::test_the_link_token_is_the_derived_one_and_only_its_digest_is_stored` | CONFIRMED |
| **R1** A retry that read the cart and then lost the cart lock to the first checkout (cart gone at the lock) answers the first order instead of `ORDERS_CHECKOUT_CART_CHANGED` | `checkout.py:301-308` | application | `test_checkout.py::test_a_retry_that_waited_on_the_cart_lock_gets_the_order_not_cart_changed` | CONFIRMED |
| **R1** A genuine change still refuses: cart gone at the lock with only an order older than 30 minutes, or ordered from another cart, gives `CART_CHANGED` | `checkout.py:270-272,305-308` | application | `test_checkout.py::test_a_cart_gone_at_the_lock_with_no_recent_order_is_still_a_changed_cart`, `test_a_cart_gone_at_the_lock_but_ordered_from_another_cart_is_a_changed_cart` | CONFIRMED |
| **R1** Over two real connections: the first checkout is held inside its transaction (cart locked, stock held), the retry reads the cart and is seen waiting on a lock in `pg_stat_activity`; after release both answer the same order: one order, one reservation, one event, counter 1001, cart gone | `checkout.py:270,301-308`, `sql_cart_repository.py` | integration | `test_checkout_flow.py::test_a_retry_that_waits_on_the_cart_lock_behind_the_first_checkout_gets_its_order` | CONFIRMED (not mutation-run: product code may not be reverted; without the final `match` the retry would get `CART_CHANGED`, by the code) |
| Replay on real adapters: same order and link, one row, one reservation, one event; none after the window or for another cart | `checkout.py:198`, `SqlOrderReader` | integration | `test_checkout_flow.py::test_a_retry_after_a_lost_response_gets_the_same_order_and_link`, `test_no_replay_after_the_payment_window_or_for_another_cart` | CONFIRMED |
| Replay over HTTP: the same cookie gets the same order back and the cookie is cleared | `router.py` | http | `test_checkout_http.py::test_after_a_success_the_same_cookie_gets_the_same_order_back` | CONFIRMED |
| `StockHoldsAdapter`: `INVENTORY_STOCK_UNAVAILABLE` keeps its details; `INVENTORY_PRESENTATION_NOT_STOCKED` and `_NOT_FOUND` become `CHECKOUT_CART_CHANGED`; any other error passes through | `stock_holds.py:13-34` | application | `test_stock_holds_adapter.py` (4 tests) | CONFIRMED |
| The same two inventory codes against the real inventory (made to order, nonexistent) | `stock_holds.py` | integration | `test_sql_orders.py::test_a_made_to_order_presentation_means_the_cart_changed`, `::test_a_presentation_that_does_not_exist_means_the_cart_changed` | CONFIRMED |
| `Delivery.create`: address required for home/local, forbidden for pickup (field `address`), pickup copies the point | `order.py:91` | domain | `test_order_domain.py::test_home_and_local_delivery_*`, `test_pickup_*` | CONFIRMED |
| `OrderLine.create`: line total = unit price x quantity | `order.py:52` | domain | `test_order_domain.py::test_an_order_line_total_is_the_unit_price_times_the_quantity` | CONFIRMED |
| `HmacOrderLinks`: stable for one id, differs across ids and secrets, unpadded base64url HMAC of `order-link:` + id bytes, digest is SHA-256 hex | `order_links.py` | application (pure adapter) | `test_order_links.py` (4 tests) | CONFIRMED |
| **T2** The configured `ORDER_LINK_SECRET` is what `HmacOrderLinks` receives through the module wiring (development, test and production), and differs from the development constant | `module.py:60-65` | wiring | `test_cart_wiring.py::test_the_configured_order_link_secret_is_what_the_links_are_derived_from` (3 cases) | CONFIRMED |
| **T2** Unset or empty secret outside production uses the development value | `module.py:41-42,60-65` | wiring | `test_cart_wiring.py::test_without_a_secret_outside_production_the_development_value_is_used` (4 cases) | CONFIRMED |
| Production refuses a missing, empty or under-32-character secret; 32 accepted; optional outside production | `config.py:92-97` | unit (settings) | `test_settings.py::test_production_requires_an_order_link_secret_of_32_characters`, `test_a_32_character_order_link_secret_is_accepted_in_production`, `test_the_order_link_secret_is_optional_outside_production` | CONFIRMED |
| `SqlOrderReader.placed_from_cart`: whole order incl. address, newest first, ignores old orders, other carts and other statuses; `cart_token_digest` stored | `sql_order_repository.py:100-` | integration | `test_sql_orders.py::test_placed_from_cart_*` (4 tests), `test_sql_orders.py:689-702` | CONFIRMED |
| Migration 0020: column absent before; upgrade adds nullable indexed varchar(64) and keeps old orders; downgrade drops column and index keeping orders; re-upgrade | `0020_orders_cart_token.py` | integration | `test_migration_0020_orders_cart_token.py` (4 tests) | CONFIRMED (upgrade and downgrade both run) |
| The `orders.orders` indexes are `customer_email` and `cart_token_digest` | `tables.py:76` | integration | `test_sql_orders.py:536` | CONFIRMED |

Not covered, by decision: R2 (the cart cookie is a bearer for the private link for 30 minutes) is
accepted with decision 31 and noted for plan 004; R3 is Deviation 3; L4 (`status` from the
aggregate) cannot be pinned by a failing test while `OrderStatus` has one value.

### First baseline 2026-10-16 (SUPERSEDED by the step 7 repair where the rows above replace it)

Baseline (before any test): `uv run just check` green (2790 passed, 3 skipped; harness 685
passed) and `uv run just test-integration` green (802 passed, 5 skipped). Tests added: 339 (domain
194, application 47, http 30, integration 68). No GAP and no NOT CONFIRMED were found: every
promised behavior exists in the code and was confirmed by a run. The integration conftest
(`tests/integration/orders/conftest.py`) now also empties `orders.orders`, resets the counter to
1000 and empties inventory's stock and reservation tables.

| Behavior (plan / code) | Source | Layer | Test | State |
| ---------------------- | ------ | ----- | ---- | ----- |
| Name 1/2/120/121 characters, whitespace collapsed | `customer.py:41-45` | domain | `test_customer_domain.py::test_a_name_*` | CONFIRMED |
| Email rule (strip, lowercase, one `@`, dot in domain, 254) | `customer.py:53-66` | domain | `test_customer_domain.py::test_a_malformed_email_is_refused`, `test_an_email_of_254_*` | CONFIRMED |
| Phone: `+52 998-123-4567` -> `9981234567`; `52` only with 12 digits; 9/11 digits, letters, non-ASCII refused | `customer.py:77-85` | domain | `test_customer_domain.py::test_every_phone_form_*`, `test_a_phone_that_is_not_ten_digits_*`, `test_a_ten_digit_number_starting_with_52_*` | CONFIRMED |
| 32 state codes accepted, others refused, code trimmed | `customer.py:88-121,256-258` | domain | `test_customer_domain.py::test_every_state_code_is_accepted`, `test_an_unknown_state_is_refused` | CONFIRMED |
| Address fields at their bounds, optional empty -> None, postal code re-wrapped, first error in form order | `customer.py:175-275` | domain | `test_customer_domain.py::test_an_address_field_*`, `test_an_empty_optional_field_*`, `test_the_first_broken_address_field_*` | CONFIRMED |
| SAT catalogs: 19 regimes, 22 uses, CP01/CN01 refused, every code accepted | `customer.py:124-172` | domain | `test_customer_domain.py::test_every_tax_regime_*`, `test_every_cfdi_use_*`, `test_a_cfdi_use_that_does_not_apply_*` | CONFIRMED |
| RFC 12/13 characters, `XAXX010101000`, `ABC123` refused; business name 1/254/255; invoice field order | `customer.py:286-315` | domain | `test_customer_domain.py::test_an_rfc_*`, `test_a_malformed_rfc_is_refused`, `test_a_business_name_*`, `test_the_first_broken_invoice_field_*` | CONFIRMED |
| `Order.place`: totals, lead time over made-to-order only, status, stamps, one `OrderPlaced`, `format_order_number` | `order.py:108-151` | domain | `test_order_domain.py` (13 tests) | CONFIRMED |
| Validation precedes the cart; contact, then address, then invoice; address required for home/local, forbidden for pickup | `checkout.py:84-132,181-185` | application | `test_checkout.py::test_each_contact_field_*`, `test_the_contact_is_checked_before_*`, `test_home_and_local_require_an_address`, `test_pickup_refuses_an_address`, `test_an_invalid_invoice_*` | CONFIRMED |
| Empty cart: no token, unknown token, no lines | `checkout.py:187-192` | application | `test_checkout.py::test_no_cookie_*`, `test_an_unknown_token_*`, `test_a_cart_without_lines_is_empty` | CONFIRMED |
| Cart with problems lists the lines (sold out, insufficient, unavailable, vanished) and wins over delivery and total | `checkout.py:196-208` | application | `test_checkout.py::test_a_cart_with_problems_*`, `test_a_line_whose_presentation_*`, `test_the_cart_problems_win_*` | CONFIRMED |
| Delivery unavailable (off-list code, switched-off method) wins over a wrong total; `Subtotal` error passes through | `checkout.py:210-225` | application | `test_checkout.py::test_local_delivery_off_*`, `test_a_switched_off_method_*`, `test_an_unavailable_delivery_wins_*`, `test_a_subtotal_over_the_quote_limit_*` | CONFIRMED |
| Total compared with `expected_total_cents`, real figures returned | `checkout.py:226-236` | application | `test_checkout.py::test_a_wrong_expected_total_*`, `test_an_expected_total_one_cent_off_*` | CONFIRMED |
| Cart changed between the read and the lock (gone, quantity, added line); a price acknowledgement is not a change | `checkout.py:255-257` | application | `test_checkout.py::test_a_cart_deleted_before_the_lock_*`, `test_a_quantity_changed_*`, `test_a_line_added_*`, `test_a_price_acknowledgement_*` | CONFIRMED |
| Hold only the in-stock lines, reference `order:<id>`, expiry now + 30 min; none when all made to order | `checkout.py:241-263` | application | `test_checkout.py::test_one_reservation_holds_only_*`, `test_a_cart_of_only_made_to_order_lines_*` | CONFIRMED |
| Stock refusal: inventory's lines, no order, cart kept, number not consumed, no event | `checkout.py:259-264` | application | `test_checkout.py::test_a_stock_refusal_*`, `test_a_refused_checkout_does_not_burn_a_number` | CONFIRMED (order of steps with fakes; the real rollback is in integration) |
| Happy path: number 1001 then 1002, token, totals, frozen lines, cart deleted, one event, pickup + invoice, free/local delivery | `checkout.py:238-300` | application | `test_checkout.py::test_a_home_order_*`, `test_the_lines_freeze_*`, `test_the_second_order_*`, `test_a_pickup_order_*`, `test_a_local_order_*` | CONFIRMED |
| Step order: reads outside the transaction, then lock, hold, number, add, delete | `checkout.py:187-283`, plan lock order | application | `test_checkout.py::test_the_steps_run_in_the_documented_order_*` | CONFIRMED |
| `StockHoldsAdapter` translates only `INVENTORY_STOCK_UNAVAILABLE`, keeps details | `stock_holds.py:21-27` | application | `test_stock_holds_adapter.py` (3 tests) | CONFIRMED |
| 201 shape, cookie cleared with the cart cookie's attributes (and Secure), kept on every refusal | `router.py:166-190` | http | `test_checkout_http.py::test_a_valid_checkout_*`, `test_success_clears_*`, `test_a_refused_checkout_keeps_the_cookie_*` | CONFIRMED |
| Every 409 code (`CART_EMPTY`, `HAS_PROBLEMS`, `CART_CHANGED`, `DELIVERY_UNAVAILABLE`, `TOTAL_CHANGED`, `STOCK_UNAVAILABLE`) with its details | `errors.py`, `router.py` | http | `test_checkout_http.py` (409 section) | CONFIRMED |
| 422 `ORDERS_CUSTOMER_DATA_INVALID` per field, and `VALIDATION_ERROR` for a malformed payload | `router.py`, `contracts.py:111-152` | http | `test_checkout_http.py::test_invalid_customer_data_*`, `test_a_malformed_payload_*` | CONFIRMED |
| OpenAPI: operation, 201/409/422, schemas, enums, error codes in the description | `router.py` | http | `test_checkout_http.py::test_the_openapi_*`, `test_the_documented_error_codes_*` | CONFIRMED |
| Gapless counter: seed 1001, consecutive, rollback gives the number back, a second transaction waits for the row | `sql_order_repository.py:12-22` | integration | `test_sql_orders.py::test_the_migration_seeds_*`, `test_a_rolled_back_transaction_gives_*`, `test_a_second_transaction_waits_*` | CONFIRMED |
| Order and lines stored as rows with every column; pickup/invoice nulls; no FK on `reservation_id`; rollback stores nothing | `sql_order_repository.py:24-83` | integration | `test_sql_orders.py::test_a_home_order_is_stored_*`, `test_the_lines_are_stored_*`, `test_a_pickup_order_*`, `test_a_rolled_back_add_*` | CONFIRMED |
| Constraints: unique number and digest, total check, method check, quantity 1-10, line total, position key, FK + cascade, email index | `tables.py`, migration 0019 | integration | `test_sql_orders.py` (constraints section) | CONFIRMED |
| `SqlCartRepository.delete` cascades its lines only, and rolls back | `sql_cart_repository.py` | integration | `test_sql_orders.py::test_deleting_a_cart_*`, `test_a_rolled_back_delete_*` | CONFIRMED |
| `StockHoldsAdapter` on the real inventory: reserved count, all or nothing, reference taken, made to order refused, joins the caller's transaction | `stock_holds.py`, inventory facade | integration | `test_sql_orders.py::test_a_hold_*`, `test_holding_the_same_reference_twice_*`, `test_a_made_to_order_presentation_*` | CONFIRMED |
| Full `PlaceOrder` on real adapters: order, lines, reservation of the in-stock lines only, cart gone, outbox event, numbers 1001/1002, made-to-order only, pickup + invoice, frozen price | `checkout.py` | integration | `test_checkout_flow.py` (happy-path section) | CONFIRMED |
| Refusals leave no rows (stock short, another order's hold, empty, total, delivery, phone, sold out) and a failure after the hold rolls back the reservation, the number and the cart | `checkout.py:254-283` | integration | `test_checkout_flow.py::test_a_stock_refusal_*`, `test_the_other_refusals_*`, `test_a_failure_after_the_hold_*` | CONFIRMED |
| Concurrency: two checkouts for the last units, one wins; five concurrent checkouts get 1001-1005 with no gap | counter row + inventory locks | integration | `test_checkout_flow.py::test_two_checkouts_*`, `test_concurrent_checkouts_*` | CONFIRMED |
| Migration 0019 on a scratch database: absent before, columns/seed/constraints/index, downgrade (also with data), re-upgrade reseeds the counter | `0019_orders_orders.py` | integration | `test_migration_0019_orders_orders.py` (20 tests) | CONFIRMED (upgrade and downgrade both run) |
| e2e | n/a | e2e | not applicable (no e2e infrastructure) | n/a |

## Review findings

### Re-review 2026-10-17 (after the step 7 repair)

Reviewer subagent, against `main...HEAD` with the repair commits `ee4c7ba` (fix) and `2dbb822`
(tests); worktree clean. The plan's status on arrival was `testing`.

#### Pass 1 — Checklist: 13/15 passed, 0 failed, 1 not applicable, 1 partial

- [x] `uv run just plans-scope` passes (28 declared, 41 changed, every change inside the plan).
      `apps/api/.importlinter` is append-only (the same two ignore lines as before).
- [x] `uv run just check` green: ruff clean, mypy 414 files clean, import-linter 7 kept / 0
      broken, plans OK, harness-check up to date, 3081 passed / 3 skipped, hook tests 685 passed.
- [x] Integration: the main session's full run after the repair (882 passed, 5 skipped) is
      taken as given; the reviewer re-ran `tests/integration/orders` live: 153 passed.
- [x] Business rules in `domain/`: `Delivery.create` (address required / forbidden by method)
      and `OrderLine.create` (line total) now live in `domain/order.py` (L3 resolved).
- [x] CQRS-lite: the replay lookup is a read port (`OrderReader.placed_from_cart`,
      `SqlOrderReader` over `Database.reader()`); the write side is unchanged.
- [x] Contracts unchanged; `openapi.json` regenerated (only the route description changed; drift
      check green).
- [x] Errors: `INVENTORY_PRESENTATION_NOT_STOCKED` / `_NOT_FOUND` now become
      `ORDERS_CHECKOUT_CART_CHANGED` (`stock_holds.py:13-16,30-31`, L1 resolved).
- [x] Money / time / ids: unchanged rules; replay window from `Clock`.
- [x] Migration `0020`: new file, only adds a nullable `varchar(64)` and a non-unique index in
      `orders`; index name `ix_orders_cart_token_digest` matches the metadata naming convention
      (`ix_%(table_name)s_%(column_0_N_name)s`) and `tables.py:76`; downgrade drops index then
      column; round trip proven on a scratch database (`test_migration_0020_orders_cart_token.py`).
- [x] Routes: unchanged (`public_router`).
- [x] Wiring: `PlaceOrder` gets `HmacOrderLinks` and `SqlOrderReader` in `module.py:60-65,111-112`;
      `test_container.py` green.
- [x] No secrets: the only secret literal is the documented development fallback
      (`module.py:42`, `# noqa: S105`); `.env.example` has an empty `ORDER_LINK_SECRET=`; tests
      use obvious dummy secrets.
- [x] `## Deviations` honest: spot-checked "`domain/repositories.py` therefore did not change"
      (`git diff c7f6221 HEAD` on it is empty) and "a pickup request with an INVALID address now
      reports the address field's own error" (`checkout.py:_validate_address` runs
      `Address.create` before `Delivery.create`).
- [~] **Partial — evidence:** `docs/architecture.md` orders row updated (secret, replay). But the
      `## Test coverage` section was declared superseded by the repair and not rewritten: rows
      are now stale (e.g. "`StockHoldsAdapter` translates only `INVENTORY_STOCK_UNAVAILABLE`",
      "Empty cart: ... unknown token" with no replay, line references into the old
      `checkout.py`). See T1.
- [ ] N/A: no PR exists for `feat/orders-checkout` yet (`gh pr list --head` empty).

#### Previous findings

- **B1 resolved.** `plans-scope` clean; the test directories are declared (step 6
  `Files (tests):` and step 7).
- **L1 resolved, with regression tests that fail without the fix:** unit
  `test_stock_holds_adapter.py::test_a_presentation_that_stopped_being_stocked_or_vanished_means_the_cart_changed`
  (both codes) and integration `test_sql_orders.py::test_a_made_to_order_presentation_means_the_cart_changed`,
  `::test_a_presentation_that_does_not_exist_means_the_cart_changed` against the real inventory.
- **L2 resolved, with regression tests that fail without the fix:** replay within 30 minutes
  (unit `test_checkout.py::test_a_retry_within_30_minutes_*`, http
  `test_after_a_success_the_same_cookie_gets_the_same_order_back`, integration
  `test_a_retry_after_a_lost_response_gets_the_same_order_and_link`: same body, one order, one
  reservation, one event, counter 1001); no replay after 30 minutes or for another cart (unit and
  integration); status filter, inclusive `since`, newest-first (integration `SqlOrderReader`
  tests); derived token (`test_order_links.py`); production validator (`test_settings.py`).
- **L3 resolved:** `Delivery.create` and `OrderLine.create` unit-tested in
  `test_order_domain.py`; the existing checkout tests pin the observable rule through them.
- **L4 resolved in code** (`checkout.py:_placed_order` uses `order.status.value`). It cannot be
  pinned by a test that fails without the fix while `OrderStatus` has one value; acceptable until
  plan 004 adds statuses.

#### Findings

**Blocking / High:** none.

**Medium (tests and evidence; no product code change)**

- **T1 — the testing phase for the repaired code is incomplete.** The repair's tests are real
  regression tests and cover every bullet of step 7's observable result, but they were written
  by the implementer and `## Test coverage` was not rewritten (only marked superseded), so the
  plan has no CONFIRMED/GAP mapping for the touched code and several rows contradict it now.
  Needed from the tester: refresh the table for `checkout.py` (replay, new precedence),
  `stock_holds.py`, `order.py` (`Delivery.create`, `OrderLine.create`), `order_links.py`,
  `sql_order_repository.py` (`SqlOrderReader`), `config.py` and migration 0020, and close T2/T3.
- **T2 — the configured secret's wiring is untested** (`modules/orders/module.py:60-65`). No test
  asserts that `ORDER_LINK_SECRET` is what `HmacOrderLinks` receives, or that the development
  constant is used only when it is unset/empty (`test_cart_wiring.py:42` and
  `test_http_platform.py:214` pass a secret but assert nothing about links; the integration
  flow builds its own `HmacOrderLinks`). Failure scenario: a refactor that always passes
  `_DEV_ORDER_LINK_SECRET` keeps every test green, and production then issues private links
  derived from a secret that is committed in the repo — anyone with an order id could compute its
  link. (Reasoned from the test inventory; not proven by a mutation run.)
- **T3 — "replay before validation" is not pinned** (`checkout.py:194-207`). The replay tests'
  bodies are schema- and domain-valid (only `expected_total_cents` is wrong, which is checked
  after validation anyway). Moving the replay after `_validate(...)` would keep every test green,
  although the Deviations and the route docstring promise "the request body is ignored". Failure
  scenario: a retry whose body now fails domain validation (e.g. the storefront re-sends with a
  phone the customer edited to 9 digits) gets 422 instead of its order. Needed: a replay test
  with a body that fails `ORDERS_CUSTOMER_DATA_INVALID`.

**Low (for the main session / plan 004 to decide; no change required to pass)**

- **R1 — a retry that races the in-flight first checkout gets `CART_CHANGED`, not the replay**
  (`checkout.py:270-272`). Scenario: double submit with the same cookie. Request B reads the cart
  outside the transaction while A holds the row lock (MVCC: B sees the cart with lines, so no
  replay), then B blocks on `get_by_token_for_update`, A commits and deletes the cart, B gets
  None → 409 `ORDERS_CHECKOUT_CART_CHANGED`. No duplicate order is possible (correct), and a
  further retry replays. If the storefront shows B's answer (A's was abandoned), the customer is
  told the cart changed although the order exists. Option: when the locked cart is gone, try
  `placed_from_cart` once after the rollback before refusing. Plan-conformant as written (step 3:
  "when it is gone ... refuse CART_CHANGED"); not tested either way.
- **R2 — the cart cookie is now a bearer for the order's private link for 30 minutes.** Whoever
  holds the cart token (same browser in practice; the cookie is cleared on success) gets the
  order's `access_token` via the replay. Inherent to decision 31; recorded for plan 004's
  threat notes. No action.
- **R3 — a pickup request with an invalid address reports the inner field** (e.g.
  `address.state`) instead of `address`; documented in Deviations; acceptance criterion
  "pickup with an address → `address`" still holds for a valid address. No action.

Bug hunt traced: token → cart digest → reader read → replay lookup (digest, `pending_payment`,
`created_at >= now - 30 min`, newest by `created_at`, `number`) → validation → cart/offers/quote
/total → `Delivery.create` (real cost and pickup point) → lock + pairs → hold (new inventory
translations roll everything back) → counter → `Order.place` with derived link digest and cart
digest → insert (new column) → cart delete → outbox → `PlacedOrder` (token regenerated from the
id, status from the aggregate). HMAC: key `secret.encode()`, message `b"order-link:" + 16 id
bytes`, 43-char base64url token, SHA-256 hex digest stored under the existing unique constraint;
production refuses a missing, empty or <32-character secret at startup; `development`/`test`
fall back to the documented constant (`AppEnv` has no other value). A new cart never reuses an
old token (`SetCartLine` mints a fresh token for an unknown one, `cart.py:73-79`), so a replay
cannot shadow a new cart. Replays read only committed rows: A's order insert and cart delete
commit together, so there is no window where the cart is gone and the order invisible.

**Result:** no product-code defect found; the repair is correct as far as reviewed. The plan
stays in `testing` (unchanged): T1–T3 are test and evidence work for the tester, after which
review repeats on the refreshed coverage (or the main session decides T2/T3 are not required and
records it). Not moved to `verify`.

### First review 2026-10-16 (superseded by the step 7 repair)

Reviewed 2026-10-16 (reviewer subagent) against `main...HEAD` (commits `018a0cd`, `c7f6221`,
`76cbd52`; worktree clean).

### Pass 1 — Checklist: 13/15 passed, 1 FAILED, 1 not applicable

- [ ] **FAILED: `uv run just plans-scope`** exits 1. Nine test files are out of scope because
      no step declares a test path: `apps/api/tests/integration/orders/{conftest.py,
      test_checkout_flow.py, test_migration_0019_orders_orders.py, test_sql_orders.py}` and
      `apps/api/tests/unit/orders/{test_checkout.py, test_checkout_http.py,
      test_customer_domain.py, test_order_domain.py, test_stock_holds_adapter.py}`. The hot file
      `apps/api/.importlinter` is genuinely append-only (two ignore lines at the end, exactly the
      two the plan names).
- [x] `uv run just check` green: ruff clean, mypy 410 files clean, import-linter 7 kept / 0
      broken, plans OK, harness-check up to date, 3061 passed / 3 skipped, hook tests 685 passed.
      `python -m fragancia_api.main.openapi --check`: up to date.
- [x] `uv run just test-integration` green: 870 passed, 5 skipped (live run, 185 s).
- [x] Business rules in `domain/` (customer fields, SAT catalogs, `Order.place` totals and lead
      time). See Low finding L3 on two small rules left in the application layer.
- [x] CQRS-lite: `PlaceOrder` reads outside, then one `transactions.run(work)` going aggregate →
      `OrderRepository` / `CartRepository`, returning `Result`; no screen-specific repository
      methods.
- [x] Contracts in `contracts.py`; `openapi.json` regenerated (drift check passes).
- [x] Errors: `Err(DomainError)` subclasses with `ORDERS_CHECKOUT_*` /
      `ORDERS_CUSTOMER_DATA_INVALID` codes (409 / 422 via `shared/http/errors.py:54-55`). See L1
      on inventory codes that can pass through.
- [x] Money as integer cents (same raw-int style as the cart and delivery), datetimes from
      `Clock`, ids from `new_id()`.
- [x] Migration `0019`: new file, only `orders` schema, no cross-schema FKs (`reservation_id`,
      `presentation_id`, `perfume_id` unconstrained by design), seed row, reversible downgrade
      (exercised by `test_migration_0019_orders_orders.py` on a scratch database).
- [x] Route in `public_router(prefix="/checkout")`; no bare `APIRouter`; no admin route added.
- [x] Wiring: `PlaceOrder` registered in `module.py`; `StockReservations` resolved from the
      registry (inventory precedes orders in `MODULES`); `test_container.py` green.
- [x] No secrets, `.env` content or real personal data (fixtures use `XAXX010101000`,
      `ana@example.com`).
- [x] `## Deviations` honest on the claim spot-checked: `Subtotal` errors do pass through as
      `ORDERS_DELIVERY_OPTIONS_INVALID` and the route docstring says so (`router.py`
      `place_order`). It does not mention the undeclared test paths (see B1).
- [x] Docs: `docs/architecture.md` orders row and the orders `__init__.py` docstring updated;
      `docs/modules.json` description still accurate; `apps/api/README.md` has no orders section.
- [ ] N/A: no PR exists yet for `feat/orders-checkout` (`gh pr list --head` empty); the six
      sections must be checked when it is opened.

### Findings

**Blocking (checklist)**

- **B1 — plan scope: test files undeclared.** `plans/orders-checkout/003-checkout-order.md:371`
  (step 6 `Files:`) lists only `docs/architecture.md` and the orders `__init__.py`, although the
  step says "the tester writes the layered tests". `plans-scope` therefore fails on the nine test
  files above. Plan 002 declared `apps/api/tests/unit/orders/` and
  `apps/api/tests/integration/orders/` (002 lines 249-251). The tests themselves are in place and
  appropriate; the defect is the plan's file list. Repair (plan-only, no code): the main session
  records a deviation and adds `apps/api/tests/unit/orders/` (modify) and
  `apps/api/tests/integration/orders/` (modify) to a `Files:` line, then re-runs
  `uv run just plans-scope plans/orders-checkout/003-checkout-order.md`. Note that the
  integration conftest now also `TRUNCATE`s inventory's tables (`fragancia_test` only); that
  cross-module test fixture belongs in the declared list too.

**High / Medium:** none found.

**Low (no code change required to pass; for the main session / plan 004 to decide)**

- **L1 — inventory codes can reach the storefront.** `infrastructure/stock_holds.py:25` passes
  every non-`INVENTORY_STOCK_UNAVAILABLE` error through unchanged (as the plan specifies).
  Scenario: the owner switches a presentation from in stock to made to order (or hides/deletes
  it) between the offers read (`checkout.py`, outside the transaction) and the hold;
  `ReserveStock` returns `INVENTORY_PRESENTATION_NOT_STOCKED` / `_NOT_FOUND`
  (`inventory/application/commands/stock.py:37-41`) and the storefront receives an inventory
  code the route docstring does not list. Rollback is correct (nothing is written); only the
  error contract is affected. Mapping these to `ORDERS_CHECKOUT_CART_CHANGED` would keep the
  public contract closed. Plan-sanctioned, so not a defect against this plan.
- **L2 — lost response leaves an order without its private link.** If the 201 response is lost
  (network drop), the cart is already deleted and the access token was shown only in that
  response (`PlacedOrder.access_token`). A retry gets `ORDERS_CHECKOUT_CART_EMPTY`; the order
  holds stock until expiry and the customer cannot open it. No duplicate order can be created
  (verified: a concurrent retry gets `CART_CHANGED` after the lock, a later one `CART_EMPTY`).
  Mitigated by plan 004's lookup by number and email and plan 005's confirmation email;
  recorded so those plans keep it in mind.
- **L3 — two small rules live in the application layer.** "Address required for home/local,
  forbidden for pickup" (`checkout.py:84-96`) and `line_total_cents = price * quantity`
  (`checkout.py:316`) are business rules outside `domain/`. The plan placed the first in the
  application step explicitly; both are covered by tests. Optional move to the domain later.
- **L4 — response status hard-coded.** `checkout.py:293` returns `status="pending_payment"`
  instead of `order.status.value`; harmless today (one status) but will drift when plan 004
  adds statuses.

Bug hunt traced: request validation → contact/address/invoice → cart read and offers outside
the transaction → `cart_view` problems → quote and total check → lock + pairs comparison →
hold (in-stock only, `order:<id>`, expiry = `payment_due_at`) → gapless counter (row lock held
to commit) → insert with frozen prices → cart delete (lines cascade) → outbox event in the same
transaction → 201 + cookie cleared with the cart cookie's attributes. Money: subtotal equals
the sum of line totals from the same offers used by `cart_view`; total check constraint
matches. Concurrency: double submission cannot create two orders; competing carts for the last
units resolve in inventory (integration-tested); lock order cart → inventory → counter has no
cycle with existing writers. No state can be reached that the (single-state) lifecycle forbids.

**Result:** the code is correct as far as reviewed, but the checklist failed on B1, so the
status stays `review`. B1 needs a plan edit (and a re-run of `plans-scope`), not a code change.

## Verification
