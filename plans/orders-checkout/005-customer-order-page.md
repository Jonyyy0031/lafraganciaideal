---
status: approved
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

## Test coverage

## Review findings

## Verification
