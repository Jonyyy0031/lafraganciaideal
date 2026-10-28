# fragancia-api

FastAPI modular monolith for La Fragancia Ideal. Architecture: [docs/architecture.md](../../docs/architecture.md).
Commands run from the repository root with `uv run just <recipe>`.

## Run

```bash
uv run just bootstrap        # once: services, apps/api/.env, migrations on both databases
uv run just api              # http://127.0.0.1:8100/api/v1/docs (Scalar, auto-reload)
uv run just worker           # background jobs: relays the outbox every 2 seconds, sends account emails,
                             # validates and expires catalog imports
```

Admin endpoints (`/api/v1/admin/*`) need a session cookie. Create an owner once with
`uv run just create-owner --email <email> --name <name>` (it asks for a 12+ character
password), then sign in with `POST /api/v1/auth/login`; the response sets the httpOnly
`fragancia_session` cookie that later admin calls send. After the first owner, invite staff
with `POST /api/v1/admin/invitations`; the email arrives in Mailpit (`http://127.0.0.1:8026`)
while the worker runs.

## Layout

```
src/fragancia_api/
├── config.py          Settings (pydantic-settings) from the environment and apps/api/.env
├── container.py       Composition root: the only place that knows concrete adapters; MODULES
├── main/
│   ├── http.py        FastAPI factory (create_app) and build_app (used by tests)
│   └── worker.py      arq WorkerSettings (outbox relay cron + module jobs: catalog imports)
├── shared/
│   ├── kernel/        Pure: Result, DomainError categories, AggregateRoot, DomainEvent, Money, ids
│   ├── application/   Ports: TransactionRunner, EventPublisher, EventSubscriptions, ActorResolver,
│   │                  EmailSender
│   ├── infrastructure/ Database + ContextVar session, SqlTransactionRunner, outbox + relay,
│   │                  SMTP email sender, Valkey health, logging, shared MetaData
│   ├── http/          Error mapping, declared access, health, request id, ServiceRegistry/provide
│   └── module.py      AppModule + Platform: what a module receives and hands back
├── shared/contracts/ Shared response shapes (Page)
└── modules/           Business modules (one PostgreSQL schema each); catalog = reference
migrations/            Alembic (one history; version table in schema `platform`)
tests/unit/            No services needed (`uv run just test`)
tests/integration/     Against fragancia_test (`uv run just test-integration`)
```

## Rules of thumb

New module or use case? Follow [docs/recipes/](../../docs/recipes/) and copy `modules/catalog`.


- **Expected failures are values**: use cases return `Ok(...)` / `Err(DomainError)`; routers
  call `unwrap(result)`. Raise only for the unexpected (ADR 0008).
- **Writes go through `TransactionRunner.run(work)`**: commits on `Ok`, rolls back on `Err` or
  exceptions. Repositories use `Database.session` (the active transaction); outside of one it
  raises.
- **Events go through the outbox**: `EventPublisher.publish(aggregate.pull_events())` inside the
  same `run(...)`. Subscribers (registered with `platform.subscriptions.subscribe(name, handler)`)
  must be idempotent: delivery is at-least-once, retried with backoff (ADR 0006).
- **Declared access**: put routes in `public_router()` or `admin_router()` — never a bare
  `APIRouter`. A test checks that every `/api/v1/admin` operation requires the session cookie (the `APIKeyCookie` security scheme).
- **Errors** always have the shape `{code, message, details?}`; categories map to
  401 (unauthenticated)/404/409/422/429 (rate limited)/503 (upstream unavailable); invalid input is 422 `VALIDATION_ERROR`; unexpected is 500 `INTERNAL_ERROR`.
- **Boundaries** are checked by `uv run just arch` (`.importlinter`). Do not work around a
  broken contract: explain why and propose an ADR.

## OpenAPI and the API reference

- `apps/api/openapi.json` is **committed**: every contract change shows up in the PR diff and
  phase 3 generates the web client from it. After changing a request/response model or a
  route, run `uv run just openapi`; `just check` fails while the file is stale.
- Operation ids are the route function names (`list_brands`) — keep them unique and readable.
  List responses use a concrete page class (`AdminBrandPage(Page[AdminBrand])`).
- `/api/v1/docs` serves **Scalar** outside production (script pinned in
  `shared/http/reference.py`). To call admin routes, call `log_in` first: the browser stores
  the session cookie and sends it with the admin calls (no manual auth setup).

## Settings

Add a field to `Settings` (or `DatabaseSettings` if migrations need it) in `config.py`, a line
to `.env.example`, and a validator if a bad value must stop startup. Production values come
from the environment.

## Mercado Pago webhooks

Payments are confirmed only by Mercado Pago's signed webhook (ADR 0004), at
`POST /api/v1/payments/webhooks/mercadopago`. The route checks the `x-signature` header with
`MERCADOPAGO_WEBHOOK_SECRET` (401 `PAYMENTS_WEBHOOK_SIGNATURE_INVALID` when it does not match,
503 when no secret is set) and queues the payment id in the outbox; the **worker** fetches the
payment from Mercado Pago and moves the order. Without the worker running, nothing is applied.

To receive webhooks in development:

1. Expose the local API (port 8100) through a public https tunnel (e.g. `cloudflared tunnel
   --url http://127.0.0.1:8100` or `ngrok http 8100`).
2. Put the credentials of your Mercado Pago test seller account in
   `MERCADOPAGO_ACCESS_TOKEN` (they start with `APP_USR-`, like production ones).
3. In the Mercado Pago panel, open "Webhooks". It has two tabs, "Modo de prueba" and "Modo
   productivo", each with its own URL and its own secret. Set the URL
   `https://<tunnel>/api/v1/payments/webhooks/mercadopago` in both tabs and mark the event
   "Pagos (legacy)" ("Order (Mercado Pago)" is the Orders API and does not apply).
4. Copy into `MERCADOPAGO_WEBHOOK_SECRET` in `apps/api/.env` the secret of the tab that matches
   the credentials: the "Modo de prueba" tab for a test seller account. With the other tab's
   secret every real notification is refused with 401. "Simular notificación" signs with the
   secret of the tab it is sent from, so a simulation can pass while real notifications fail.
   Restart the API.

With an http `STOREFRONT_URL` (as in development) the checkout is created without
`auto_return`, so after paying the guest comes back with the "Volver al sitio" button.

The checkout offers cards (one installment), cash in stores (OXXO and others, `ticket`) and
SPEI (`bank_transfer`); only bank counters and ATMs (`atm`) are excluded. A cash or transfer
ticket stays `pending` until the guest pays it and expires 48 hours after the link was
created. For OXXO, Mercado Pago moves that expiry to the end of the day in Mexico City time (up
to about 56 hours), and we use the expiry it reports. When its webhook arrives, the worker holds
the order once: orders moves the order's `payment_due_at` and its reservation to the ticket's
expiry plus 48 hours (never earlier), because a store payment can take one or two business days
to be credited. It publishes `orders.order.payment_pending`, which emails the customer "Tu ficha
de pago del pedido …" with the amount, the ticket's own expiry in Cancún time as the deadline,
and Mercado Pago's ticket link. The 48 hours count from the first ticket: a later ticket never
moves the deadline past the first ticket's expiry plus the 48-hour margin. It is still recorded,
and its email shows its own expiry, never later than the order's deadline. A ticket that had already
expired when its webhook is processed sends no email (`payments.ticket_expired_on_arrival`), and
a ticket link longer than 500 characters is stored empty (`payments.ticket_url_too_long`). The
order page reads the live tickets with `POST /api/v1/payments/pending` (the order's token in
the body); each ticket's `expires_at` there is the earlier of its own expiry and the order's
`payment_due_at`. When an order is paid or cancelled, payments publishes
`payments.checkout.close_requested` and, in that event's own outbox row, expires its Mercado
Pago preference (`PUT /checkout/preferences/{id}`), closes the stored checkout, so the old link
no longer takes payments, and cancels each of the order's `pending` cash and transfer tickets
(`PUT /v1/payments/{id}` with `status: cancelled`). A 400, 404, 409 or 422 from Mercado Pago on
either call is permanent and only logged (`payments.checkout_close_refused`,
`payments.ticket_cancel_refused`); any other 4xx (401, 403, 408, 429…), a 5xx or a transport
error retries the event, which ends in the failed-events panel if it never recovers. A
pending ticket whose webhook is processed after its order was already paid or cancelled is
cancelled too: the worker publishes `payments.ticket.cancel_requested`, whose own outbox row
sends the same cancel with the same permanent and retried answers. A ticket paid before the
cancel still arrives `approved` and is `refund_due`.

## Database changes

Define or change Core tables in `<module>/infrastructure/tables.py`, then:

```bash
uv run just db-revision "catalog brands"   # autogenerate NNNN_<slug>.py; review it by hand
uv run just db-migrate                      # development database
uv run just db-migrate --test               # test database (test-integration does it too)
```

Migrations must be reversible: CI runs upgrade → `alembic check` → downgrade → upgrade.

## Docker

```bash
docker build -f apps/api/Dockerfile --target runtime  -t fragancia-api .
docker build -f apps/api/Dockerfile --target migrator -t fragancia-migrator .
```

`runtime` serves HTTP on 8100 (`APP_ENV=production`, docs disabled, non-root user). The worker
is the same image with `arq fragancia_api.main.worker.WorkerSettings`. `migrator` runs
`alembic upgrade head`. Configure them with environment variables (`DATABASE_URL`,
`VALKEY_URL`, …).
