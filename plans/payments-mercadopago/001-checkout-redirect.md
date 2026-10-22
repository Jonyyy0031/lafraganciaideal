---
status: testing
module: payments
min_implementer: mid
depends_on: ["orders-checkout/006"]
---

# 001 — Payments module and checkout redirect

## Context

A pending order has no way to be paid yet. This plan creates the `payments` module (ADR 0004)
with its `PaymentGateway` port and the Mercado Pago Checkout Pro adapter.

**What the guest gets.** From the order page, the guest asks for a payment link. The API
creates a Mercado Pago *preference* for the order's frozen total and answers its redirect URL.
- Cards only, in one installment (decision 4). OXXO and SPEI come in plan 003.
- A second request reuses the link while it is valid.

**Out of this plan.** Confirming the payment (the webhook) is plan 002. Nothing here moves an
order to paid.

**What exists today:**
- **The module is planned, not created.** `payments` is registered as `planned`
  (`docs/modules.json:9`). `MODULES` is `(identity, catalog, inventory, orders)`
  (`apps/api/src/fragancia_api/container.py:34`). The recipe is `docs/recipes/new-module.md`
  (§1 register, §2 layers, §3 wiring, §4 database, §5 talking to another module).
- **How a guest reaches an order.** The guest holds the order's private-link token. Orders
  looks it up only by its digest (`modules/orders/application/queries/customer_order.py:7-20`;
  `modules/orders/application/ports.py:125-133`), and the token travels only in request bodies
  (orders-checkout decision 42).
- **Orders exports no facade yet** (`modules/orders/__init__.py:34`, "Public API for other
  modules: nothing yet"). The pattern to copy is inventory's `StockReservations`
  (`modules/inventory/application/stock_reservations.py:19-72`), consumed by orders through a
  port and an adapter (`modules/orders/infrastructure/stock_holds.py`), with two `.importlinter`
  ignore lines.
- **Domain errors** have categories for 404, 409, 422, 401 and 429 only
  (`apps/api/src/fragancia_api/shared/kernel/errors.py:15-50`;
  `shared/http/errors.py:50-57`). There is no category for an unavailable upstream.
- **`httpx` is already a dependency** (`apps/api/pyproject.toml:13`).
- **Mercado Pago Checkout Pro**, checked on 2026-10-22 against the official SDK types
  (`mercadopago/sdk-nodejs` `src/clients/preference/commonTypes.ts`) and the API reference
  ("create preference"):
  - The endpoint is `POST https://api.mercadopago.com/checkout/preferences`, with
    `Authorization: Bearer <access token>`.
  - Request fields:
    - `items[]`: `id`, `title`, `quantity`, `unit_price` (a number in pesos) and
      `currency_id`;
    - `payer` (`name`, `email`);
    - `external_reference`;
    - `back_urls` (`success`, `pending`, `failure`) and `auto_return` (`"approved"`);
    - `payment_methods`: `excluded_payment_types[]` (`{id}`) and `installments`;
    - `expires`, `expiration_date_from` and `expiration_date_to` (ISO 8601);
    - `notification_url`, `date_of_expiration` and `binary_mode`.
  - The response has `id`, `init_point` and `sandbox_init_point`.

**Approach.**
- **Orders exports a query facade, `OrderPayments`,** with `payable(access_token)`. It returns
  what a payment needs: id, number, status, payment deadline, customer name and email, lines,
  delivery cost and total. Payments reads it through its own port.
- **`StartCheckout(access_token)` in payments:**
  1. Read the payable order; it must be `pending_payment` with its deadline in the future.
  2. Reuse the stored checkout for that order while it is valid.
  3. Otherwise call `PaymentGateway.create_checkout` **outside any transaction** (an HTTP call),
     then upsert the checkout row.
  4. Answer the redirect URL.
- **The preference expires at the order's `payment_due_at`** (`expires: true`), so a stale link
  cannot pay an order the overdue job cancelled.
- **`external_reference` is the order id.** Plan 002 uses it to match the payment.
- **Rejected:** creating the preference at checkout time (orders-checkout 003). That would put
  an external HTTP call inside the order transaction, and orders would depend on payments.

## Out of scope

- **The webhook and moving orders to paid** (002). This plan sends no `notification_url`.
- **OXXO and SPEI** (003): they are excluded here.
- **Refunds** (004).
- **The web pages** (phase 3).
  - Every back URL is `{STOREFRONT_URL}/pedido/pago`. Mercado Pago appends query parameters
    to it, so the token cannot ride in it; the web page keeps the token in `sessionStorage`
    before redirecting (decision 7).
  - The browser redirect never confirms a payment (ADR 0004).
- **The production credentials of the client:** only the settings change (decision 1).

## Dependencies

- **orders-checkout/006 (done):** the order lifecycle, `payment_due_at`, the private link, and
  the token in the body.

## Steps

1. **Register the module, and add the shared "unavailable" category**
   - Files: `docs/modules.json` (modify),
     `apps/api/.importlinter` (modify),
     `apps/api/src/fragancia_api/container.py` (modify),
     `apps/api/src/fragancia_api/shared/kernel/errors.py` (modify),
     `apps/api/src/fragancia_api/shared/kernel/__init__.py` (modify),
     `apps/api/src/fragancia_api/shared/http/errors.py` (modify)
   - Do:
     - **Register it.** Set `payments` to `active` in `docs/modules.json`. Add its
       `module-layers` container in `.importlinter`, as the recipe's §1 does. Append `payments`
       to `MODULES` after `orders`.
     - **`ServiceUnavailableError(DomainError)`:** "An upstream service failed or is not
       configured; try again later (HTTP 503)". Export it from the kernel, and map it to 503 in
       `_STATUS_BY_CATEGORY`.
   - Observable result: `uv run just arch` passes, and a unit test maps the new category to 503.

2. **Orders: the `OrderPayments` facade**
   - Files: `apps/api/src/fragancia_api/modules/orders/application/queries/order_payments.py` (create),
     `apps/api/src/fragancia_api/modules/orders/application/ports.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_customer_orders.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/in_memory.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/module.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/__init__.py` (modify)
   - Do:
     - **`PayableOrder`,** frozen:
       - `order_id`, `number` (`"LFI-1001"`), `status` (str) and `payment_due_at`;
       - `customer_name` and `customer_email`;
       - `lines: list[PayableLine(presentation_id, label, quantity, unit_price_cents)]`;
       - `delivery_cents` and `total_cents`.
     - **`OrderPayments(queries, links)`** with
       `payable(access_token) -> PayableOrder | None`, which digests the token. The docstring
       says it is the only way other modules read an order for payment.
     - **The query port.** `CustomerOrderQueries` gains `payable_by_access_digest(digest)`,
       implemented in `SqlCustomerOrders` (order row plus lines by position) and in the
       in-memory fake.
     - **Wiring and export.** Register the facade in `module.py`. Export `OrderPayments`,
       `PayableOrder` and `PayableLine` from `orders/__init__.py`, and replace "Public API: nothing
       yet" with a description.
   - Observable result: unit and integration tests read a payable order by token. An unknown
     token gives None.

3. **Payments domain and application**
   - Files: `apps/api/src/fragancia_api/modules/payments/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/payments/domain/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/payments/domain/checkout.py` (create),
     `apps/api/src/fragancia_api/modules/payments/domain/errors.py` (create),
     `apps/api/src/fragancia_api/modules/payments/domain/repositories.py` (create),
     `apps/api/src/fragancia_api/modules/payments/application/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/payments/application/ports.py` (create),
     `apps/api/src/fragancia_api/modules/payments/application/commands/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/payments/application/commands/start_checkout.py` (create),
     `apps/api/src/fragancia_api/modules/payments/contracts.py` (create)
   - Do:
     - **Domain** (`checkout.py`):
       - `Checkout(order_id, preference_id, redirect_url, expires_at, created_at)`;
       - `REUSE_MARGIN = timedelta(minutes=2)`;
       - `checkout.reusable(now)` is `expires_at - REUSE_MARGIN > now`.
     - **Errors:**

       | Error | Code | Status |
       | ----- | ---- | ------ |
       | `PaymentOrderNotFound(NotFoundError)` | `PAYMENTS_ORDER_NOT_FOUND` | 404 |
       | `PaymentOrderNotPayable(ConflictError)` | `PAYMENTS_ORDER_NOT_PAYABLE` | 409 |
       | `PaymentGatewayUnavailable(ServiceUnavailableError)` | `PAYMENTS_GATEWAY_UNAVAILABLE` | 503 |

       - Not payable means not `pending_payment`, or past `payment_due_at`. Its message is
         "Este pedido ya no se puede pagar", and `details` is `{"status"}`.
       - The gateway error covers Mercado Pago failing, timing out, refusing, or not being
         configured. Its message is "No pudimos conectar con Mercado Pago; intenta de nuevo".
     - **`CheckoutRepository`:** `get_for_order(order_id)` and `upsert(checkout)`. It joins the
       active transaction.
     - **Ports:**
       - `PayableOrders.payable(access_token) -> PayableOrder | None`, payments' own copy of the
         order facts, implemented over `OrderPayments`.
       - `PaymentGateway.create_checkout(request) -> Result[CheckoutLink, PaymentGatewayUnavailable]`.
         - The request has `order_id`, `number`, the payer's name and email, `items`
           (title, quantity, unit_price_cents), `expires_at` and `back_url`.
         - `CheckoutLink` has `preference_id` and `redirect_url`.
     - **`StartCheckout(access_token)`:**
       1. Read the payable order. None gives `PAYMENTS_ORDER_NOT_FOUND`.
       2. Not `pending_payment`, or `payment_due_at <= now`, gives `PAYMENTS_ORDER_NOT_PAYABLE`.
       3. Read the stored checkout. When it is `reusable(now)`, answer its URL.
       4. Otherwise build the items: one per line, plus "Envío" when `delivery_cents > 0`. Their
          sum must equal `total_cents`; assert it.
       5. Call the gateway outside any transaction, with `expires_at = payment_due_at`.
       6. `transactions.run` upserts the checkout, and the URL is answered.
     - **Contracts:** `StartCheckoutRequest(access_token: str, max 200)` and
       `StartCheckoutResponse(redirect_url: str)`.
   - Observable result: application tests with fakes cover:
     - not found, not payable (each status, and past the deadline);
     - reuse inside the margin, and a new link after it;
     - the items, including shipping, summing to the total;
     - a gateway failure that stores nothing.

4. **Infrastructure: the Mercado Pago adapter, tables and settings**
   - Files: `apps/api/src/fragancia_api/config.py` (modify),
     `apps/api/.env.example` (modify),
     `apps/api/src/fragancia_api/modules/payments/infrastructure/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/payments/infrastructure/tables.py` (create),
     `apps/api/src/fragancia_api/modules/payments/infrastructure/sql_checkout_repository.py` (create),
     `apps/api/src/fragancia_api/modules/payments/infrastructure/mercadopago_gateway.py` (create),
     `apps/api/src/fragancia_api/modules/payments/infrastructure/order_payments.py` (create),
     `apps/api/src/fragancia_api/modules/payments/infrastructure/in_memory.py` (create),
     `apps/api/migrations/versions/0023_payments_checkouts.py` (create)
   - Do:
     - **Settings** (decision 1):
       - `mercadopago_access_token: SecretStr | None = None`, required in production (the
         `ORDER_LINK_SECRET` validator style);
       - `mercadopago_api_url: str = "https://api.mercadopago.com"`, so tests and local QA can
         point at a fake;
       - `mercadopago_sandbox: bool = True`, which must be false in production.

       Add `MERCADOPAGO_ACCESS_TOKEN=` (with a comment: TEST- credentials of the developer's
       test application, never committed), `MERCADOPAGO_API_URL` and `MERCADOPAGO_SANDBOX` to
       `.env.example`.
     - **`payments.checkouts`:**
       - `order_id` (uuid, PK; no FK, it is orders' schema);
       - `preference_id` (varchar 64), `redirect_url` (varchar 500);
       - `expires_at` and `created_at` (timestamptz).

       Migration `0023` creates the `payments` schema and the table, and it is reversible.
     - **`SqlCheckoutRepository`:** `upsert` is `INSERT … ON CONFLICT (order_id) DO UPDATE`.
     - **`MercadoPagoGateway(api_url, access_token | None, sandbox, client: httpx.AsyncClient)`:**
       - With no token, it answers `PaymentGatewayUnavailable` (logged once per call as
         `payments.gateway_not_configured`).
       - It sends `POST {api_url}/checkout/preferences` with a 10 s timeout and this body:
         - `items`: each one with `id` (presentation or `"shipping"`), `title`, `quantity`,
           `unit_price` as cents / 100 (a JSON number, two decimals) and `currency_id: "MXN"`;
         - `payer: {name, email}` and `external_reference: str(order_id)`;
         - `back_urls` (all three the given back URL) and `auto_return: "approved"`;
         - `payment_methods: {excluded_payment_types: [{id: "ticket"}, {id: "atm"}, {id: "bank_transfer"}], installments: 1}`
           (decision 4; plan 003 lifts the first three);
         - `expires: true` and `expiration_date_to: expires_at` (ISO 8601 with offset).
       - It answers `CheckoutLink(id, sandbox_init_point if sandbox else init_point)`.
       - A transport error, a timeout, a non-2xx answer or a body missing those fields gives
         `PaymentGatewayUnavailable`. It logs `payments.gateway_error` with the status, and
         never the token.
     - **`OrderPaymentsAdapter`** implements `PayableOrders` over `OrderPayments`.
     - **In-memory:** `InMemoryCheckouts`, `FakeGateway` (records requests, can fail) and
       `InMemoryPayableOrders`.
   - Observable result:
     - integration tests run the repository upsert and the migration round trip on a scratch
       database;
     - adapter tests with `httpx.MockTransport` check the exact request body and headers, the
       sandbox and production URL choice, and every failure mode.

5. **HTTP, wiring and docs**
   - Files: `apps/api/src/fragancia_api/modules/payments/http/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/payments/http/router.py` (create),
     `apps/api/src/fragancia_api/modules/payments/module.py` (create),
     `apps/api/openapi.json` (modify),
     `docs/architecture.md` (modify),
     `apps/api/tests/unit/payments/` (create), `apps/api/tests/integration/payments/` (create),
     `apps/api/tests/unit/orders/` (modify), `apps/api/tests/integration/orders/` (modify),
     `apps/api/tests/unit/test_settings.py` (modify), `apps/api/tests/unit/test_http_platform.py` (modify)
   - Do:
     - **The route.** `public_router(prefix="/payments", tags=["payments"])` with
       `POST /checkout`. It takes `StartCheckoutRequest` and answers `StartCheckoutResponse`,
       declaring 404, 409 and 503. The docstring names decisions 2 and 4 and ADR 0004, and says
       the token is a secret in the body.
     - **`module.py`:**
       - build the gateway with one `httpx.AsyncClient` owned by the module;
       - pass the back URL `f"{settings.storefront_url}/pedido/pago"` and the `OrderPayments`
         facade from the registry;
       - wire the repository, `StartCheckout` and the router.
     - **`.importlinter`** gains two ignore lines:
       - `fragancia_api.modules.payments.infrastructure.order_payments -> fragancia_api.modules.orders`
       - `fragancia_api.modules.payments.module -> fragancia_api.modules.orders`
     - **Docs.** Regenerate `openapi.json`. Extend `docs/architecture.md`: the payments row,
       and the example paid flow that names this route.
     - The tester writes the layered tests.
   - Observable result: `uv run just check` and `uv run just test-integration` pass.

## Acceptance criteria

Run with the API on the development database. Delivery options are configured, and an order is
placed through `POST /checkout`.
- **Against Mercado Pago:** without credentials, the API points `MERCADOPAGO_API_URL` at a local
  fake server (a QA script) that answers like the preferences endpoint.
- **With the developer's test credentials:** against the real sandbox.

- [ ] **Link.**
  - `POST /api/v1/payments/checkout` with the order's token answers 200 with a `redirect_url`.
  - The fake (or Mercado Pago) received one preference:
    - items for the lines and "Envío", summing to the total, in MXN;
    - `external_reference` = the order id;
    - tickets, ATM and bank transfer excluded, and `installments: 1`;
    - `expiration_date_to` = the order's `payment_due_at`.
  - One `payments.checkouts` row exists.
- [ ] **Reuse.** A second call within the window answers the same URL, and no second preference
  is created.
- [ ] **Refusals.**
  - An unknown token gives 404 `PAYMENTS_ORDER_NOT_FOUND`.
  - A cancelled order, or one past its deadline, gives 409 `PAYMENTS_ORDER_NOT_PAYABLE`.
  - The fake answering 500, or no access token configured, gives 503
    `PAYMENTS_GATEWAY_UNAVAILABLE` and stores nothing.
- [ ] **Sandbox (needs decision 1's account).** The `redirect_url` opens Mercado Pago's sandbox
  checkout, showing the order's total and only card payment. Until the account exists, this
  stays NOT VERIFIED.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `Checkout.reusable` at the margin; the error categories (503 mapping) |
| application | yes     | `StartCheckout`: not found, not payable per status and deadline, reuse, new link after expiry, items and shipping summing to the total, gateway failure stores nothing; `OrderPayments.payable` |
| http        | yes     | The route's statuses and codes, the token in the body, OpenAPI |
| integration | yes     | `SqlCheckoutRepository` upsert; `payable_by_access_digest` over real orders; `MercadoPagoGateway` request body, headers, URL choice and failures via `httpx.MockTransport`; settings validators; migration 0023 round trip on a scratch database |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

- **`PayableOrder` / `PayableLine` live in `orders/application/ports.py`,** not in
  `queries/order_payments.py` (step 2). The `CustomerOrderQueries` port returns them, and the
  query module imports the ports, so defining them in the query module would be a cycle.
  `order_payments.py` and `orders/__init__.py` re-export them. Cosmetic.
- **Existing tests updated because production settings now require Mercado Pago** (step 4's
  validators), outside the plan's Files lines:
  - `apps/api/tests/unit/orders/test_cart_wiring.py`: production cases pass a token and
    `mercadopago_sandbox=False`.
  - `apps/api/tests/unit/orders/test_customer_order.py`: the `LoggingQueries` wrapper gained
    `payable_by_access_digest` (the port grew a method).
- `apps/api/tests/unit/test_settings.py` gained three minimal settings tests alongside the
  fixes to the production cases (it is in step 5's list).

## Test coverage

## Review findings

## Verification
