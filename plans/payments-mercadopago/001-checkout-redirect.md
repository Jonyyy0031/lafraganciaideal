---
status: verify
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

6. **Review repair (added 2026-10-23 after the first review)**
   - Files: `apps/api/src/fragancia_api/modules/payments/infrastructure/mercadopago_gateway.py` (modify),
     `apps/api/src/fragancia_api/modules/payments/module.py` (modify),
     `apps/api/src/fragancia_api/modules/payments/domain/checkout.py` (modify),
     `apps/api/src/fragancia_api/modules/payments/domain/errors.py` (modify),
     `apps/api/src/fragancia_api/modules/payments/application/commands/start_checkout.py` (modify),
     `apps/api/src/fragancia_api/config.py` (modify),
     `apps/api/README.md` (modify),
     `apps/api/openapi.json` (modify),
     `apps/api/tests/unit/payments/` (modify), `apps/api/tests/integration/payments/` (modify),
     `apps/api/tests/unit/test_settings.py` (modify)
   - Do:
     - **M1, the HTTP client.** The gateway opens an `httpx.AsyncClient` per call, with
       `async with`, a 10 s timeout and `trust_env=False`. It copies the catalog downloader
       (`modules/catalog/infrastructure/http_image_downloader.py:169-172`).
       - For tests, the constructor takes a `transport: httpx.AsyncBaseTransport | None`
         instead of a client.
       - `module.py` no longer creates a long-lived client, so nothing is left unclosed.
     - **M2, the timestamp.** `expiration_date_to` is sent with milliseconds and the offset,
       `expires_at.isoformat(timespec="milliseconds")` (for example
       `2026-10-23T15:32:44.222-05:00`), as Mercado Pago documents.
     - **L1, reuse without a margin.** Drop `REUSE_MARGIN`. A stored checkout is reused when
       `expires_at == order.payment_due_at and expires_at > now`. A new link is made only when
       there is none, or when the deadline changed (plan 003 extends it).
     - **L2.** `PaymentOrderNotFound` says "No encontramos ese pedido".
     - **L3.** `apps/api/README.md:62` lists 503 (upstream unavailable) among the error
       categories.
     - **L4.** Replace the bare `assert` on the items' sum with an explicit check that raises
       `ValueError` (a programming error, never a guest error).
     - **L5.** In production, `mercadopago_api_url` must start with `https://`.
     - **Info, for plan 002.** One preference can be paid more than once, and concurrent
       requests can create two. Plan 002 matches payments by `external_reference` and handles
       a second approved payment for an order that is already paid. This is recorded in the
       initiative README's "Dependency notes".
     - **Tests.** Update the MockTransport tests to inject a transport, the timestamp
       expectations and the reuse tests, and add tests for L4 and L5.
   - Observable result: `check`, `test-integration` and `plans-scope` are clean.

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

Baseline (before the tests): `uv run just check` green (3621 passed, 3 skipped; harness 685
passed); `uv run just test-integration` green (1040 passed, 5 skipped). No GAP and no NOT
CONFIRMED found: every behavior below was confirmed from the code and by running it.

The `MercadoPagoGateway` tests run under `tests/unit/payments/` (they use `httpx.MockTransport`,
no network and no database), so they run in `uv run just check`, not only in the integration
suite. The plan lists them under integration; the layer is a floor, and the test is not weaker.

| Behavior | Source | Layer | Test | State |
| -------- | ------ | ----- | ---- | ----- |
| `reusable` is `expires_at - 2 min > now`: far, +1 s outside, exactly at, inside, expired | `domain/checkout.py:8-20` | domain | `unit/payments/test_checkout_domain.py::test_a_checkout_*`, `test_an_expired_checkout_is_not_reusable` | CONFIRMED |
| The gateway error is a `ServiceUnavailableError`, code and message; the category maps to 503; the other two errors keep 404/409 categories | `domain/errors.py`, `shared/http/errors.py:57` | domain | `test_checkout_domain.py::test_the_unavailable_category_maps_to_503` and siblings | CONFIRMED |
| Unknown token gives `PAYMENTS_ORDER_NOT_FOUND`, no gateway call | `start_checkout.py:67-69` | application | `unit/payments/test_start_checkout.py::test_an_unknown_token_is_order_not_found` | CONFIRMED |
| Not payable for every status but `pending_payment`; `details.status` | `start_checkout.py:71-72` | application | `test_an_order_that_is_not_pending_payment_is_not_payable[*]` | CONFIRMED |
| Not payable at or past `payment_due_at` (0 s, -1 s, -2 h), payable 1 s before | `start_checkout.py:71` | application | `test_an_order_at_or_past_its_deadline_is_not_payable`, `test_an_order_one_second_before_its_deadline_is_still_payable` | CONFIRMED |
| A valid stored link is not reused once the order is not payable | `start_checkout.py:71-72` order | application | `test_a_valid_stored_link_is_not_reused_for_an_order_that_is_no_longer_payable` | CONFIRMED |
| New link: stored with `expires_at = payment_due_at`; gateway request carries order id, number, payer, deadline, back URL | `start_checkout.py:85-113` | application | `test_a_payable_order_gets_a_link_*`, `test_the_gateway_receives_the_order_facts_*` | CONFIRMED |
| The gateway is called outside any transaction | `start_checkout.py:85` (before `run(store)`) | application | `test_the_gateway_is_called_outside_the_transaction` | CONFIRMED |
| Items: one per line plus "Envío" (id `shipping`, qty 1) summing to the total; none when delivery is 0; a mismatch is an assertion that never reaches the gateway | `start_checkout.py:25-39` | application | `test_the_items_are_the_lines_plus_the_shipping_*`, `test_a_free_delivery_adds_no_shipping_item`, `test_items_that_do_not_add_up_*`, `test_a_total_mismatch_never_reaches_the_gateway` | CONFIRMED |
| Reuse: second call same URL with one gateway call; reuse just outside the 2 min margin; new link at/inside the margin (upsert replaces); reuse until 28 of 30 min | `start_checkout.py:76-84`, `checkout.py:20` | application | `test_a_second_request_reuses_*`, `test_a_checkout_just_outside_the_margin_is_reused`, `test_a_checkout_at_or_inside_the_margin_is_replaced_*`, `test_a_link_is_reused_until_two_minutes_*` | CONFIRMED |
| Gateway failure stores nothing and keeps the stored row | `start_checkout.py:100-101` | application | `test_a_gateway_failure_is_answered_and_stores_nothing`, `test_a_gateway_failure_keeps_the_expiring_checkout_*` | CONFIRMED |
| `OrderPayments.payable`: unknown token and the stored digest used as a token give None; facts, lines, figures; per-token | `orders/.../order_payments.py`, `in_memory.py` | application | `unit/orders/test_order_payments.py` (7 tests) | CONFIRMED |
| Exact preference body: items (id, title, quantity, `unit_price` pesos float, `MXN`), payer, `external_reference`, three back URLs, `auto_return`, excluded `ticket`/`atm`/`bank_transfer`, `installments: 1`, `expires: true`, `expiration_date_to`; no other field (no `notification_url`) | `mercadopago_gateway.py:77-98` | integration-style (MockTransport) | `unit/payments/test_mercadopago_gateway.py::test_it_posts_the_exact_preference_body`, `test_it_sends_no_notification_url_and_no_other_field` | CONFIRMED |
| `unit_price` is cents / 100 as a JSON number (1, 5, 10, 199, 1005, 99900, 250000 cents; raw `1234.56`) | `mercadopago_gateway.py:83` | integration-style | `test_the_unit_price_is_the_cents_in_pesos[*]`, `test_the_unit_price_is_a_json_number_*`, `test_the_items_keep_their_amounts_summing_*` | CONFIRMED |
| `expiration_date_to` is ISO 8601 with the offset of the datetime (+00:00, -06:00) | `mercadopago_gateway.py:97` | integration-style | `test_it_posts_the_exact_preference_body`, `test_the_expiration_keeps_the_offset_of_the_datetime` | CONFIRMED |
| Headers and URL: POST `{api_url}/checkout/preferences`, `Authorization: Bearer`, JSON content type, trailing slash stripped, 10 s timeout; the token only in the Authorization header (not in body or URL) | `mercadopago_gateway.py:43-52` | integration-style | `test_it_posts_to_the_preferences_endpoint_*`, `test_a_trailing_slash_*`, `test_the_access_token_travels_only_in_the_authorization_header`, `test_the_request_has_a_ten_second_timeout` | CONFIRMED |
| Sandbox answers `sandbox_init_point`, production `init_point`; the configured API URL is used | `mercadopago_gateway.py:68` | integration-style | `test_in_sandbox_*`, `test_in_production_*`, `test_the_api_url_is_the_configured_one` | CONFIRMED |
| 21 failure modes (connect error, timeouts, 400/401/403/429/500/502, redirect, 204, non-JSON, list/null body, missing or non-string or empty id and URLs, missing chosen init point) give `PaymentGatewayUnavailable` with one `payments.gateway_error` log and the access token never in any log | `mercadopago_gateway.py:46-60` | integration-style | `test_every_failure_mode_answers_gateway_unavailable_without_leaking_the_token[*]`, `test_a_non_2xx_answer_logs_its_status_*`, `test_a_transport_error_logs_*`, `test_a_body_missing_the_fields_*` | CONFIRMED |
| No access token (None or empty): nothing sent, `payments.gateway_not_configured` logged once per call | `mercadopago_gateway.py:40-42` | integration-style | `test_without_an_access_token_it_sends_nothing_and_logs_once[*]`, `test_it_logs_once_per_call_when_not_configured` | CONFIRMED |
| Every gateway failure and the unconfigured gateway store nothing through the real adapter in `StartCheckout`; success stores the preference | `start_checkout.py` + adapter | integration-style | `test_a_failed_gateway_makes_the_use_case_store_nothing[*]`, `test_an_unconfigured_gateway_*`, `test_a_successful_gateway_*` | CONFIRMED |
| Route: 200 with `redirect_url`; reuse; 404/409 (per status, past deadline, `details.status`)/503/422 codes; public (no session); 503 body is `{code, message}` | `router.py` | http | `unit/payments/test_payments_http.py` | CONFIRMED |
| The token only in the body: query string and header are not read (422), GET is 405, the response never echoes it; OpenAPI has no parameters, the body schema with `maxLength` 200 and the 200/404/409/422/503 responses | `router.py`, `contracts.py` | http | `test_the_token_in_the_query_string_is_not_read`, `test_the_token_in_a_header_is_not_read`, `test_the_route_does_not_answer_get`, `test_openapi_declares_*` | CONFIRMED |
| Container resolves `StartCheckout`; payments registered after orders; gateway and back URL come from the settings | `module.py`, `container.py:34` | http (wiring) | `unit/payments/test_payments_wiring.py` | CONFIRMED |
| Settings validators (token required and sandbox off in production) | `config.py:107-115` | unit | `unit/test_settings.py` (written by the implementer, step 5) | CONFIRMED (not rewritten) |
| `SqlCheckoutRepository`: read back whole, `ON CONFLICT` upsert keeps one row, per-order rows, instants keep their moment, no FK, rollback stores nothing, 64/500 limits | `sql_checkout_repository.py` | integration | `integration/payments/test_sql_checkouts.py` (13 tests) | CONFIRMED |
| `payable_by_access_digest` over real orders: unknown digest, figures and lines in position order at the charged price, pickup has no delivery cost, status follows the order, per-digest; the facade digests the token | `sql_customer_orders.py:117-150` | integration | `integration/orders/test_sql_order_payments.py` (7 tests) | CONFIRMED |
| Migration 0023 on a scratch database: nothing at 0022; schema, table, columns and `pk_checkouts` at 0023; no FK; null, duplicate and length refusals; orders untouched; downgrade drops table and schema; the round trip rebuilds the same schema and the table works again | `0023_payments_checkouts.py` | integration | `integration/payments/test_migration_0023_payments_checkouts.py` (18 cases) | CONFIRMED |
| Sandbox checkout opens at Mercado Pago with the real credentials | plan acceptance | (none) | Not exercisable locally: the verifier's QA with a fake server, or the developer's account | NOT CONFIRMED (stays for verify, no test written) |

Notes: PostgreSQL 18 lists `NOT NULL` as `contype 'n'` rows in `pg_constraint`; the constraint
tests exclude them. No product code was touched.

## Review findings

Reviewed 2026-10-23, diff `main...HEAD` (4da31b0), tree clean.

**Checklist: 14 of 15 applicable items passed. 1 failed (stale doc). 1 n/a (PR body).**

- [x] `plans-scope`: "Every change is inside the plan" (42 declared, 55 changed, all covered).
  The hot files are append-only: `.importlinter` has one container line and two ignore lines,
  `container.py` has an import and `payments` appended to `MODULES`, and `modules.json` flips
  `planned` to `active`.
- [x] `uv run just check` green: lint, types, every import-linter contract KEPT (12 ignored
  imports), 3748 passed and 3 skipped, harness 685 passed.
- [x] `uv run just test-integration` green: 1074 passed, 5 skipped.
- [x] Business rules: `Checkout.reusable` is in the domain. The "payable" rule
  (`pending_payment` and before the deadline) is in `StartCheckout`
  (`start_checkout.py:72`), as step 3 prescribes. It is a cross-module status string, so it
  is accepted here.
- [x] CQRS-lite: the read and the upsert run inside `transactions.run`, the result is a
  `Result`, and the repository has no screen-specific methods.
- [x] Contracts come from `payments/contracts.py`, and `openapi.json` was regenerated (no
  drift in `check`).
- [x] Errors: `PAYMENTS_ORDER_NOT_FOUND`, `PAYMENTS_ORDER_NOT_PAYABLE` and
  `PAYMENTS_GATEWAY_UNAVAILABLE`. No internals leak: the 503 body is `{code, message}`.
- [x] Money: integer cents throughout. The only float is `unit_price` on the wire, which is
  where the plan puts it (see the bug hunt).
- [x] Migration 0023: `CREATE SCHEMA`, no FK, reversible (the round trip is tested).
- [x] Route in `public_router`; no bare `APIRouter`.
- [x] Wiring resolves (`test_payments_wiring.py`). `payments` comes after `orders`, and the
  adapters are built only in `module.py`.
- [x] No secrets: the tests use `APP_USR-test` placeholders, and `.env.example` has empty values.
- [x] Deviations are honest. Spot check: `PayableOrder`/`PayableLine` are defined in
  `orders/application/ports.py:125-148` and re-exported from `queries/order_payments.py`
  and `orders/__init__.py`.
- [ ] **Docs: `apps/api/README.md:62` still lists the error categories as
  "401/404/409/422/429" and omits the new 503 `ServiceUnavailableError`.** That file is not
  in the plan's file list, so fixing it needs a deviation line.
- n/a PR body: the reviewer saw no PR (`gh` was not available in this run).

**Focus areas checked and found correct:**
- **Boundaries.** Nothing under `modules/orders` imports `modules.payments`. Payments reaches
  orders only from `infrastructure/order_payments.py` and `module.py`, both through the
  public `fragancia_api.modules.orders`, with exactly the two declared ignore lines.
  `PayableOrders` is payments' own port with its own dataclasses, and the adapter copies
  every field.
- **Cents to pesos.** `round(cents / 100, 2)` (`mercadopago_gateway.py:79`) is exact for
  any integer cents below 2**53. `cents / 100` is the correctly rounded double, and `json`
  serializes it with the shortest repr, which is exactly the 2-decimal string (for example
  `1005` gives `10.05`). The `round` is a no-op. Mercado Pago's total is
  quantity × unit_price on decimals of 2 places, so there is no drift. The items' sum is
  checked against `total_cents`, which `Order` builds as subtotal plus delivery
  (`order.py:316-335`).
- **Secrets.** The token is a `SecretStr`, unwrapped once in `module.py:22`. It travels
  only in the `Authorization` header. The logs carry only the status and the exception class
  name (`mercadopago_gateway.py:47,50,54`). The gateway has no repr of its fields. httpx
  0.28.1 does not log headers.
- **Production validators** (`config.py:107-115`). In production a missing or empty token
  fails, and so does `sandbox=true`; both are tested.
- **503 category.** `ServiceUnavailableError` is independent of the other categories, so
  its position in `_STATUS_BY_CATEGORY` cannot shadow anything. It is exported from the
  kernel, which stays stdlib-only (KEPT).
- **Preference body** against the fields cited in the Context: `items[]` (`id`, `title`,
  `quantity`, `unit_price`, `currency_id`), `payer{name,email}`, `external_reference`,
  `back_urls` with all three keys, `auto_return`, `payment_methods.excluded_payment_types[{id}]`
  and `installments`, `expires` and `expiration_date_to`. No `notification_url`. This
  matches the plan. The one exception is the timestamp format (M2).

### Findings

**Medium**

- **M1 — The module's `httpx.AsyncClient` is never closed**
  (`modules/payments/module.py:24`; `container.py:49-51`).
  - What fails: `register` creates the client, and nothing ever closes it.
    `Container.close()` closes Valkey and the database only, and `AppModule`/`Platform` have
    no shutdown hook.
  - Where it runs: every process that builds the container creates one. That covers the API
    (in `create_app`, synchronously, before the loop), the worker
    (`main/worker.py:33`, which never uses it) and the CLI (`main/cli.py:31`). Every
    `build_container` in the tests creates another.
  - Failure scenario: at API shutdown, the keep-alive connections to `api.mercadopago.com`
    are dropped without a clean close. Long test runs accumulate unclosed clients.
  - Safety across processes: correct today. Each process gets its own client, and httpx
    binds connections lazily on first use, inside that process's single loop. It is fragile,
    though. Any entrypoint that runs `asyncio.run` twice over one container (the CLI
    pattern) would reuse pooled connections across event loops.
  - The plan's "owned by the module" has no owner lifecycle. It also uses httpx's default
    `trust_env=True`, so `HTTP(S)_PROXY`/`SSL_CERT_FILE` from the environment apply. The
    catalog downloader sets `trust_env=False` (`http_image_downloader.py:169-173`).
  - In-scope fix: build the client per call with `async with httpx.AsyncClient(...)` inside
    the gateway, as the catalog downloader does. Tests can still inject a transport factory.
    The alternative, a container close hook for module resources, touches
    `shared/module.py` and `container.py`, which is outside this plan's file list and needs a
    user decision.

- **M2 (uncertain) — The format of `expiration_date_to`** (`mercadopago_gateway.py:97`).
  - What fails: `request.expires_at.isoformat()` emits microseconds, because
    `payment_due_at` comes from `timestamptz`, for example `2026-10-22T18:30:12.345678+00:00`.
    Mercado Pago's API reference documents the format `yyyy-MM-dd'T'HH:mm:ss.SSSZ` (example
    `2016-02-28T12:00:00.000-04:00`), which has milliseconds.
  - Uncertain: the reviewer could not confirm whether Mercado Pago rejects 6 fractional
    digits or tolerates them.
  - Failure scenario: if it rejects them, every real preference request answers 400. Users
    get 503 `PAYMENTS_GATEWAY_UNAVAILABLE` and no one can pay.
  - The tests and the verifier's local fake check our own format, so neither would catch
    this. Only the sandbox run (still NOT VERIFIED) would.
  - Suggested fix: `isoformat(timespec="milliseconds")`.

**Low**

- **L1 — The 2-minute margin replaces a link with one that expires at the same instant**
  (`domain/checkout.py:20`, `start_checkout.py:79,93,105`).
  - In the last 2 minutes before `payment_due_at`, `reusable` is false. The command then
    creates a new preference with the same `expires_at = payment_due_at`.
  - So every request in that window calls Mercado Pago and overwrites the row, and the new
    link is just as close to expiring as the one it replaced. The stated reason ("the guest
    could land on an expired page") is not served.
  - Suggestion: answer `PAYMENTS_ORDER_NOT_PAYABLE` when `payment_due_at - REUSE_MARGIN <= now`,
    or reuse until expiry. Either is a small behavior decision for the user.
- **L2 — The customer-facing 404 message is in English** (`domain/errors.py:9`, "Order not
  found"). The guest's page shows it. The orders equivalents are Spanish ("No encontramos ese
  pedido", `orders/domain/errors.py:110`), and the backend conventions require Spanish for
  customer-facing messages.
- **L3 — The stale doc** listed in the checklist (`apps/api/README.md:62`).
- **L4 — The total check is a bare `assert`** (`start_checkout.py:38`). It is stripped
  under `python -O`, and when it fires it is a 500. The plan prescribes an assert and the
  totals are consistent by construction (`order.py:335`), so this is only a note.
- **L5 — `MERCADOPAGO_API_URL` is not validated in production** (`config.py:68`). A leftover
  fake URL, or a plain `http://` one, would send the production access token there. A
  production validator requiring `https://api.mercadopago.com` (or at least `https://`)
  would stop it.

**Info (no code change in this plan)**

- **Concurrent `StartCheckout` calls** (two tabs, double click).
  - Both read "no checkout" and both call the gateway. Two preferences are created and the
    last upsert wins, so the first caller gets a valid link that is not stored. Both carry
    `external_reference = order id` and expire at `payment_due_at`.
  - Harmless for 001: no row is corrupted (PK plus `ON CONFLICT`), and the extra preference
    expires.
  - **Input for plan 002:** match payments by `external_reference`, never by the stored
    `preference_id`. Treat a second approved payment for an already paid order as a refund
    case. A single Checkout Pro preference can also be paid more than once, so double payment
    is possible even without the race.
  - Suggest the main session file this under `plans/findings/` or in plan 002's Context. The
    reviewer writes only to this plan.
- **Order cancelled between the read and the gateway call.** A preference can be created and
  stored for an order that was just cancelled. Paying it is the late-payment refund of plan
  004 (decision 3). Accepted by design.
- **For verify against the sandbox (uncertain, from Mercado Pago integration experience):**
  - Mercado Pago may reject `auto_return` when `back_urls.success` is a `localhost` URL.
  - With test credentials, a real `payer.email` can make the checkout refuse ("una de las
    partes es de prueba").
  - Both would show up as 503 locally and are not product bugs of this plan.

**Status stays `review`:** M1 needs a code change, and M2 should be resolved before the
sandbox run. L1, L2, L3 and L5 are small and go in the same repair pass if the user agrees.

## Repair log

Step 6, 2026-10-23, after the first review. The first review, test and coverage evidence above
is superseded where it names the 2-minute margin, the injected client, the microsecond
timestamp or the `assert`; testing, review and verify repeat on the repaired code.

- **M1.** `MercadoPagoGateway` opens an `httpx.AsyncClient(timeout=10, trust_env=False)` per
  call inside `async with`; the constructor takes `transport` (default None) instead of
  `client`. `module.py` creates no client.
- **M2.** `expiration_date_to` is `isoformat(timespec="milliseconds")`.
- **L1.** `REUSE_MARGIN` removed. `Checkout.reusable(payment_due_at, now)` is
  `expires_at == payment_due_at and expires_at > now`; `StartCheckout` passes the order's
  deadline. (The signature gained the deadline argument.)
- **L2.** `PaymentOrderNotFound.message` is "No encontramos ese pedido".
- **L3.** `apps/api/README.md` lists 503 (upstream unavailable).
- **L4.** The items-sum check raises `ValueError`; no `assert` remains in `start_checkout.py`.
- **L5.** Production requires `mercadopago_api_url` to start with `https://`.
- **Info.** Already recorded in the initiative README's "Dependency notes" (no change).
- **Tests updated:** `test_mercadopago_gateway.py` (transport injection, millisecond
  timestamps), `test_checkout_domain.py` and `test_start_checkout.py` (deadline-based reuse
  replaces the margin tests; `ValueError` replaces `AssertionError`), `test_settings.py`.
- **Tests added:** milliseconds vs microseconds; a client opened per call with
  `trust_env=False` and the 10 s timeout; no `REUSE_MARGIN`; reuse in the last second and
  replacement for another deadline; reuse until the deadline then not payable; the Spanish 404
  message; no `assert` in `start_checkout.py`; production `https://` validation (4 invalid
  URLs, the default accepted, a local fake allowed outside production).
- **Results:** `uv run just check` green (harness 685 passed); `uv run just test-integration`
  1074 passed, 5 skipped; `plans-scope` run below.
- `openapi.json` unchanged (no contract change); no file outside the step 6 list was touched.
- **Re-review (2026-10-23, main session).**
  - **M1:** one `httpx.AsyncClient` per call inside `async with`, with `trust_env=False`;
    `module.py` holds no client.
  - **M2:** `isoformat(timespec="milliseconds")`.
  - **L1:** `reusable(payment_due_at, now)` compares two `timestamptz` values read from the
    database, at the same microsecond precision, so equality is exact.
  - **L5:** the production check is in `config.py`.
  - **L2–L4:** fixed as described above.
  - No new defect found. The change is small and pinned by tests, so there is no separate tester
    round. The status moves to `verify`.

## Verification

2026-10-23, main session, live against the development database.

**Setup:**
- **The fake Mercado Pago.** `scratchpad/qa_pay/fake_mercadopago.py`, on 127.0.0.1:8199. It
  records every `POST /checkout/preferences` and answers `id`, `init_point` and
  `sandbox_init_point`. It can be switched to answer 500.
- **The API.** It ran on :8101 through `scratchpad/qa_pay/api_with_fake_mp.py`, which builds the
  usual `Settings` and overrides only `mercadopago_api_url`, a fake `TEST-` token and
  `mercadopago_sandbox=true` in code. The guard hook refuses inline environment assignments.
- **The QA script.** `scratchpad/qa_pay/qa_checkout_redirect.py`.
- **Delivery.** Home delivery for 18000, free from 500000, was set for the run and restored
  afterwards.
- **Orders left behind.** `LFI-1023` to `LFI-1027` stay in the database: one paid link, one
  cancelled order, one past its deadline, and two for the failure test.

- [x] **Link.** `POST /api/v1/payments/checkout` with `LFI-1023`'s token answered 200 with
  `https://sandbox.mercadopago.com.mx/…?pref_id=qa-…` (the sandbox URL, as configured).
  - The fake received one preference, with `Authorization: Bearer TEST-…`.
  - Items:
    - the 50 ml at 1899.5;
    - the 200 ml at 3000.0;
    - "Envío" (`id` shipping) at 180.0, all quantity 1 in MXN.

    They sum to 5079.5, which is the order total of 507950 cents.
  - `external_reference` equals the order id.
  - `payment_methods` excludes ticket, atm and bank_transfer, with `installments: 1`.
  - `expires: true`, and `expiration_date_to: 2026-10-23T20:35:10.002+00:00` (milliseconds,
    equal to `payment_due_at`).
  - Every back URL is `http://localhost:4200/pedido/pago`, with `auto_return: approved` and
    the payer's name and email. There is no `notification_url`.
  - One `payments.checkouts` row holds the preference id, the URL and `expires_at` =
    `payment_due_at`.
- [x] **Reuse.** A second call answered the same URL, and the fake still had 1 preference.
- [x] **Refusals.**
  - An unknown token gave 404 `PAYMENTS_ORDER_NOT_FOUND`, "No encontramos ese pedido".
  - A cancelled order, and one past its deadline, gave 409 `PAYMENTS_ORDER_NOT_PAYABLE`.
  - With the fake answering 500, the call gave 503 `PAYMENTS_GATEWAY_UNAVAILABLE` and stored no
    row. After the fake recovered, the same order got a link (200).
  - The API log has `payments.gateway_error`, and never the access token.
- [ ] **Sandbox: NOT VERIFIED.** The developer's Mercado Pago test account does not exist yet
  (decision 1). The real API's acceptance of the body, including the millisecond
  `expiration_date_to` (review M2) and `localhost` back URLs, is unproven until then.

Also NOT VERIFIED live: "no access token configured gives 503". The development `.env` may hold
a token, and this run injected one. Unit tests cover it.
