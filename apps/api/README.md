# fragancia-api

FastAPI modular monolith for La Fragancia Ideal. Architecture: [docs/architecture.md](../../docs/architecture.md).
Commands run from the repository root with `uv run just <recipe>`.

## Run

```bash
uv run just bootstrap        # once: services, apps/api/.env, migrations on both databases
uv run just api              # http://127.0.0.1:8100/api/v1/docs (Scalar, auto-reload)
uv run just worker           # background jobs: relays the outbox every 2 seconds
```

Admin endpoints (`/api/v1/admin/*`) need `Authorization: Bearer <ADMIN_DEV_TOKEN>` from
`apps/api/.env` — a development-only stand-in until the identity module exists.

## Layout

```
src/fragancia_api/
├── config.py          Settings (pydantic-settings) from the environment and apps/api/.env
├── container.py       Composition root: the only place that knows concrete adapters; MODULES
├── main/
│   ├── http.py        FastAPI factory (create_app) and build_app (used by tests)
│   └── worker.py      arq WorkerSettings (outbox relay cron)
├── shared/
│   ├── kernel/        Pure: Result, DomainError categories, AggregateRoot, DomainEvent, Money, ids
│   ├── application/   Ports: TransactionRunner, EventPublisher, EventSubscriptions, ActorResolver
│   ├── infrastructure/ Database + ContextVar session, SqlTransactionRunner, outbox + relay,
│   │                  dev token resolver, Valkey health, logging, shared MetaData
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
  `APIRouter`. A test checks that every `/api/v1/admin` operation requires the bearer scheme.
- **Errors** always have the shape `{code, message, details?}`; categories map to
  404/409/422; invalid input is 422 `VALIDATION_ERROR`; unexpected is 500 `INTERNAL_ERROR`.
- **Boundaries** are checked by `uv run just arch` (`.importlinter`). Do not work around a
  broken contract: explain why and propose an ADR.

## OpenAPI and the API reference

- `apps/api/openapi.json` is **committed**: every contract change shows up in the PR diff and
  phase 3 generates the web client from it. After changing a request/response model or a
  route, run `uv run just openapi`; `just check` fails while the file is stale.
- Operation ids are the route function names (`list_brands`) — keep them unique and readable.
  List responses use a concrete page class (`AdminBrandPage(Page[AdminBrand])`).
- `/api/v1/docs` serves **Scalar** outside production (script pinned in
  `shared/http/reference.py`). Click *Authorize*, choose *HTTPBearer* and paste
  `ADMIN_DEV_TOKEN` to call admin routes; the token is remembered in the browser.

## Settings

Add a field to `Settings` (or `DatabaseSettings` if migrations need it) in `config.py`, a line
to `.env.example`, and a validator if a bad value must stop startup. Production values come
from the environment.

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
