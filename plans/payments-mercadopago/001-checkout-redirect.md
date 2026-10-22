---
status: review
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

## Verification
