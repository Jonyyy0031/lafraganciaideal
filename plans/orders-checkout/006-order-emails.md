---
status: verify
module: orders
min_implementer: mid
depends_on: ["005"]
---

# 006 — Order emails

## Context

The customer hears about their order by email, and the owners hear about every paid order
(decision 12). This last plan of the initiative applies decisions 12, 13 and 31, and the new
44–46.

**What exists today:**
- **Events the orders module already publishes through the outbox:**
  - `orders.order.placed`, with `{order_id, number}` (plan 003);
  - `orders.order.status_changed`, with `{order_id, number, from_status, to_status}`, for every
    move (plan 004).

  Marking a refund done publishes cancelled → cancelled. Both events are documented in
  `apps/api/src/fragancia_api/modules/orders/__init__.py`.
- **The pattern to copy** is inventory's low-stock email: an outbox subscriber in the worker,
  inside the relay's transaction (`modules/inventory/application/handlers/low_stock_email.py:1-80`).
  - It is plain text, with Spanish constants for the subject and body.
  - It goes through the shared `EmailSender` port (`shared/application/email.py:1-30`), whose
    `EmailMessage` has `to`, `subject` and a plain-text `body`.
  - A send failure raises, so the relay retries it (at-least-once delivery).
  - `EmailRejected` (a permanent 5xx) is logged and skipped.
  - Wiring: `platform.subscriptions.subscribe(event_name, handler)`
    (`modules/inventory/module.py:58-65`).
- **The owners come from identity's `OwnerDirectory` facade.** Inventory reads it through its
  own `OwnerRecipients` port and an `IdentityOwners` adapter
  (`modules/inventory/application/ports.py:45-50`;
  `modules/inventory/infrastructure/identity_owners.py:1-15`), with two `.importlinter` ignore
  lines (`apps/api/.importlinter:106-107`). Identity registers before orders in `MODULES`.
- **The order's data.**
  - `OrderQueries.detail(number)` returns everything an email needs: customer email and name,
    lines, totals, address or pickup point, carrier and tracking, refund status, and the history
    with its actors (`modules/orders/application/ports.py:88-98`).
  - `cancelled_by(actor)` maps an actor to customer, system or shop (plan 005,
    `modules/orders/domain/order.py`).
  - `HmacOrderLinks.token(order_id)` regenerates the private-link token
    (`modules/orders/infrastructure/order_links.py:7-20`).
- **The link format** comes from decision 42: the token goes in the URL fragment,
  `{storefront}/pedido#token=…`. Settings have `admin_web_url`, validated as an http(s) URL with
  no trailing slash (`apps/api/src/fragancia_api/config.py:68`, `:100-105`), but no storefront
  URL.

**Approach.**
- **Three subscribers in the orders module** (decision 44), plain text (decision 45):
  1. `orders.order.placed` → the customer's "pedido recibido" email.
  2. `orders.order.status_changed` → one customer email per status:
     - paid, preparing (decision 46), shipped, ready_for_pickup, delivered, cancelled;
     - **not** the refund's cancelled → cancelled.
  3. `orders.order.status_changed` with `to_status = paid` → one email per active owner.
- **Each handler reads the order** through `OrderQueries.detail`, so the email shows the
  order's current data. The template is picked by the event's `to_status`.
- **Customer emails carry the private link,** regenerated from the order id. They never include
  the shop's internal cancel reason (decision 40).
- **Rejected:**
  - A `notifications` module now (decision 44): it waits for the second channel (WhatsApp).
  - HTML emails (decision 45): the `EmailSender` port is plain text, and the visual design
    belongs to phase 3.

## Out of scope

- **Payment instructions** (OXXO reference, SPEI CLABE) and "payment pending" emails: the
  payments initiative. The "pedido recibido" email shows only the payment deadline.
- **A refund email** (decision 46 leaves it out).
- **Sending the link again on request**, and WhatsApp.
- **De-duplicating retries.** Delivery is at-least-once, as with the low-stock email, so a relay
  retry may send the same email twice.

## Dependencies

- **005 (done):** `cancelled_by` and the customer view rules (no internal reason).
- **004 (done):** `orders.order.status_changed`, `OrderQueries.detail` and the statuses.
- **003 (done):** `orders.order.placed` and `HmacOrderLinks`.
- **identity:** the `OwnerDirectory` facade.

## Steps

1. **Settings: the storefront URL**
   - Files: `apps/api/src/fragancia_api/config.py` (modify),
     `apps/api/.env.example` (modify)
   - Do:
     - Add `storefront_url: str = "http://localhost:4200"`, with a validator like
       `_check_admin_web_url`: it must start with http:// or https:// and not end with `/`.
     - Add `STOREFRONT_URL=http://localhost:4200` to `.env.example`, with a comment: the
       customer emails link to `{STOREFRONT_URL}/pedido#token=…`.
   - Observable result: the settings tests accept a valid URL and refuse a trailing slash.

2. **Application: the email texts and handlers**
   - Files: `apps/api/src/fragancia_api/modules/orders/application/handlers/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/orders/application/handlers/order_emails.py` (create),
     `apps/api/src/fragancia_api/modules/orders/application/ports.py` (modify)
   - Do:
     - **Ports:** `OwnerRecipients.active_owners() -> list[Recipient(name, email)]`, in orders'
       own terms.
     - **`ORDER_PAGE_PATH = "/pedido"`**, and `order_link(storefront_url, token)` returns
       `f"{storefront_url}{ORDER_PAGE_PATH}#token={token}"`.
     - **Templates.** Pure functions that take an `AdminOrderDetail` and the link, and return
       `(subject, body)`. The texts are in Spanish. Money is formatted as `$1,899.50 MXN` by one
       helper. Every body has a footer with the link and "Si tienes dudas, responde a este
       correo."
       - **placed:** "Recibimos tu pedido LFI-n". The body has:
         - the lines (label, quantity, line total), subtotal, delivery and total;
         - the delivery method with the address, or the pickup point and hours;
         - "Tienes hasta {payment_due_at, America/Mexico_City, dd/mm/yyyy HH:MM} para pagar";
         - the longest lead time, when there is one;
         - the link.
       - **paid:** "Confirmamos el pago de tu pedido LFI-n", plus the next step for the method.
       - **preparing:** "Estamos preparando tu pedido LFI-n", plus the estimated lead time when
         there is one.
       - **shipped:**
         - home: "Tu pedido LFI-n va en camino", with the carrier and tracking number;
         - local: the same, without a tracking number.
       - **ready_for_pickup:** "Tu pedido LFI-n está listo para recoger", with the pickup
         address and hours.
       - **delivered:** "Entregamos tu pedido LFI-n".
       - **cancelled:** "Tu pedido LFI-n fue cancelado". The reason comes from `cancelled_by`,
         never from the shop's typed reason:
         - customer: "a tu solicitud";
         - system: "porque no recibimos el pago a tiempo";
         - shop: "por la tienda".

         When `refund_status` is pending, the body adds "Te reembolsaremos el pago completo."
       - **owners' paid:** "Pedido pagado: LFI-n — $total". The body has the customer name,
         email and phone, the lines, the delivery method and the admin link
         `{admin_web_url}/admin/pedidos/LFI-n`.
     - **Handlers.** They follow `SendLowStockEmail`: they raise on a send failure, and log and
       skip `EmailRejected`.
       - `SendOrderPlacedEmail(queries, links, email, storefront_url)` reads `detail(number)`.
         When the order is missing it logs `orders.email_order_missing` and returns.
       - `SendOrderStatusEmail(...)` does nothing when `from_status == to_status` (the refund
         row). Otherwise it picks the template by `to_status`.
       - `SendPaidOrderToOwners(queries, owners, email, admin_web_url)` handles only
         `to_status == "paid"`. With no active owner it logs `orders.paid_email_no_owner`.
   - Observable result: unit tests check every template's subject and body (the link, the money
     format, the deadline in Mexico City time, no internal reason) and every handler branch,
     with fake ports.

3. **Infrastructure and wiring**
   - Files: `apps/api/src/fragancia_api/modules/orders/infrastructure/identity_owners.py` (create),
     `apps/api/src/fragancia_api/modules/orders/module.py` (modify),
     `apps/api/.importlinter` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/in_memory.py` (modify)
   - Do:
     - **`IdentityOwners`** over `OwnerDirectory`, a deliberate copy of inventory's adapter.
     - **`module.py`** subscribes the three handlers to the two event names. It passes the
       existing `HmacOrderLinks`, the `SqlOrderQueries`, `platform.email`,
       `platform.settings.storefront_url` and `platform.settings.admin_web_url`.
     - **`.importlinter`** gains two ignore lines:
       - `fragancia_api.modules.orders.infrastructure.identity_owners -> fragancia_api.modules.identity`
       - `fragancia_api.modules.orders.module -> fragancia_api.modules.identity`
     - **In-memory:** an `InMemoryOwnerRecipients` fake.
   - Observable result: `uv run just arch` passes, and the wiring test lists the three
     subscriptions.

4. **Docs and tests**
   - Files: `docs/architecture.md` (modify),
     `apps/api/src/fragancia_api/modules/orders/__init__.py` (modify),
     `apps/api/tests/unit/orders/` (modify), `apps/api/tests/integration/orders/` (modify),
     `apps/api/tests/unit/test_settings.py` (modify)
   - Do:
     - Extend the orders row of `docs/architecture.md`: the emails, which events they follow,
       and the `STOREFRONT_URL` setting.
     - In the orders `__init__.py` docstring, note that orders itself consumes both events to
       send emails.
     - The tester writes the layered tests. The integration test runs the relay against Mailpit
       or the fake sender used by inventory's tests.
   - Observable result: `uv run just check` and `uv run just test-integration` pass.

5. **Review repair (added 2026-10-20 after the first review; decisions 47 and 48)**
   - Files: `apps/api/src/fragancia_api/modules/orders/domain/order.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/application/handlers/order_emails.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/module.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/__init__.py` (modify),
     `apps/api/src/fragancia_api/shared/infrastructure/smtp_email_sender.py` (modify),
     `docs/architecture.md` (modify),
     `apps/api/tests/unit/orders/` (modify), `apps/api/tests/integration/orders/` (modify),
     `apps/api/tests/unit/test_smtp_email_sender.py` (modify)
   - Do:
     - **M1, the owners' email gets its own outbox row (decision 47).**
       - `Order.mark_paid` also records `OrderPaid(DomainEvent)`, with
         `name = "orders.order.paid"` and `{order_id, number}`.
       - `SendPaidOrderToOwners` subscribes to `orders.order.paid` instead of
         `orders.order.status_changed`, so it no longer filters on `to_status`.
       - Each outbox row now has exactly one orders email handler. A failure on the customer's
         address can no longer block or repeat the owners' email, and the reverse holds too.
       - Document the event in the orders `__init__.py` docstring.
     - **M1-A at the source.** In `SmtpEmailSender._send_blocking`, an
       `smtplib.SMTPNotSupportedError` raised by `send_message` becomes `EmailRejected`: the
       server cannot take a non-ASCII address without SMTPUTF8, so no retry will help.
       - Comment it next to the existing `SMTPRecipientsRefused` rule.
     - **M1-B is accepted:** a temporary refusal of one owner's address still resends the
       owners' email to the owners already served, within its own row only. This matches the
       low-stock email (inventory decision 36).
     - **L1, the shop's time zone (decision 48).** `SHOP_TIMEZONE = ZoneInfo("America/Cancun")`,
       as catalog decision 66 fixed the shop's days (`modules/catalog/domain/import_cells.py:64`).
       The deadline is labelled "hora de Cancún". Update the tests' expected times: Cancún is
       UTC−5 all year.
     - **L2 is accepted:** emails describe the order as it is when they are sent, under
       at-least-once delivery.
     - **Info.** `_lead_time` says "1 día" in the singular and "N días" otherwise. The plan's
       test-coverage note about the token digest is reworded to "independent of the view's
       secret".
     - **Docs:** `docs/architecture.md` (orders row) names `orders.order.paid` for the owners'
       email.
   - Observable result:
     - Tests show that `mark_paid` records both events.
     - The owners' handler is subscribed to `orders.order.paid` only.
     - In the relay flow, a customer send failing on the paid status row still lets the owners'
       email go out from its own row.
     - `SMTPNotSupportedError` becomes `EmailRejected`.
     - The deadline is shown in Cancún time.
     - `check`, `test-integration` and `plans-scope` are clean.

## Acceptance criteria

Run with the API, the worker and Mailpit (http://127.0.0.1:8026) on the development database.
Orders are placed through `POST /checkout` and moved through the panel routes of plan 004.

- [ ] **Placed.**
  - About one relay cycle after a checkout, Mailpit has "Recibimos tu pedido LFI-n" for the
    customer's email.
  - It lists the lines and totals, the address, and the deadline in Cancún time (decision 48).
  - Its link, `{STOREFRONT_URL}/pedido#token=…`, carries a token that `POST /orders/view`
    accepts.
- [ ] **Paid.**
  - Marked paid from the panel: the customer gets "Confirmamos el pago…".
  - Every active owner gets "Pedido pagado: LFI-n — $…", with the admin link.
- [ ] **Moves.**
  - preparing, then shipped (Estafeta / 123456, which the email shows), then delivered: one
    email each.
  - A pickup order marked ready_for_pickup gets the pickup address and hours.
- [ ] **Cancelled.**
  - Customer cancel: "a tu solicitud".
  - Overdue job: "no recibimos el pago a tiempo".
  - Shop cancel with an internal reason: "por la tienda", and the email does not contain the
    typed reason.
  - A paid cancellation says it will be refunded.
  - Marking the refund done sends no email.
- [ ] **Outbox.** Every handled event is published (`published_at` set, 0 failures).

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | no      | (no domain change) |
| application | yes     | Each template's subject and body per status and delivery method (link, money format, Mexico City time, lead time, refund sentence, cancelled_by wording, never the typed reason); handlers: refund row skipped, owners only on paid, missing order logged, no owner logged, `EmailRejected` logged and skipped, other failures raise |
| http        | no      | (no route change) |
| integration | yes     | The relay delivering both events to the handlers with the real `SqlOrderQueries` and a capturing sender; the owner adapter over identity's facade; the settings validator |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

None. No existing test broke, so no test file was touched; the tester writes the layered tests
(templates, handlers, settings validator, relay integration) in the next phase.

## Test coverage

Baseline (before any test): `uv run just check` green (3519 passed, 3 skipped);
`uv run just test-integration` green (1028 passed, 5 skipped). Closing: `just check` 3620 passed,
3 skipped; `just test-integration` 1038 passed, 5 skipped. No GAP, no NOT CONFIRMED, no product
code touched. Files: `tests/unit/orders/test_order_emails.py`,
`tests/unit/orders/test_order_emails_wiring.py`, `tests/unit/test_settings.py` (appended),
`tests/integration/orders/test_order_emails_flow.py`.

| Behavior | Source | Layer | Test | State |
| -------- | ------ | ----- | ---- | ----- |
| `storefront_url` refuses no scheme / trailing slash, accepts a base URL, default local | `config.py` `_check_storefront_url` | application (settings) | `test_settings.py::test_storefront_url_*` | CONFIRMED |
| Money `$1,899.50 MXN`; link `{storefront}/pedido#token=` | `order_emails.py` `money`, `order_link` | application | `test_order_emails.py::test_money_*`, `test_the_link_puts_the_token_*` | CONFIRMED |
| Placed: lines, subtotal/delivery/total, address per method (home, local, pickup point + hours), lead time only when present | `placed_email`, `_delivery` | application | `test_placed_*` | CONFIRMED |
| Deadline in Mexico City time, incl. UTC already next day, midnight, year crossing, non-UTC offset | `placed_email` | application | `test_the_deadline_is_shown_in_mexico_city_time_*`, `test_a_deadline_with_another_offset_*` | CONFIRMED |
| paid per method; preparing lead time; shipped home (carrier+tracking) / local (none); ready_for_pickup address+hours; delivered | `STATUS_TEMPLATES` | application | `test_paid_*`, `test_preparing_*`, `test_shipped_*`, `test_ready_for_pickup_*`, `test_delivered_*` | CONFIRMED |
| Every customer template x 3 methods: greeting, link footer; exactly six statuses | `_wrap`, `STATUS_TEMPLATES` | application | `test_every_customer_template_*`, `test_the_customer_templates_cover_*` | CONFIRMED |
| Cancelled wording from `cancelled_by` (customer / system / shop); refund sentence only when pending; wording survives the refund history row | `cancelled_email`, `cancelled_actor` | application | `test_cancelled_*`, `test_a_pending_refund_*`, `test_without_a_pending_refund_*`, `test_after_the_refund_mark_*` | CONFIRMED |
| The shop's typed reason never in any customer email (all templates, handler path, SQL path) | `order_emails.py` | application, integration | `test_the_typed_cancel_reason_*`, `test_the_cancel_reason_is_absent_from_every_customer_template`, `test_a_shop_cancel_email_sent_by_the_handler_*`, `test_a_shop_cancel_says_por_la_tienda_*`, `test_a_paid_cancel_promises_the_refund_*` | CONFIRMED |
| Owner email: subject with total, customer data, lines, method, admin link, no customer link | `owners_paid_email` | application | `test_the_owner_email_*` | CONFIRMED |
| Handlers: placed / status send to the customer; missing order logged (`orders.email_order_missing`); send failure raises; `EmailRejected` logged and skipped | `SendOrderPlacedEmail`, `SendOrderStatusEmail` | application | `test_the_placed_handler_*`, `test_the_status_handler_*`, `test_a_move_sends_the_template_*` | CONFIRMED |
| Refund row (cancelled to cancelled) sends nothing and reads nothing; status without template sends nothing | `SendOrderStatusEmail` | application | `test_the_refund_row_*`, `test_a_status_without_a_template_*` | CONFIRMED |
| Owners only on `paid`; one email per active owner; none logged (`orders.paid_email_no_owner`); missing order; failure raises; rejected owner skipped | `SendPaidOrderToOwners` | application | `test_every_active_owner_*`, `test_the_owners_hear_only_about_paid_*`, `test_with_no_active_owner_*`, `test_the_owner_handler_*`, `test_a_rejected_owner_*` | CONFIRMED |
| Link token from the email accepted by the real `GetCustomerOrder` and `POST /api/v1/orders/view`; tampered token refused | `HmacOrderLinks`, `GetCustomerOrder`, router | application | `test_the_token_in_the_email_link_is_accepted_*`, `test_a_tampered_token_*` | CONFIRMED |
| Same token from a real checkout opens `GetCustomerOrder` over `SqlCustomerOrders` | `SqlCustomerOrders` | integration | `test_order_emails_flow.py::test_a_checkout_emails_the_customer_*` | CONFIRMED |
| Container subscribes the three handlers to the two events with the configured URLs and real adapters; `IdentityOwners` maps only active owners | `module.py`, `identity_owners.py` | application (wiring) | `test_order_emails_wiring.py` | CONFIRMED |
| Relay delivers placed and status_changed with real `SqlOrderQueries`, `IdentityOwners` over the real facade and a capturing sender: placed, paid (customer + active owners only, not inactive/staff), preparing/shipped (Estafeta/123456)/delivered, pickup ready, customer / overdue-job / shop / paid cancel, refund done sends no email, all rows published with no failure, no owner still emails the customer | handlers over SQL | integration | `test_order_emails_flow.py` (10 tests) | CONFIRMED |
| Real SMTP delivery to Mailpit; the relay retry after an SMTP failure for these handlers | `SmtpEmailSender` | integration | not written: a capturing sender was requested; the retry mechanism is proven for inventory by `test_low_stock_email_flow.py` and the raise-on-failure branch by unit tests here | NOT CONFIRMED (not exercised) |

Notes: the token digest is a plain SHA-256 of the token, independent of the view's secret, so the
view accepts any token whose digest was stored at checkout (no test assumes otherwise).

## Review findings

Reviewed 2026-10-20 (reviewer subagent), diff `main..HEAD` (cbe3ac0, f2f4458, 831ff71); clean
worktree; no PR yet.

**Checklist: 14/15 pass, 1 not applicable yet (PR body).**

- [x] `plans-scope`: "Every change is inside the plan" (17 changed / 14 declared, the rest
      allowed). `.importlinter` hot-file edit is append-only (two lines at the end of the
      orders block).
- [x] `uv run just check` green: 3620 passed, 3 skipped; harness 685 passed; lint, types, arch,
      plans-lint, adapter drift all pass.
- [x] `uv run just test-integration`: green, 1038 passed, 5 skipped (the 10 tests in `test_order_emails_flow.py` included).
- [x] Business rules: none in adapters. Templates and handlers sit in `application/handlers/`
      as inventory's low-stock email does; the cancel wording reuses domain `cancelled_by`.
- [x] CQRS-lite: the handlers only read through the `OrderQueries` port; no command or
      repository change.
- [x] Contracts: no contract change; `openapi.json` correctly untouched.
- [x] Errors: no new `Result` paths; handlers raise on failure on purpose (outbox retry).
- [x] Money: integer cents, formatted only at the boundary (`money()`); datetimes come from
      the DB as aware UTC; no new ids.
- [x] No schema change, no migration.
- [x] No routes added.
- [x] Wiring: `test_order_emails_wiring.py` and `test_container.py` green; identity comes
      before orders in `MODULES`, so `services.get(OwnerDirectory)` resolves; adapters are
      built only in `module.py`.
- [x] No secrets or real personal data. Tests use `example.com`/`.test` and a literal test
      secret.
- [x] `## Deviations` ("None") holds up. Spot check: the plan says local shipping has "no
      tracking number", and the code prints tracking whenever it is set. The domain
      (`order.py:354-376`) refuses carrier and tracking for local, so the result matches.
- [x] Docs: the orders row, the `notifications` row and the paid flow in
      `docs/architecture.md`, the orders `__init__.py` docstring, `.env.example` and README
      decisions 44–46 are updated.
- [ ] PR body: no PR exists yet (N/A at this point; the main session writes it).

**Focus areas asked for (all checked, no defect unless listed below):**

- *No internal data in customer emails.* No customer template reads `history[].note`,
  `history[].actor` (only through `cancelled_by`), `notes`, `invoice` or admin ids. The cancel
  wording comes from `CANCEL_REASONS[cancelled_by(...)]`. Tests show the typed reason is absent
  on all templates, on the handler path and on the SQL path.
- *`cancelled_by` source row.* `cancelled_actor` (`order_emails.py:146-151`) takes the latest
  history row with `to_status == "cancelled"` and `from_status != "cancelled"`, so the refund
  mark (cancelled → cancelled) is skipped. Forward-only transitions allow only one such row,
  so this matches `SqlCustomerOrders` (first move to cancelled, `sql_customer_orders.py:49`).
  The default `"system"` for an empty history cannot happen (a cancelled order always has its
  row).
- *Reads inside the relay transaction.* `SqlOrderQueries.detail` opens its own
  `Database.reader()` session, not the relay's unit of work. The order rows were committed
  together with the outbox row, so the reader always sees them. There is no lock interplay:
  the relay only locks the outbox row. Each delivery uses two pooled connections, the same as
  inventory. The email reflects the order's state at delivery time, not at event time; the
  plan intends this.
- *Owner email.* It carries the customer's name, email and phone, the lines, the delivery and
  address, and `{admin_web_url}/admin/pedidos/LFI-n`, with no customer link. This is intended.
- *`storefront_url` validator.* It is a faithful copy of `_check_admin_web_url`, and
  `order_link` concatenates without a double slash. Like ADMIN_WEB_URL, there is no
  production guard against the localhost default (an existing pattern, not a regression).
- *Time zone.* `payment_due_at.astimezone(ZoneInfo("America/Mexico_City"))`. `tzdata` is a
  declared dependency (slim image is fine), and the label says "hora de Ciudad de México", so
  the text is correct as written. See L1 for the consistency question.
- *Tester's note on the digest.* `HmacOrderLinks.digest` is `sha256(token)` with no secret,
  so `GetCustomerOrder` verifies a token whatever secret its own `links` holds. The note's
  wording ("whatever secret the handler used") overstates this: the handler's token only
  matches if the handler's secret equals the one `PlaceOrder` used at checkout. This holds in
  practice. `module.py` passes the same `links` instance to checkout and to the handlers, and
  the API and the worker both read `ORDER_LINK_SECRET` (required in production; the same dev
  fallback otherwise). The integration test uses the same secret for checkout and handler.
  Documentation imprecision only; no test depends on the wrong reading.

### Medium

- **M1: the customer and owner handlers share one outbox row, so a customer-address failure
  withholds the owners' "pedido pagado" and an owner-side failure resends the customer
  email.** Location: `module.py:205-222` (two subscribers on `orders.order.status_changed`)
  with `outbox.py:135-144` (one transaction per row; any exception rolls back and retries the
  whole row, up to `MAX_ATTEMPTS = 295` over about 24 h).
  - Failure scenario A (uncertain: depends on the production SMTP server). `CustomerEmail`
    (`domain/customer.py:53-66`) accepts non-ASCII addresses such as `josé@example.com`.
    `smtplib.send_message` raises `SMTPNotSupportedError` when the server does not advertise
    SMTPUTF8, and `SmtpEmailSender` does not map that to `EmailRejected`. So the `paid` row
    raises in `SendOrderStatusEmail`, which is subscribed first, and `SendPaidOrderToOwners`
    never runs. The owners get no paid notice for about 24 hours, then never (row
    `failed_at`), against decision 12. The same applies to a persistent 4xx for the customer's
    RCPT (mailbox full) when the server delivers directly.
  - Failure scenario B. One owner's address returns a temporary 4xx. Every retry of the row
    resends "Confirmamos el pago" to the customer and "Pedido pagado" to the owners ahead in
    the loop, up to 295 times. The plan's out-of-scope note accepts "may send the same email
    twice", not this amplification. Inventory had the owner-loop half of this; the coupling to
    customer input is new here.
  - Needs a decision from the user. Either accept and record it as a deviation/finding, or
    isolate it in code (for example, run the owner handler first, catch non-rejection errors
    per handler, map `SMTPNotSupportedError` to `EmailRejected`, or give each audience its own
    event). The reviewer does not choose.

### Low

- **L1 (uncertain, plan-level): the deadline time zone differs from the shop's established
  zone.** Catalog decision 66 fixes the shop's days in `America/Cancun` (UTC-5,
  `catalog/domain/import_cells.py:64`), and the pickup examples are in Cancún. This plan
  formats the payment deadline in `America/Mexico_City` (UTC-6, `order_emails.py:30,99`).
  - Scenario: a Cancún customer reads "hasta 20/10/2026 14:30 (hora de Ciudad de México)",
    which is 15:30 on their clock. The text is accurate as labeled, but it is one hour off
    from the shop's local time and adds a second shop zone.
  - Confirm with the user which zone is intended. If it is Cancún, change the constant and
    the label.
- **L2: a delayed or retried event can describe a stale situation.** The templates read the
  current order. If the "placed" row is delivered late (an SMTP outage past the 30-minute
  deadline, or after the customer cancels), the email still says "Tienes hasta … para pagar"
  for an order that is already cancelled. Retries with backoff also let later rows go first,
  so "preparando" can arrive before "pago confirmado". Accepted by the at-least-once design;
  recorded only so verify does not treat it as new.

### Info

- `_lead_time` prints "hasta 1 días" when `lead_time_max_days == 1` (grammar only).
- Tester's digest note: see the focus area above; consider rewording it to "independent of
  the view's secret".

**Result:** M1 needs a user decision, either a code change or a recorded acceptance, so the
status stays `review`.

## Repair log

Step 5 implemented 2026-10-20, after the review (M1, L1; L2 and M1-B accepted).

- **M1:** `OrderPaid` (`orders.order.paid`, `{order_id, number}`) is recorded by `Order.mark_paid`
  after `OrderStatusChanged`; `SendPaidOrderToOwners` no longer filters on `to_status` and is
  subscribed to `orders.order.paid` only. Documented in the orders `__init__.py` and in
  `docs/architecture.md`.
- **M1-A:** `SmtpEmailSender._send_blocking` maps `SMTPNotSupportedError` to `EmailRejected`.
- **L1:** `SHOP_TIMEZONE = ZoneInfo("America/Cancun")`; the label is "hora de Cancún".
- **Info:** `_lead_time` says "1 día" and "N días"; the digest note and the placed acceptance
  criterion are reworded.
- **Tests updated:** Cancún expectations (unit and integration), `mark_paid` now records two
  events (`test_mark_paid_*`; the full-walk test filters `OrderStatusChanged`), owners' handler
  tests use a `paid_message()` and the "only about paid" parametrized test became
  `test_the_owners_handler_needs_no_status_filter_*`, wiring (status_changed has one handler,
  `orders.order.paid` has the owners'), integration relay subscribes the owners' handler to
  `orders.order.paid`.
- **Tests added:** `OrderPaid` payload and order (domain), singular lead time, a no-DST Cancún
  case, `SMTPNotSupportedError` becomes `EmailRejected`, and two relay tests: a customer send
  failing on the paid row still lets the owners' email go out from its own row (status row
  retried, `attempts == 1`), and the reverse.
- **Results:** `just check` green (3617 passed, 3 skipped; harness 685); `just test-integration`
  1040 passed, 5 skipped. Real SMTP to Mailpit not exercised (as before).
- **Re-review (2026-10-20, main session).**
  - **M1:** the owners' handler is subscribed only to `orders.order.paid` (`module.py`).
    `mark_paid` records both events. The two relay tests prove each row fails and retries
    alone.
  - **M1-A:** `SMTPNotSupportedError` is caught around `send_message` only, where it means the
    message needs SMTPUTF8. The sender address is configured ASCII, so it can only come from a
    recipient.
  - **L1** uses `America/Cancun` and the "hora de Cancún" label.
  - **Info** is fixed.
  - No new defect found. The changes are one event, one subscription, one `except` and the time
    zone, all pinned by tests, so there is no separate tester round. The status moves to
    `verify`.

## Verification

2026-10-20, main session, live against the development database, with Mailpit receiving every
email through the real `SmtpEmailSender`.
- **Setup:** the API on :8100 and an arq worker started from this branch.
- **Scripts:** `scratchpad/qa_inv/qa_emails.py` and `qa_emails_refund.py`. Mailpit's API was
  queried by recipient.
- **Accounts:**
  - three active owners: `example@example.com`, `verifier007@example.test` and
    `verifier@example.test`;
  - a fresh customer address per run.
- **Delivery options:** set for the run (home 18000, free from 250000; pickup on) and restored.

**A stale worker.** A first attempt sent no email. A worker left running from an earlier QA
session, with the code from before this plan, was also relaying, and it published the events with
no handler. Once it was stopped, the run below used only this branch's worker.

- [x] **Placed.**
  - About one relay cycle after the checkout of `LFI-1017`, Mailpit had "Recibimos tu pedido
    LFI-1017" for the customer.
  - It listed the line ($1,899.50 MXN), the subtotal, delivery $180.00 and total $2,079.50 MXN,
    and the home address.
  - It said "Tienes hasta 20/10/2026 15:32 (hora de Cancún) para pagar". The database has
    `payment_due_at` 20:32 UTC, which is 15:32 in Cancún (UTC−5).
  - Its link `http://localhost:4200/pedido#token=…` carries a token that `POST /orders/view`
    accepted (200).
- [x] **Paid.**
  - The customer got "Confirmamos el pago de tu pedido LFI-1017".
  - Each of the three owners got "Pedido pagado: LFI-1017 — $2,079.50 MXN", with the
    customer's name, email and phone, the line, the address and
    `http://localhost:4200/admin/pedidos/LFI-1017`.
  - The owners' emails come from the separate `orders.order.paid` rows.
- [x] **Moves.**
  - preparing, shipped (Estafeta / 123456) and delivered each sent one email: "Estamos
    preparando…", "Tu pedido LFI-1017 va en camino" (with Estafeta and 123456 in the body) and
    "Entregamos tu pedido LFI-1017".
  - `LFI-1018` (pickup) marked ready_for_pickup sent "Tu pedido LFI-1018 está listo para
    recoger", with "Av. Tulum 123" and the hours.
- [x] **Cancelled.**
  - Customer cancel (`LFI-1019`): "fue cancelado … a tu solicitud".
  - Shop cancel with the typed reason "MOTIVO-INTERNO-QA" (`LFI-1020`): "por la tienda".
  - Overdue job (`LFI-1021`): "porque no recibimos el pago a tiempo".
  - Paid cancellation (`LFI-1022`): "por la tienda. Te reembolsaremos el pago completo."
  - "MOTIVO-INTERNO-QA" appears in none of the customer's emails.
  - Marking `LFI-1022`'s refund done sent no further email (still 3 after 20 s).
- [x] **Outbox.** For the run, `orders.order.placed` ×6, `orders.order.status_changed` ×9 and
  `orders.order.paid` ×2. All of them are published, with 0 failures.
  - The first run's placed event was published by the stale worker, without an email.
  - `LFI-1018` was not cancelled in this run, because a ready-for-pickup order cannot be
    cancelled (409, plan 004). The script's mistake was covered by `LFI-1022`.

NOT VERIFIED live:
- the relay retry after an SMTP failure, and an address refused for SMTPUTF8 (both
  integration-tested);
- delivery through a production SMTP server.

The orders `LFI-1017` to `LFI-1022` stay in the development database. Two units really sold
(`LFI-1017`'s 50 ml and `LFI-1018`'s made-to-order bottle); the rest were cancelled or
released.
