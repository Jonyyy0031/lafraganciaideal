---
status: done
module: platform
depends_on: [platform-infraestructura/001]
---

# 001 — API platform

## Context

Phase 1 left the repo with local services (`infra/docker/compose.yaml`), an idempotent
bootstrap (`scripts/bootstrap.py`), `just` recipes (`justfile`) and CI
(`.github/workflows/ci.yml`). `apps/` is empty. The target architecture is described in
`docs/architecture.md` (modules, hexagonal layers, lightweight CQRS, outbox) and decided in
ADRs 0002, 0003 and 0006. Decisions for this initiative: [README.md](README.md).

The shape imitates `~/codes/web-rh/apps/api` translated to Python:

- `TransactionRunner` + "repositories use the active transaction without receiving it"
  (web-rh `docs/architecture.md` → Persistence, AsyncLocalStorage) → here a `ContextVar`.
- Error categories mapped to HTTP status in one place (web-rh `docs/architecture.md` → Errors).
- Composition root as the only place that knows concrete classes (web-rh `src/container.ts`).
- Same image, two entrypoints: HTTP and worker (web-rh `src/main/`).

## Out of scope

- Any business module (plan 002 creates `catalog`).
- Real authentication (identity initiative); only the development token adapter.
- Production deployment (platform-infraestructura/002), metrics/tracing.
- Outbox subscribers other than test ones.

## Dependencies

- platform-infraestructura/001: PostgreSQL on 5433 with `fragancia` / `fragancia_test`,
  Valkey on 6380, `uv run just` recipes, CI workflow.

## Steps

1. **uv workspace and API project**
   - Files: `pyproject.toml` (modify), `uv.lock` (modify), `apps/api/pyproject.toml` (create),
     `apps/api/src/fragancia_api/__init__.py` (create), `apps/.gitkeep` (delete)
   - Do: root becomes a workspace (`members = ["apps/api"]`) and depends on `fragancia-api`
     so one `.venv` has everything. `apps/api` runtime deps: fastapi, uvicorn[standard],
     sqlalchemy[asyncio], alembic, psycopg[binary], pydantic-settings, arq, structlog. Dev tools
     (root dev group): mypy, import-linter, pytest-asyncio, httpx. Python 3.14.
   - Observable result: `uv sync` installs the API package; `uv run python -c "import fragancia_api"` works.

2. **Configuration**
   - Files: `apps/api/src/fragancia_api/config.py` (create), `apps/api/.env.example` (create),
     `scripts/bootstrap.py` (modify), `scripts/infra.py` (modify), `scripts/test_bootstrap.py` (modify)
   - Do: `Settings` (pydantic-settings) reading `apps/api/.env` and the environment:
     `APP_ENV` (development|test|production), `API_PORT=8100`, `DATABASE_URL`,
     `DATABASE_URL_TEST`, `VALKEY_URL`, `CORS_ORIGINS`, `LOG_LEVEL`, `ADMIN_DEV_TOKEN`.
     Validation: production refuses `ADMIN_DEV_TOKEN`; `DATABASE_URL_TEST` must point to a
     database whose name ends in `_test`. Bootstrap also creates `apps/api/.env` (never
     overwrites) and runs migrations on both databases.
   - Observable result: missing/invalid settings fail at startup with a clear message.

3. **Shared kernel (pure)**
   - Files: `apps/api/src/fragancia_api/shared/kernel/__init__.py` (create), `.../kernel/result.py`
     (create), `.../kernel/errors.py` (create), `.../kernel/entity.py` (create),
     `.../kernel/events.py` (create), `.../kernel/money.py` (create), `.../kernel/ids.py` (create)
   - Do: `Ok[T]` / `Err[E]` and `type Result[T, E] = Ok[T] | Err[E]`; `DomainError(code,
     message, details)` with categories `NotFoundError`, `ConflictError`, `InvalidValueError`,
     `BusinessRuleViolationError`; `AggregateRoot` that records and pulls `DomainEvent`s
     (frozen dataclasses with a class-level `name`); `Money` in integer cents, MXN; `new_id()`
     returning UUIDv7.
   - Observable result: no third-party imports in `shared/kernel` (enforced by step 9).

4. **Database, transactions and migrations**
   - Files: `apps/api/src/fragancia_api/shared/application/transactions.py` (create),
     `apps/api/src/fragancia_api/shared/infrastructure/database.py` (create),
     `apps/api/src/fragancia_api/shared/infrastructure/tables.py` (create),
     `apps/api/alembic.ini` (create), `apps/api/migrations/env.py` (create),
     `apps/api/migrations/script.py.mako` (create),
     `apps/api/migrations/versions/0001_platform_outbox.py` (create)
   - Do: `TransactionRunner` port (`async with tx.transaction(): ...`). `Database` owns the
     async engine and session factory; the active session lives in a `ContextVar`;
     `Database.session` returns it or raises if there is none; `Database.reader()` opens a
     read-only session for queries. One `MetaData` with naming conventions shared by all
     modules' Core tables. Alembic: async `env.py`, `include_schemas=True`, version table in
     schema `platform`, URL from `Settings`.
   - Observable result: `uv run just db-migrate` creates schema `platform` and its tables.

5. **Outbox and event publishing**
   - Files: `apps/api/src/fragancia_api/shared/application/events.py` (create),
     `apps/api/src/fragancia_api/shared/infrastructure/outbox.py` (create)
   - Do: `EventPublisher` port; `OutboxEventPublisher` inserts one row per event in
     `platform.outbox` (id, name, payload jsonb, occurred_at, available_at, published_at,
     attempts, last_error) **using the active transaction** (fails without one). `EventBus`
     registry of subscribers by event name. `OutboxRelay.relay_batch()`: selects due rows with
     `FOR UPDATE SKIP LOCKED`, delivers each in its own transaction to its subscribers, marks
     it published, or records the error and backs off (`available_at`) on failure.
   - Observable result: an event written inside a rolled-back transaction never reaches a
     subscriber; a failing subscriber is retried later.

6. **HTTP foundation: errors, access, health, docs**
   - Files: `apps/api/src/fragancia_api/shared/http/__init__.py` (create),
     `.../shared/http/errors.py` (create), `.../shared/http/access.py` (create),
     `.../shared/http/health.py` (create), `.../shared/http/middleware.py` (create),
     `apps/api/src/fragancia_api/shared/application/actor.py` (create),
     `apps/api/src/fragancia_api/shared/infrastructure/dev_token_actor_resolver.py` (create),
     `apps/api/src/fragancia_api/shared/infrastructure/logging.py` (create)
   - Do: error body `{code, message, details?}` (`ErrorResponse`); `unwrap(result)` raises a
     `DomainErrorRaised` mapped by category (404/409/422); Pydantic input errors → 422
     `VALIDATION_ERROR` with per-field issues; unexpected → 500 `INTERNAL_ERROR`, logged with
     the request id. Access: `Actor` + `ActorResolver` port; `require_admin` dependency
     (Bearer token → 401 `AUTHENTICATION_REQUIRED` if missing/unknown, 403 `FORBIDDEN` if not
     admin); `public_router()` / `admin_router()` helpers so every route declares its access.
     `DevTokenActorResolver` compares with `hmac.compare_digest`. Health:
     `GET /api/v1/health/live` and `/api/v1/health/ready` (DB `SELECT 1` + Valkey `PING`;
     503 with the failing check). Request id middleware (`X-Request-ID`), structlog (console in
     development, JSON otherwise). OpenAPI at `/api/v1/openapi.json`, docs at `/api/v1/docs`,
     both disabled in production. CORS from settings.
   - Observable result: `curl :8100/api/v1/health/ready` → 200 `{"status":"ok", ...}`.

7. **Composition root and entrypoints**
   - Files: `apps/api/src/fragancia_api/container.py` (create),
     `apps/api/src/fragancia_api/main/__init__.py` (create),
     `apps/api/src/fragancia_api/main/http.py` (create),
     `apps/api/src/fragancia_api/main/worker.py` (create),
     `apps/api/src/fragancia_api/shared/module.py` (create)
   - Do: `AppModule` protocol (routers, tables, subscriptions, wiring). `build_container
     (settings)` is the only place creating adapters. `create_app()` (FastAPI factory, lifespan
     disposes the engine). `WorkerSettings` for arq with a cron job that runs
     `OutboxRelay.relay_batch()` every 2 seconds.
   - Observable result: `uv run just api` serves on 8100; `uv run just worker` starts and
     relays outbox rows.

8. **Recipes**
   - Files: `justfile` (modify)
   - Do: `api` (uvicorn `--factory --reload`, port from `apps/api/.env`), `worker`,
     `db-migrate [--test]`, `db-revision name`, `typecheck` (mypy), `arch` (lint-imports),
     `test` (unit: tooling + API), `test-integration` (API integration, requires `up`);
     `check` adds typecheck and arch.
   - Observable result: `uv run just --list` shows them; `uv run just check` passes.

9. **Architecture rules**
   - Files: `apps/api/.importlinter` (create)
   - Do: contracts — `shared.kernel` imports no third-party package and nothing else from the
     app; shared layers `http | infrastructure` → `application` → `kernel`; module layers
     (added per module from plan 002); modules independent of each other; `main` and
     `container` are the only importers of infrastructure adapters across modules.
   - Observable result: `uv run just arch` passes; a deliberate violation fails it.

10. **Tests**
    - Files: `apps/api/tests/conftest.py` (create), `apps/api/tests/unit/` (create),
      `apps/api/tests/integration/` (create), `pyproject.toml` (modify)
    - Do: pytest-asyncio auto mode; `integration` marker deselected by default. Unit: Result,
      errors, Money, ids, settings validation, error mapping, access (401/403/200), health with
      fake checks, container resolves. Integration (against `fragancia_test`, migrations
      applied once per session): transaction commit/rollback, outbox write + relay + retry +
      rollback discards events, health ready with real DB/Valkey.
    - Observable result: `uv run just test` and `uv run just test-integration` pass.

11. **Docker image**
    - Files: `apps/api/Dockerfile` (create), `.dockerignore` (create)
    - Do: multi-stage on `python:3.14-slim` pinned by digest, uv copied from its official image
      pinned by digest; `uv sync --frozen --no-dev --package fragancia-api`; non-root user;
      targets `runtime` (default CMD uvicorn on 8100; worker = same image with
      `arq fragancia_api.main.worker.WorkerSettings`) and `migrator` (`alembic upgrade head`).
    - Observable result: `docker build -f apps/api/Dockerfile --target runtime .` succeeds and
      the container answers `/api/v1/health/live`.

12. **CI**
    - Files: `.github/workflows/ci.yml` (modify), `.github/workflows/README.md` (modify)
    - Do: `quality` also runs typecheck and arch. New job `api-integration` with an ephemeral
      PostgreSQL (same digest) and Valkey service: migrations + `pytest -m integration`. New
      job `docker` building `runtime` and `migrator` (no push).
    - Observable result: all jobs green on push.

13. **Documentation**
    - Files: `docs/architecture.md` (modify), `AGENTS.md` (modify), `README.md` (modify),
      `docs/adr/0007-sqlalchemy-core-and-mappers.md` (create),
      `docs/adr/0008-result-type-and-manual-composition-root.md` (create),
      `docs/adr/README.md` (modify), `apps/api/README.md` (create)
    - Do: mark what now exists, document layout, commands, how to add settings/migrations,
      declared access, outbox semantics.
    - Observable result: a new agent can run, test and extend the API from the docs alone.

## Acceptance criteria

- [ ] After `uv run just bootstrap`, `apps/api/.env` exists and both databases are migrated.
- [ ] `uv run just api` → `GET http://127.0.0.1:8100/api/v1/health/live` 200; `/ready` 200 with
      `database` and `valkey` checks `ok`; with Valkey stopped `/ready` → 503 naming `valkey`.
- [ ] `/api/v1/docs` serves the reference in development; with `APP_ENV=production` it is 404.
- [ ] Unknown route → 404 with `{code, message}`; unexpected exception → 500 `INTERNAL_ERROR`
      without details and with `X-Request-ID` in the response.
- [ ] Starting with `APP_ENV=production` and `ADMIN_DEV_TOKEN` set fails with a clear error.
- [ ] Outbox: event published in a committed transaction is delivered by the relay; in a
      rolled-back one it is not; a failing subscriber increments `attempts` and is retried.
- [ ] `uv run just worker` starts and relays.
- [ ] `uv run just check` (lint, typecheck, arch, unit tests) and `uv run just test-integration` pass.
- [ ] The Docker image builds and its container answers `/api/v1/health/live`.
- [ ] CI is green on push, including `api-integration` and `docker`.

## Test layers required

| Layer       | Applies | Focus                                                         |
| ----------- | ------- | ------------------------------------------------------------- |
| domain      | yes     | kernel: Result, errors, Money, ids, aggregate events          |
| application | yes     | settings validation, container wiring                         |
| http        | yes     | error mapping, access dependency, health, docs toggling       |
| integration | yes     | transactions, outbox relay, migrations, readiness             |
| e2e         | no      | no web app yet                                                |

## Deviations

1. **`TransactionRunner.run(work)` instead of `async with tx.transaction()`** — the plan's step 4
   described a context manager; `run` takes the work and commits on `Ok`, rolls back on `Err`
   or exceptions, so an `Err` can never commit half a change. Nested runs join the outer one.
2. **`DatabaseSettings` split from `Settings`** — the `migrator` image only has the database
   URL; Alembic's `env.py` reads `DatabaseSettings`, which `Settings` extends.
3. **Additions needed by plan 002**: `Clock` port (`shared/application/clock.py`) +
   `SystemClock`; in-memory platform fakes (`shared/infrastructure/in_memory.py`);
   `shared/contracts` with the generic `Page[T]`, placed next to the kernel in the shared
   layers contract. `Platform` gained `clock`.
4. **Declared-access check uses the OpenAPI document** — FastAPI 0.142 stores included routers
   as private `_IncludedRouter` objects; `tests/support.py::assert_admin_routes_are_protected`
   checks that every `/api/v1/admin` operation requires the `HTTPBearer` scheme instead.
5. **Dockerfile without BuildKit cache mounts** — the dev machine's Docker has no `buildx`
   plugin; plain layers keep dependency caching and the image builds everywhere.
6. **Worker logs** — arq's handler duplicated lines through the root logger and logged every
   2-second cron run; propagation is off for `arq` and a filter hides only the outbox cron
   start/finish lines (failures still show).
7. **mypy also checks `apps/api/migrations`**; `apps/api/README.md` is the package readme.
8. **`scripts/infra.py` / `scripts/test_bootstrap.py` were not modified** — `ensure_env_file`
   already covered `apps/api/.env`.
9. **Branch history** — a pre-staged index made the first commits lump files together; the
   unpublished branch was re-committed with `git reset --soft` (no change lost).

## Verification

Run on 2026-10-02 against the local services (web-rh running in parallel).

| Criterion | Result | Evidence |
| --- | --- | --- |
| Bootstrap creates `apps/api/.env` and migrates both databases | ✔ | `= apps/api/.env already exists` on rerun; `Running upgrade -> 0001` on dev and test |
| `/health/live` 200, `/ready` 200 with both checks, 503 naming `valkey` when stopped | ✔ | live `{"status":"ok"}`; ready `{"database":"ok","valkey":"ok"}`; Valkey stopped → 503 `{"valkey":"error"}`; restarted → 200 |
| Docs in development, 404 in production | ✔ | `/api/v1/docs` 200 with `just api`; 404 in the `runtime` image and in `test_production_app_hides_the_docs` |
| 404 / 500 shapes and `X-Request-ID` | ✔ | `/api/v1/nope` → `{"code":"NOT_FOUND",...}`; unit test: 500 `INTERNAL_ERROR` without the exception text, header present |
| Production + `ADMIN_DEV_TOKEN` refuses to start | ✔ | `APP_ENV=production` → `ADMIN_DEV_TOKEN is for development only; unset it in production` |
| Outbox: committed delivered, rolled back not, failures retried | ✔ | integration tests (9) incl. backoff and `attempts`; live: event inserted in dev DB → worker marked it published |
| `just worker` starts and relays | ✔ | `Starting worker for 1 functions: cron:relay_outbox`, `worker.started`; outbox row published |
| `just check` and `just test-integration` | ✔ | ruff, all hooks, mypy (77 files), import-linter 7 contracts, 95 unit; 15 integration |
| import-linter catches violations | ✔ | temporary `import sqlalchemy` in the kernel → `kernel-is-pure BROKEN` |
| Docker image builds and answers | ✔ | `runtime` and `migrator` built (330 MB); container live/ready 200, uid 10001; worker command starts with JSON logs; migrator ran |
| CI green on push | **PENDING** | waiting for the user's choice: push to `main` or PR |
