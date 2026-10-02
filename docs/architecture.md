# Architecture

> Everything below the infrastructure layer is **to be built** (phase 2: API, phase 3: web).
> This document is the target the plans for those phases must respect. Sections describing
> code that does not exist yet are marked _(to be built)_. Decisions are in [adr/](adr/).

## Overview

```
          ┌───────────────────────────────────────────────┐
          │ apps/web — Angular (SSR)            (phase 3) │   UI only. No business rules.
          │  storefront (/)        admin (/admin)         │
          └──────────────────────┬────────────────────────┘
                                 │ packages/api-client (generated from OpenAPI)
                                 ▼ HTTP /api/v1              ◀── Mercado Pago webhooks
┌─────────────────────────────────────────────────────────────────────────────┐
│ apps/api — FastAPI                                                (phase 2) │
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

## Modular monolith _(to be built)_

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

| Module          | Responsibility                                                                        |
| --------------- | ------------------------------------------------------------------------------------- |
| `catalog`       | Perfumes, brands, sizes (ml), prices, photos, availability mode: in stock / made to order |
| `inventory`     | Stock per size; reserve on checkout, release on cancellation, commit on payment       |
| `orders`        | Cart → order; the `Order` aggregate and its state machine                             |
| `payments`      | `PaymentGateway` port → Mercado Pago adapter (Checkout Pro); idempotent webhooks      |
| `identity`      | Admin login with opaque sessions; customers check out as guests in the first release  |
| `notifications` | Order emails first; WhatsApp (Cloud API) later; driven by events                      |
| `shipping`      | `CarrierGateway` port (quotes, labels, tracking) — later                              |

## Layers inside a module _(to be built)_

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
├── module.py        Wiring for the composition root
└── __init__.py      Public API of the module
```

Dependency rule: `http → application → domain ← infrastructure`. Enforced with import-linter
(layers contract per module + independence contract between modules).

## Lightweight CQRS _(to be built)_

|                | Command (write)                        | Query (read)                               |
| -------------- | -------------------------------------- | ------------------------------------------ |
| Goes through   | Domain aggregate + `XxxRepository`     | `XxxQueries` port                          |
| Business rules | Yes, in the aggregate                  | Nothing to protect                         |
| Returns        | `Result[id \| None, DomainError]`      | Pydantic response model                    |
| Optimized for  | Consistency                            | Exact selects, joins, views                |

Same database for both sides. Repositories have no "for a screen" methods; that is what
`XxxQueries` is for.

## Contracts _(to be built)_

Pydantic v2 request/response models in each module's `http/` are the single source of truth
for the HTTP API. FastAPI generates the OpenAPI document; `packages/api-client` is generated
from it for Angular. No hand-written duplicate types in the frontend.

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

## Events, outbox and jobs _(to be built)_

- Domain events are written to an **outbox table in the same transaction** as the change
  ([ADR 0006](adr/0006-outbox-from-day-one.md)). The worker relays them to subscribers with
  at-least-once delivery; subscribers are idempotent.
- Example flow: Mercado Pago webhook → `payments` verifies signature and stores the
  notification idempotently → `payments.payment.approved` → `orders` moves the order to
  `paid` → `orders.order.paid` → `inventory` commits stock, `notifications` emails the customer.
- `arq` on Valkey runs heavy or deferred work (emails, expiring unpaid orders, carrier sync).
  Email never goes out inside an HTTP request.

## Errors _(to be built)_

- **Expected** (validation, rules, not found, conflict) → `Result` with a `DomainError` that
  has a stable `code`. HTTP maps the category: not found → 404, conflict → 409, invalid value
  or broken rule → 422, unauthenticated → 401, forbidden → 403.
- **Invalid HTTP input** → 422 from Pydantic, normalized to the same error shape.
- **Unexpected** → logged in full; the client gets 500 `INTERNAL_ERROR` with no details.
- Clients always receive `{ code, message, details? }` and use `code` for translations.

## Clients _(to be built)_

- One Angular app with SSR: storefront at `/` (catalog pages rendered on the server for SEO)
  and the admin panel at `/admin` (lazy-loaded, authenticated).
- It calls the API only through the generated client; no business rules in components.
