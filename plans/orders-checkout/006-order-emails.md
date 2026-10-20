---
status: testing
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

## Acceptance criteria

Run with the API, the worker and Mailpit (http://127.0.0.1:8026) on the development database.
Orders are placed through `POST /checkout` and moved through the panel routes of plan 004.

- [ ] **Placed.**
  - About one relay cycle after a checkout, Mailpit has "Recibimos tu pedido LFI-n" for the
    customer's email.
  - It lists the lines and totals, the address, and the deadline in Mexico City time.
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

## Review findings

## Verification
