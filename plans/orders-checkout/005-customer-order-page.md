---
status: implementing
module: orders
min_implementer: mid
depends_on: ["004"]
---

# 005 — Customer order page

## Context

A guest follows their order through the private link given at checkout (decision 13), or finds
it again with the order number and their email. From that page they can cancel it themselves
while it is unpaid. The plan applies decisions 13 and 31, and the new 39–41.

**What exists today:**
- **The private link.** Its token is `HmacOrderLinks.token(order_id)`, and only its SHA-256
  digest is stored in `orders.orders.access_token_digest`, which is unique
  (`apps/api/src/fragancia_api/modules/orders/infrastructure/order_links.py:7-20`;
  `modules/orders/infrastructure/tables.py:60-80`).
- **Cancelling.** `CancelUnpaidOrder.execute(number, reason, actor)` cancels a `pending_payment`
  order and releases its reservation. It goes through the shared `OrderMove._apply` (lock, move,
  inventory call, save, events) (`modules/orders/application/commands/lifecycle.py:37-82`,
  `:128-137`). `actor` is a free string in the history; the job already uses `"system"`.
- **The panel's read side.** `OrderQueries.detail(number)` returns the panel's view, with
  history and notes (`modules/orders/application/ports.py:88-98`;
  `modules/orders/infrastructure/sql_order_queries.py:88-…`).
- **Attempt throttling** exists only in identity:
  - `SqlLoginThrottle.hit` keeps a fixed window per key with one atomic upsert
    (`modules/identity/infrastructure/sql_login_throttle.py:12-36`);
  - its table is `identity.login_throttle` (`modules/identity/infrastructure/tables.py:38-45`);
  - the login counts the email key and the IP key in their own committed transaction, then
    refuses with a `RateLimitedError` (429) (`modules/identity/application/commands/log_in.py:68-86`;
    `modules/identity/domain/errors.py:50-52`);
  - the route passes `request.client.host` (`modules/identity/http/router.py:107-111`).

  Orders cannot import identity, so it gets its own copy.
- **Customer email normalization** is `CustomerEmail.create`, which strips and lowercases
  (`modules/orders/domain/customer.py`).

**Approach.**
- **Three public routes** under `/api/v1/orders`:
  - read the order by its token;
  - look it up by number and email, which answers the token;
  - cancel by token.
- **A customer view** separate from the panel's. It has no internal notes and no admin ids, and
  it does not show the cancel reason the shop typed. The page says only who cancelled:
  - the customer;
  - the system ("No pagado a tiempo");
  - or the shop.
- **The lookup** is throttled per IP and per order number. A wrong email and an unknown number
  answer the same 404, so the lookup does not reveal which numbers exist.
- **Rejected:**
  - Emailing the link instead of showing it (decision 39): that depends on plan 006 and is
    slower.
  - Reusing `AdminOrderDetail`: it carries notes and admin ids.

## Out of scope

- **The emails** (plan 006), including sending the link again by email.
- **Paying from the page:** Mercado Pago comes with the payments initiative. The page shows the
  payment deadline only.
- **Cancelling a paid order from the page** (decision 34: only owners).
- **Masking the address or phone** (decision 40 shows them in full).
- **Purging old throttle rows:** identity does not purge either. Revisit with a job if the table
  grows.

## Dependencies

- **004 (done):** the statuses, the history, `CancelUnpaidOrder` and `OrderMove`.
- **003 (done):** `HmacOrderLinks` and `access_token_digest`.

## Steps

1. **Domain: the customer's cancel and the lookup rules**
   - Files: `apps/api/src/fragancia_api/modules/orders/domain/order.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/domain/errors.py` (modify)
   - Do:
     - **Constants:**
       - `CUSTOMER_ACTOR = "customer"` and `CUSTOMER_CANCEL_REASON = "Cancelado por el cliente"`
         (decision 41);
       - `LOOKUP_WINDOW = timedelta(minutes=15)`, `LOOKUP_IP_MAX = 20` and
         `LOOKUP_NUMBER_MAX = 5`.
     - **`cancelled_by(actor) -> Literal["customer", "system", "shop"]`:** `customer` for
       `CUSTOMER_ACTOR`, `system` for `SYSTEM_ACTOR`, otherwise `shop`.
     - **New errors:**
       - `OrderLookupNotFound(NotFoundError)`, with code `ORDERS_LOOKUP_NOT_FOUND` and message
         "No encontramos un pedido con ese número y correo". It is the same for an unknown
         number and for a wrong email.
       - `OrderLookupRateLimited(RateLimitedError)`, with code `ORDERS_LOOKUP_RATE_LIMITED` and
         message "Demasiados intentos; espera unos minutos".
   - Observable result: unit tests for `cancelled_by` and the constants.

2. **Application: queries, lookup and cancel**
   - Files: `apps/api/src/fragancia_api/modules/orders/application/ports.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/application/queries/customer_order.py` (create),
     `apps/api/src/fragancia_api/modules/orders/application/commands/customer_order.py` (create),
     `apps/api/src/fragancia_api/modules/orders/contracts.py` (modify)
   - Do:
     - **Ports:**
       - `CustomerOrderQueries.by_access_digest(digest) -> CustomerOrderView | None`, through
         `Database.reader()`.
       - `CustomerOrderQueries.match(number, email) -> tuple[UUID, int] | None`. It returns the
         order id and number when that number exists **and** its email equals `email`, which is
         already normalized.
       - `LookupThrottle.hit(key, now, window) -> int`. It joins the active transaction, like
         identity's.
     - **Contracts:**
       - `CustomerOrderView`:
         - the number, `status`, `created_at` and `payment_due_at`;
         - `lines`: label, ml, quantity, `unit_price_cents`, `line_total_cents`, availability
           and lead times;
         - `subtotal_cents`, `delivery_cents`, `total_cents` and `lead_time_max_days`;
         - the delivery: `delivery_method`, `address` (all fields), `pickup_address` and
           `pickup_hours`;
         - `invoice` (all fields, or null);
         - `shipping_carrier` and `tracking_number`;
         - `refund_status`;
         - `cancelled_by` (customer, system or shop, or null);
         - `timeline`, a list of `{status, at}`: one entry per status change oldest first,
           starting with `pending_payment` at `created_at`. The refund row
           (cancelled → cancelled) is left out, because `refund_status` says it.
         - `can_cancel`, true while `pending_payment`.

         The contract has no notes, no actor ids, no cancel reason, no reservation id and no
         email or token digests.
       - `OrderLookupRequest(number: str, email: str)`.
       - `OrderLookupResponse(access_token: str)`.
     - **`GetCustomerOrder(token)`** digests the token with `OrderLinks.digest` and returns the
       view, or `ORDERS_ORDER_NOT_FOUND`.
     - **`LookUpOrder(number, email, ip)`** (decision 39):
       1. Parse the number with `parse_order_number`, which accepts `LFI-1001` or `1001`, and
          normalize the email with `CustomerEmail.create`.
       2. Count `lookup-ip:{ip or 'unknown'}`, plus `lookup-number:{n}` when the number parses,
          in one committed transaction, as identity's `log_in` counts.
       3. Over `LOOKUP_IP_MAX` or `LOOKUP_NUMBER_MAX`, refuse with `ORDERS_LOOKUP_RATE_LIMITED`.
       4. A number that does not parse, an invalid email or no match all give
          `ORDERS_LOOKUP_NOT_FOUND`.
       5. On a match, return `access_token = links.token(order_id)`.
     - **`CancelOrderByCustomer(token)`** (decision 41): digest the token and find the order
       number with the query port. Unknown gives `ORDERS_ORDER_NOT_FOUND`. Then run
       `CancelUnpaidOrder.execute(number, CUSTOMER_CANCEL_REASON, CUSTOMER_ACTOR)`. When it is
       not `pending_payment`, that refuses with `ORDERS_TRANSITION_INVALID` (409).
   - Observable result: application tests with fakes cover the view built from an order with
     history, the lookup (match, wrong email, unknown number, bad input, both limits), and the
     customer cancel (unpaid, paid refused, unknown token).

3. **Infrastructure: the SQL read side, the throttle and the migration**
   - Files: `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_customer_orders.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_lookup_throttle.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/tables.py` (modify),
     `apps/api/migrations/versions/0022_orders_lookup_throttle.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/in_memory.py` (modify)
   - Do:
     - **`orders.lookup_throttle`:** `key` (varchar 64, PK), `attempts` (int) and
       `window_started_at` (timestamptz). Migration `0022`, reversible.
     - **`SqlLookupThrottle`:** a copy of identity's `hit`, which is one atomic upsert. It
       carries a comment saying it is a deliberate copy, because modules may not import each
       other.
     - **`SqlCustomerOrders`:**
       - `by_access_digest` selects the order, its lines by position and its history oldest
         first. It builds the timeline and `cancelled_by` from the history row whose `to_status`
         is `cancelled` (its actor).
       - `match` compares `number` and `customer_email`, which is stored already normalized.
     - **In-memory:** fakes for the new ports.
   - Observable result: integration tests read the view by digest, match number and email (and
     refuse a wrong email), count throttle hits atomically, and run the migration round trip on
     a scratch database.

4. **HTTP, wiring and docs**
   - Files: `apps/api/src/fragancia_api/modules/orders/http/router.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/module.py` (modify),
     `apps/api/openapi.json` (modify),
     `docs/architecture.md` (modify),
     `apps/api/tests/unit/orders/` (modify), `apps/api/tests/integration/orders/` (modify)
   - Do:
     - **`customer_orders = public_router(prefix="/orders", tags=["orders · customer"])`:**

       | Route | Answers |
       | ----- | ------- |
       | `GET "/{access_token}"` | `CustomerOrderView`; 404 `ORDERS_ORDER_NOT_FOUND` |
       | `POST "/lookup"` | `OrderLookupResponse`; 404 `ORDERS_LOOKUP_NOT_FOUND`, 429 `ORDERS_LOOKUP_RATE_LIMITED`. It passes `request.client.host`, like identity's login |
       | `POST "/{access_token}/cancel"` | 204; 404, or 409 `ORDERS_TRANSITION_INVALID` |

       - Register `/lookup` before `/{access_token}` so that the path matches first.
       - The docstrings name decisions 13, 39, 40 and 41, and say the token is a secret.
     - **Wiring:** in `module.py`, reusing the existing `HmacOrderLinks` instance and the
       `CancelUnpaidOrder` service.
     - **Docs:** regenerate `openapi.json`, and extend the orders row of `docs/architecture.md`.
     - The tester writes the layered tests.
   - Observable result: `uv run just check` and `uv run just test-integration` pass.

5. **Review repair (added 2026-10-20 after the first review; decisions 42 and 43)**
   - Files: `apps/api/src/fragancia_api/modules/orders/http/router.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/contracts.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_customer_orders.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/in_memory.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/application/queries/customer_order.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/application/commands/customer_order.py` (modify),
     `apps/api/openapi.json` (modify),
     `docs/architecture.md` (modify),
     `apps/api/tests/unit/orders/` (modify), `apps/api/tests/integration/orders/` (modify)
   - Do:
     - **M1, the token stays out of URLs (decision 42).**
       - Replace `GET /orders/{access_token}` with `POST /orders/view`, and
         `POST /orders/{access_token}/cancel` with `POST /orders/cancel`. Both take the body
         `OrderAccessRequest(access_token: str, max_length 200)`.
       - `POST /orders/lookup` is unchanged. No route keeps the token in its path, so the access
         log, a proxy log and the unhandled-error log (`shared/http/middleware.py:146`, which logs
         the path) never see it.
       - The docstrings say the web link carries the token in the URL **fragment**
         (`/pedido#token=…`). The page reads the fragment and POSTs it, as ADR 0010 does for
         identity's links.
       - Error codes and statuses stay as they are.
     - **L2, name and phone on the page (decision 43).** `CustomerOrderView` gains
       `customer_name` and `customer_phone`, filled by the SQL and in-memory readers. The route
       docstring says what the page shows, and `openapi.json` is regenerated.
     - **L3** goes to `plans/findings/platform-client-ip-behind-proxy.md` (deployment). Nothing
       changes in this plan.
     - **Info 4** is accepted: the per-number lookup limit can be used to block one number's
       lookup for 15 minutes, while the private link still works. It is the same trade-off as
       identity's per-email login limit.
     - **Tests.**
       - Update the HTTP, flow and OpenAPI tests to the new routes.
       - Assert that no route path under `/orders` has a token parameter.
       - Add the two fields to the exact key sets.
   - Observable result:
     - `uv run just check`, `uv run just test-integration` and `plans-scope` are clean.
     - The old path routes answer 404 or 405.

## Acceptance criteria

Run with the API and the worker on the development database. Orders are placed through
`POST /checkout`, and moved with the panel routes of plan 004.

- [ ] **By link.** `GET /api/v1/orders/{access_token}` with the token from checkout answers the
  view:
  - the status, lines and totals;
  - the address, and the invoice when there is one;
  - a timeline that starts at `pending_payment`, and `can_cancel: true`.

  It has no notes, actor ids or reservation id. A wrong token answers 404.
- [ ] **After moves.** Paid and shipped (Estafeta / 123456) from the panel, the page shows the
  timeline pending_payment → paid → shipped with times, the carrier and tracking number, and
  `can_cancel: false`.
- [ ] **Lookup.**
  - `POST /orders/lookup` with `LFI-n` and the email (in any case, with spaces) answers a token
    that opens the same view.
  - `1001`-style numbers work too.
  - A wrong email and an unknown number both answer 404 `ORDERS_LOOKUP_NOT_FOUND`, with the same
    body.
  - The 6th attempt for one number within 15 minutes answers 429 `ORDERS_LOOKUP_RATE_LIMITED`.
- [ ] **Customer cancel.**
  - `POST /orders/{token}/cancel` on an unpaid order answers 204. The page shows `cancelled`,
    `cancelled_by: "customer"` and `can_cancel: false`.
  - The panel history shows actor `customer` with reason "Cancelado por el cliente", and the
    reservation is released.
  - On a paid order it answers 409 `ORDERS_TRANSITION_INVALID`.
- [ ] **Who cancelled.**
  - An order cancelled by the overdue job shows `cancelled_by: "system"`.
  - One cancelled from the panel shows `"shop"`, without the reason the shop typed.
  - A refunded paid cancellation shows `refund_status: "done"`, and no extra timeline entry.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `cancelled_by` for each actor kind; the constants |
| application | yes     | View building (timeline from created_at, refund row excluded, cancelled_by, can_cancel); lookup (match, normalization, wrong email and unknown number answer the same, unparsable number, IP and number limits, counts committed before the refusal); customer cancel (unpaid, paid 409, unknown token) |
| http        | yes     | The three routes, `/lookup` not captured by `/{access_token}`, the view has no internal fields, every status and error code, OpenAPI |
| integration | yes     | `SqlCustomerOrders` (view, timeline, cancelled_by, match), `SqlLookupThrottle` atomic counting and window reset, customer cancel over real inventory (reservation released), migration 0022 round trip on a scratch database |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

None. (Cosmetic note: `GetCustomerOrder` and `CancelOrderByCustomer` are wired in `module.py` with the `SqlCustomerOrders` instance; the in-memory customer fake builds the view from `InMemoryOrders` for the tester.)

## Test coverage

Baseline (before tests): `just check` 3413 passed / 3 skipped, harness 685 passed;
`just test-integration` 982 passed / 5 skipped. Closing: `just check` 3507 passed / 3 skipped,
harness 685 passed; `just test-integration` 1028 passed / 5 skipped. Delta: 12 domain + 48
application + 34 http (94 unit) and 46 integration tests (23 SQL, 13 flow, 10 migration). No GAP
and no NOT CONFIRMED were found. Test-infra change: `tests/integration/orders/conftest.py`
truncates `orders.lookup_throttle` in its autouse reset.

| Behavior | Source | Layer | Test | State |
| -------- | ------ | ----- | ---- | ----- |
| `cancelled_by`: customer / system / anything else is shop | `domain/order.py:91-97` | domain | `test_customer_order_domain.py::test_the_customer_actor_cancels_as_the_customer`, `::test_the_system_actor_cancels_as_the_system`, `::test_any_other_actor_is_the_shop` | CONFIRMED |
| Constants (actor, reason, window 15 min, 20 per IP, 5 per number) | `domain/order.py:44-51` | domain | `test_customer_order_domain.py::test_the_actor_and_reason_constants_are_the_planned_ones`, `::test_the_lookup_limits_are_the_planned_ones` | CONFIRMED |
| Lookup errors: code, message, 404 / 429 kind | `domain/errors.py:129-140` | domain | `test_customer_order_domain.py::test_the_lookup_not_found_is_a_404_kind_with_its_code_and_message`, `::test_the_lookup_rate_limited_is_a_429_kind_with_its_code_and_message` | CONFIRMED |
| View: data, one-step timeline from `created_at`, `can_cancel` | `queries/customer_order.py`, `in_memory.py` (`InMemoryCustomerOrders`) | application | `test_customer_order.py::test_a_new_order_shows_its_data_a_one_step_timeline_and_can_be_cancelled` | CONFIRMED |
| View exact key set, no notes / actor ids / cancel reason / reservation id / digests / email | `contracts.py:309-334` | application, http, integration | `test_customer_order.py::test_the_view_has_exactly_the_customer_keys_and_nothing_internal`, `::test_no_internal_value_appears_anywhere_in_the_serialized_view`; `test_customer_order_http.py::test_the_view_has_exactly_the_customer_keys_and_nothing_internal`, `::test_the_view_body_never_contains_an_internal_value`, `::test_the_openapi_view_schema_has_exactly_the_customer_keys`; `test_sql_customer_orders.py::test_the_view_has_exactly_the_customer_keys_and_no_internal_value`; `test_customer_order_flow.py::test_the_token_of_a_real_checkout_opens_the_view_with_only_customer_keys` | CONFIRMED |
| Timeline per move with times, refund row excluded, carrier / tracking | `sql_customer_orders.py:40-42, 100-108` | application, integration | `test_customer_order.py::test_the_timeline_follows_the_moves_with_their_times_and_the_shipping_data`, `::test_the_refund_row_is_not_a_timeline_step_and_refund_status_says_it`; `test_sql_customer_orders.py::test_the_timeline_follows_the_history_oldest_first_and_carries_the_shipping_data`, `::test_the_refund_row_is_not_a_step_and_refund_status_says_it`; `test_customer_order_flow.py::test_a_refunded_paid_cancellation_shows_done_and_no_extra_step`, `::test_the_timeline_after_paying_and_shipping_has_times_in_order` | CONFIRMED |
| `cancelled_by` customer / system / shop, reason never shown | `sql_customer_orders.py:41` | application, integration | `test_customer_order.py::test_a_shop_cancellation_says_shop_and_never_its_reason`, `::test_the_overdue_job_cancellation_says_system`; `test_sql_customer_orders.py::test_cancelled_by_comes_from_the_actor_of_the_cancelling_move`; `test_customer_order_flow.py::test_the_overdue_job_shows_system`, `::test_a_panel_cancellation_shows_shop_without_its_reason` | CONFIRMED |
| Lookup match with number (prefix / case / spaces) and email normalization | `commands/customer_order.py:50-84` | application, integration | `test_customer_order.py::test_a_matching_number_and_email_answer_the_token_of_the_order`, `::test_the_number_is_accepted_with_or_without_prefix_in_any_case`, `::test_the_email_is_normalized_before_matching`; `test_customer_order_flow.py::test_the_lookup_answers_the_token_that_opens_the_same_order` | CONFIRMED |
| Wrong email and unknown number give the same error / byte-identical body | `commands/customer_order.py:78-84`, `http/router.py:395-414` | application, http, integration | `test_customer_order.py::test_a_wrong_email_and_an_unknown_number_answer_the_same_error`, `::test_unparsable_input_answers_the_same_not_found`; `test_customer_order_http.py::test_a_wrong_email_and_an_unknown_number_answer_byte_identical_responses`, `::test_unparsable_input_answers_the_same_bytes_as_a_wrong_email`; `test_customer_order_flow.py::test_a_wrong_email_and_an_unknown_number_answer_equal_errors` | CONFIRMED |
| IP limit (21st refused) and number limit (6th refused), other keys and the window reset | `commands/customer_order.py:68-73` | application, http | `test_customer_order.py::test_the_number_limit_allows_five_and_refuses_the_sixth`, `::test_the_ip_limit_allows_twenty_and_refuses_the_twenty_first`, `::test_the_window_resets_after_fifteen_minutes`; `test_customer_order_http.py::test_the_sixth_attempt_for_one_number_answers_429_with_its_code`, `::test_the_ip_limit_answers_429_on_the_twenty_first_attempt` | CONFIRMED |
| Throttle counts commit BEFORE the refusal | `commands/customer_order.py:56-71` | application, integration | `test_customer_order.py::test_the_counts_commit_before_the_refusal_and_nothing_is_looked_up`, `::test_the_counts_are_made_inside_one_transaction_before_the_match`; `test_customer_order_flow.py::test_the_attempts_are_committed_when_the_sixth_is_refused` (rows in `orders.lookup_throttle` after the 429) | CONFIRMED |
| Throttle keys: parsed number, `unknown` IP, client IP passed by the route | `commands/customer_order.py:50-56`, `router.py:410` | application, http | `test_customer_order.py::test_the_number_key_is_the_parsed_number_whatever_its_spelling`, `::test_a_missing_ip_counts_under_unknown`; `test_customer_order_http.py::test_the_throttle_counts_the_client_ip_of_the_request` | CONFIRMED |
| `/lookup` not captured by `/{access_token}` | `router.py:395-417` | http | `test_customer_order_http.py::test_lookup_is_not_captured_by_the_token_route`, `::test_lookup_validates_its_own_body_not_a_token_path`, `::test_lookup_answers_the_token_that_opens_the_same_order`, `::test_get_on_lookup_reaches_the_token_route_with_the_token_lookup`, `::test_lookup_is_registered_before_the_token_route` (OpenAPI registration order; the behavioral tests are the proof) | CONFIRMED |
| Customer cancel: unpaid ok (reason, actor, release), paid 409, unknown token 404 | `commands/customer_order.py:88-106` | application, http, integration | `test_customer_order.py::test_the_customer_cancels_an_unpaid_order_and_the_reservation_is_released`, `::test_a_paid_order_is_refused_with_transition_invalid_and_stays_untouched`, `::test_an_unknown_token_is_order_not_found_and_changes_nothing`; `test_customer_order_http.py::test_cancel_answers_204_with_no_body_and_cancels_the_order`, `::test_cancel_of_a_paid_order_answers_409_transition_invalid`, `::test_cancel_with_an_unknown_token_answers_404`; `test_customer_order_flow.py::test_the_customer_cancel_releases_the_real_reservation_and_records_the_actor`, `::test_a_paid_order_is_refused_and_its_reservation_stays_committed` | CONFIRMED |
| Every status and error code of the three routes, validation, public access, OpenAPI | `router.py:392-436` | http | `test_customer_order_http.py` (GET 200 / 404 / 422, POST lookup 200 / 404 / 422 / 429, cancel 204 / 404 / 409 / 422, `test_the_openapi_declares_every_status_of_the_customer_routes`, `::test_the_customer_routes_are_public_and_tagged`) | CONFIRMED |
| `SqlCustomerOrders.by_access_digest` (lines by position, address / invoice / pickup, lead times) | `sql_customer_orders.py:25-100` | integration | `test_sql_customer_orders.py::test_a_new_home_order_is_shown_with_its_lines_totals_and_address`, `::test_a_pickup_order_shows_the_pickup_data_and_no_address_or_invoice`, `::test_the_lines_come_in_their_stored_position`, `::test_a_made_to_order_line_carries_its_lead_times`, `::test_each_digest_opens_only_its_own_order` | CONFIRMED |
| `SqlCustomerOrders.match` (number AND stored email) | `sql_customer_orders.py:111-120` | integration | `test_sql_customer_orders.py::test_match_finds_the_order_by_number_and_its_stored_email`, `::test_match_refuses_a_wrong_email_an_unknown_number_and_a_swapped_pair` | CONFIRMED |
| `SqlLookupThrottle`: atomic counting under concurrency, window reset, independent keys, joins the transaction | `sql_lookup_throttle.py:21-38` | integration | `test_sql_customer_orders.py::test_concurrent_hits_on_one_key_each_get_their_own_count`, `::test_concurrent_first_hits_on_a_new_key_do_not_lose_an_attempt`, `::test_a_hit_exactly_one_window_later_starts_a_new_window`, `::test_keys_count_independently`, `::test_a_hit_joins_the_active_transaction_and_a_rollback_undoes_it` | CONFIRMED |
| Migration 0022 round trip on a scratch database | `migrations/versions/0022_orders_lookup_throttle.py` | integration | `test_migration_0022_orders_lookup_throttle.py::test_the_round_trip_rebuilds_exactly_the_same_schema`, `::test_downgrade_removes_only_the_table_and_keeps_the_orders`, `::test_upgrade_creates_the_table_with_the_planned_columns`, `::test_the_key_is_the_primary_key`, `::test_the_table_works_again_after_a_round_trip` | CONFIRMED |
| Page shown by the real checkout token | `order_links.py`, `sql_customer_orders.py` | integration | `test_customer_order_flow.py::test_the_token_of_a_real_checkout_opens_the_view_with_only_customer_keys` | CONFIRMED |

Observations (not gaps): the lookup consumes an attempt for a request that parses but names an
unknown number, as the plan says; a body that fails validation (422) consumes none
(`test_a_validation_error_does_not_count_an_attempt`). `GET /orders/lookup` reaches the token
route with the token `lookup` and answers 404 `ORDERS_ORDER_NOT_FOUND` (documented by a test).

## Review findings

### First review 2026-10-20 (reviewer subagent; written up by the main session)

The reviewer was interrupted before writing this section, so the main session recorded its
report here.

**Pass 1, checklist: 14 of 15 pass, 1 not applicable (no PR yet).**
- `plans-scope` is clean: 24 files, all inside the plan.
- `uv run just check` passes, with 3507 passed and 3 skipped; the harness has 685, and
  `openapi.json` is current.
- `uv run just test-integration` passes, with 1028 passed and 5 skipped.
- Layers, the CQRS split, contracts, migration 0022 (new, reversible, no cross-schema foreign
  key), `public_router`, wiring, secrets and docs all pass.

**Pass 2, findings: 0 high, 1 medium, 2 low, 2 info.**

1. **Medium: the permanent order token travels in the URL path, and is logged.**
   - Where: `http/router.py:422` (`GET /{access_token}`) and `:433` (`POST /{access_token}/cancel`).
   - The token never expires (an HMAC of the order id, decision 31). The uvicorn access log and
     any proxy log write the full path, and so does the unhandled-error log
     (`shared/http/middleware.py:146`).
   - Scenario: anyone who reads the logs can open any order, with its address and RFC, and
     cancel the unpaid ones, forever.
   - **Resolution (decision 42):** step 5 moves the token into a POST body, and the web link
     carries it in a URL fragment, as ADR 0010 does.
2. **Low: the docstring promises a phone that the view does not show.**
   - Where: `router.py:427-429`; `CustomerOrderView` (`contracts.py:245-270`) has neither the
     phone nor the name.
   - **Resolution (decision 43):** step 5 adds `customer_name` and `customer_phone`.
3. **Low, outside this plan: per-IP limits depend on the proxy setup** (`request.client.host`;
   uvicorn trusts `X-Forwarded-For` only from `FORWARDED_ALLOW_IPS`).
   - Filed as `plans/findings/platform-client-ip-behind-proxy.md`.
4. **Info: anyone can block one number's lookup for 15 minutes,** because the per-number counter
   is shared by every IP. The private link still works. **Accepted**, by the same trade-off as
   identity's per-email login limit.
5. **Info: what came out clean.**
   - **Number formats.** Every format of a number maps to one throttle key: `parse_order_number`
     accepts ASCII digits only, at most 9 (`domain/order.py:100-105`).
   - **No enumeration.** An unknown number and a wrong email run the same query and get
     byte-identical bodies. Unknown numbers count against the limit too.
   - **The throttle.** It is one atomic upsert, with no window race, and the counts commit before
     the refusal.
   - **Token lookup.** It goes by the SHA-256 digest on a unique index, over 256 bits of token.
   - **Cancel.** The number comes from the token's own order, under a row lock, and
     `cancel_unpaid` refuses anything that is not pending, so a paid order or another customer's
     order cannot be cancelled.
   - **The view.** No notes, actor ids, cancel reason, reservation id, email or digests.

**Status:** `review`. M1 and L2 need code (step 5), so the plan returns to `implementing`.

## Verification
