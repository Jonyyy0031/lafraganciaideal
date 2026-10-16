# Architecture

> Sections marked _(to be built)_ describe the target that plans must respect; everything else
> exists. API conventions in practice: [apps/api/README.md](../apps/api/README.md). Decisions
> are in [adr/](adr/).

## Overview

```
          ┌───────────────────────────────────────────────┐
          │ apps/web — Angular (SSR)            (phase 3) │   UI only. No business rules.
          │  storefront (/)        admin (/admin)         │
          └──────────────────────┬────────────────────────┘
                                 │ packages/api-client (generated from OpenAPI)
                                 ▼ HTTP /api/v1              ◀── Mercado Pago webhooks
┌─────────────────────────────────────────────────────────────────────────────┐
│ apps/api — FastAPI                                                       ✔  │
│ one HTTP process + one worker process (arq), same image and code            │
│                                                                             │
│  modules/: catalog │ inventory │ orders │ payments │ identity │ notifications│
│            shipping (later)                                                 │
│  shared/: kernel (Result, Money, errors) · outbox · event bus · db session  │
└────────────┬──────────────────────┬───────────────────────┬─────────────────┘
             ▼                      ▼                       ▼
      PostgreSQL 18            Valkey 9 (arq)          S3 (RustFS in dev)
   one schema per module     queues, scheduled jobs      product photos
             ✔ available in infra/docker          Mailpit captures email in dev
```

## Local infrastructure ✔

`infra/docker/compose.yaml` runs the dependencies only; the apps will run on the host for fast
reloads. Ports bind to `127.0.0.1`; images are pinned by digest. `just bootstrap` creates
`.env`, starts everything, ensures the `fragancia_test` database and the `fragancia-media`
bucket, and installs git hooks. See [README.md](../README.md#local-services).

## Modular monolith

One deployable with **strict internal boundaries** ([ADR 0002](adr/0002-modular-monolith.md)).
Each module:

- Owns its **PostgreSQL schema** and tables. Nobody else reads or writes them; no foreign keys
  across schemas (references across modules are plain ids validated through the owning module).
- Exposes a **public API** in its `__init__.py` (facade + types + event names). Everything else
  is private.
- Talks to other modules in two ways:
  - **Synchronously**: through a port of its own (e.g. `orders` defines `ProductCatalog`)
    implemented by an adapter that calls the other module's facade (anti-corruption layer).
  - **Asynchronously**: domain events published through the **outbox**.
  - Built example: `inventory` defines `PresentationCatalog`, implemented in
    `inventory/infrastructure/catalog_presentations.py` over the catalog's public
    `PresentationDirectory` facade, and `OwnerRecipients`, implemented in
    `inventory/infrastructure/identity_owners.py` over identity's `OwnerDirectory` facade.
    `.importlinter` allows exactly those imports (the catalog and identity ones), plus
    `inventory/module.py` fetching the facades from the service registry (the catalog and
    identity register first in `MODULES`).

Modules (✔ = built; `catalog` is the **reference module** — see [recipes/new-module.md](recipes/new-module.md)):

| Module          | Responsibility                                                                        |
| --------------- | ------------------------------------------------------------------------------------- |
| `catalog`       | ✔ Brands, olfactory families, concentrations, perfumes and presentations (back office), public catalog (filters, search, sort, detail by slug), perfume photos (S3, WebP), Excel import (template, validation in the worker, confirm) |
| `inventory`     | ✔ Back-office stock ledger: units per in-stock presentation, append-only movements (restock, adjustment, loss, return, count), low-stock thresholds and list, stock reservations (reserve, extend, commit, release, expiry job) for checkout and payment. Emails the owners when a presentation drops to its low-stock threshold |
| `orders`        | ✔ Delivery options (home, local, pickup; quote). Next: cart → order; the `Order` aggregate and its state machine |
| `payments`      | `PaymentGateway` port → Mercado Pago adapter (Checkout Pro); idempotent webhooks      |
| `identity`      | ✔ Back-office users (owner, staff), opaque sessions in a cookie, login throttle, invitations, password reset by email, deactivation, `OwnerDirectory` facade (active owners, for other modules) |
| `notifications` | Order emails first; WhatsApp (Cloud API) later; driven by events                      |
| `shipping`      | `CarrierGateway` port (quotes, labels, tracking) — later                              |

## Layers inside a module

```
modules/orders/
├── domain/          Aggregates, value objects, rules, events, write ports. Pure: no IO,
│                    no FastAPI, no SQLAlchemy. Depends only on the shared kernel.
├── application/
│   ├── commands/    Writes → return a Result
│   ├── queries/     Reads → XxxQueries port + thin use case
│   └── ports/       What this module needs from the outside, in ITS terms
├── infrastructure/  Adapters: SQLAlchemy repositories/queries, other modules, in-memory fakes
├── http/            FastAPI router: translates HTTP ↔ use case. Zero logic.
├── module.py        AppModule: register(platform, services) + routers
└── __init__.py      Public API of the module
```

Dependency rule: `http → application → domain ← infrastructure`. Enforced with import-linter
(`apps/api/.importlinter`): the shared kernel is pure, shared layers point inwards, only
`container.py`/`main/` know the composition root; each module adds its own layers contract.

The same shape applies to `shared/`: `kernel` (pure) ← `application` (ports) ←
`infrastructure` (adapters) · `http`.

## Lightweight CQRS

|                | Command (write)                        | Query (read)                               |
| -------------- | -------------------------------------- | ------------------------------------------ |
| Goes through   | Domain aggregate + `XxxRepository`     | `XxxQueries` port                          |
| Business rules | Yes, in the aggregate                  | Nothing to protect                         |
| Returns        | `Result[T, DomainError]` (Ok / Err)    | Pydantic response model                    |
| Transaction    | `TransactionRunner.run(work)`          | `Database.reader()`                        |
| Optimized for  | Consistency                            | Exact selects, joins, views                |

Same database for both sides. Repositories have no "for a screen" methods; that is what
`XxxQueries` is for.

## Contracts

Pydantic v2 request/response models in each module's `http/` are the single source of truth
for the HTTP API. FastAPI generates the OpenAPI document; `packages/api-client` is generated
from it for Angular. No hand-written duplicate types in the frontend.

- **Request body limits**: `BodySizeLimitMiddleware` (outside `RequestContextMiddleware`
  and inside CORS, before any route code runs) answers 413 `REQUEST_TOO_LARGE` when a body
  passes the limit, whether declared in `Content-Length` or streamed. JSON, form, text and
  body-less requests are limited to `REQUEST_MAX_BYTES` (1 MiB); any other media type is a
  binary upload, limited to the larger of the photo and catalog-import limits. The upload
  routes keep their own limits, at or below that binary limit.

## The order lifecycle _(to be built)_

```
                 payment approved            ready to ship         carrier/hand-off      received
pending_payment ───────────────────▶ paid ─────────────────▶ awaiting_fulfillment ─▶ shipped ─▶ completed
      │                                │
      │ expired / cancelled            │ cancelled by admin (refund handled in payments)
      ▼                                ▼
  cancelled                         cancelled
```

- Transitions are methods on the `Order` aggregate; invalid transitions are domain errors.
- Made-to-order items follow the same states; they skip the stock reservation and carry a
  longer estimated delivery time.
- The admin panel lists orders by state, which replaces manual tracking of pending payments
  and deliveries.

## A request, end to end

```
POST /api/v1/admin/brands
 → BodySizeLimitMiddleware: body over the limit → 413 REQUEST_TOO_LARGE, before any route code
 → RequestContextMiddleware: request id (X-Request-ID), bound to every log line
 → admin_router dependency require_admin: session cookie → ActorResolver → 401 / 403
 → router dependency require_permission("catalog:manage"): missing permission → 403
 → FastAPI validates the body with the Pydantic contract (422 VALIDATION_ERROR)
 → router: use_case = provide(CreateBrand) from the ServiceRegistry; no logic here
 → CreateBrand.execute() → TransactionRunner.run(work):
       value objects (Err → rollback) → repository checks → Brand.create(...) records an event
       → repository.add() (Database.session = the active transaction)
       → EventPublisher.publish(brand.pull_events())  (outbox row, same transaction)
     Ok → commit · Err → rollback
 → unwrap(result): Ok → 201 {id} · Err → status by category (404/409/422)
```

## Events, outbox and jobs

- Domain events are written to `platform.outbox` **in the same transaction** as the change
  ([ADR 0006](adr/0006-outbox-from-day-one.md)). The worker's 2-second cron
  (`OutboxRelay.relay_batch`) takes due rows with `FOR UPDATE SKIP LOCKED`, delivers each in
  its own transaction to the subscribers registered for its name, and marks it published; a
  failure records `attempts`/`last_error` and backs off exponentially (max 5 minutes). After
  295 attempts (about 24 hours) the row gets `failed_at`, is logged
  (`outbox.delivery_failed_permanently`) and the relay skips it; owners (`outbox:manage`) list
  failed events at `GET /admin/outbox/failed` and retry one with `POST /admin/outbox/{id}/retry`,
  which restarts the window. Delivery is at-least-once; subscribers are idempotent.
- **Stock on the storefront** (an event feeding a copy): `inventory` publishes
  `inventory.stock.changed` (`presentation_id`, `available`, effective `threshold`, `as_of`) on
  every movement, every threshold change and, for every stock item, every change of the global
  threshold. The catalog subscribes by name and keeps the figures in three columns of
  `catalog.presentations` (`stock_available`, `stock_threshold`, `stock_as_of`), applying an
  event only when its `as_of` is newer, so replays and reordering are harmless. The public
  reads compute the stock status from those columns, so they can filter and sort on it without
  reading across schemas.
- **Initial stock from the Excel import** (an event feeding count movements): confirming a
  catalog import publishes `catalog.import.applied` (`import_id`, `actor_id`, `stock_counts`:
  presentation id → count) in the confirm's transaction. `inventory` subscribes by name and
  records one `count` movement per entry. It is an event because a direct call would make
  `catalog` and `inventory` depend on each other (inventory already depends on the catalog
  facade). The counts therefore lag the confirm by one relay cycle (about 2 s); delivery cannot
  be lost, and it commits with the handler's writes, so a committed delivery is not repeated.
- **Low-stock email to the owners** (an event feeding an email): every command that changes
  `on_hand` or a threshold calls `StockItem.track_low`, which sets the item's `low_alerted`
  flag when it drops to or below its threshold in units on hand (reservations cannot flap it)
  and re-arms it when it goes back above. The command publishes one
  `inventory.stock.went_low` event (`lines`: presentation id, `on_hand`, `threshold`) listing
  the items that just went low. An `inventory` subscriber sends one email per active owner,
  reading them through identity's `OwnerDirectory` facade and the labels through the catalog.
  Delivery is at-least-once, so a retry can repeat the email.
- **Stock reservations** (a counter under the item's row lock): each `inventory.stock_items`
  row keeps a `reserved` counter beside `on_hand`, changed under the same row lock, so a
  reserve and a movement on one presentation serialize. Available is
  `max(on_hand − reserved, 0)`; the storefront copy, the low-stock flag and the panel use it.
  Other modules (orders) call the `StockReservations` facade exported by `inventory`:
  reserve (all or nothing, with the caller's expiry), extend, commit (a `sale` movement per
  presentation, idempotent) and release. The `inventory_expire_reservations` cron (every
  minute) claims active reservations past their expiry with `FOR UPDATE SKIP LOCKED`, expires
  them and frees their units.
- Example flow _(to be built)_: Mercado Pago webhook → `payments` verifies signature and stores the
  notification idempotently → `payments.payment.approved` → `orders` moves the order to
  `paid` → `orders.order.paid` → `inventory` commits stock, `notifications` emails the customer.
- `arq` on Valkey runs heavy or deferred work (emails, expiring unpaid orders, carrier sync).
  Email never goes out inside an HTTP request.
- Modules declare periodic jobs as `ScheduledJob`s (`AppModule.jobs` in `shared/module.py`);
  the worker runs each one as an arq cron named `cron:<job name>`, with the service registry.
  A run that finds the previous run of the same job still going returns at once, so a long job
  never fills arq's slots and starves the outbox relay.
- The catalog import is a **database-backed job**: the upload stores the `.xlsx` file as a
  `pending` row of `catalog.imports`, and the `catalog_validate_imports` cron (every 5
  seconds) claims the oldest one with `FOR UPDATE SKIP LOCKED`, like the outbox relay. It
  validates outside any transaction (downloading and staging the image URLs in S3) and writes
  the report back in one short transaction. Image downloads not started within 20 minutes
  become cell issues, so a slow file still gets a full report before the 30-minute job
  timeout. `catalog_expire_imports` (every minute) fails
  validations interrupted for 30 minutes and expires unconfirmed ready imports, deleting their
  staged objects. Confirming a ready import runs in the request, in **one transaction, all or
  nothing**: each planned perfume goes through its aggregate against the live catalog, and if
  anything no longer fits (the catalog changed since the validation) the whole transaction
  rolls back and the confirm answers 409 `CATALOG_IMPORT_STALE` with the affected rows. Only
  rows are written (the photos are already staged at their final keys); objects of replaced
  photos, and the staged objects of a discarded import, are deleted after the commit.

## Errors

- **Expected** (validation, rules, not found, conflict) → `Result` with a `DomainError` that
  has a stable `code`. HTTP maps the category: not found → 404, conflict → 409, invalid value
  or broken rule → 422, unauthenticated → 401, forbidden → 403, rate limited (too many
  attempts) → 429.
- **Invalid HTTP input** → 422 `VALIDATION_ERROR` with `details.issues` per field.
- **No or unknown session** → 401 `AUTHENTICATION_REQUIRED`; **not allowed** → 403 `FORBIDDEN`.
- **Unknown route / method** → 404 `NOT_FOUND` / 405 `METHOD_NOT_ALLOWED`.
- **Unexpected** → logged in full; the client gets 500 `INTERNAL_ERROR` with no details.
- Clients always receive `{ code, message, details? }` and use `code` for translations.

## Health and operations

- `GET /api/v1/health/live` (process up) and `/api/v1/health/ready` (PostgreSQL `SELECT 1` and
  Valkey `PING`, 2 s timeout each; 503 naming the failing check).
- OpenAPI at `/api/v1/openapi.json` and docs at `/api/v1/docs`, disabled in production.
- Logs: structlog, console in development, JSON elsewhere, with the request id.
- One image (`apps/api/Dockerfile`): `runtime` (HTTP or worker by command) and `migrator`.

## Clients _(to be built)_

- One Angular app with SSR: storefront at `/` (catalog pages rendered on the server for SEO)
  and the admin panel at `/admin` (lazy-loaded, authenticated).
- It calls the API only through the generated client; no business rules in components.
