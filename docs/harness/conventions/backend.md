# Backend conventions (apps/api)

This is the checklist version. The **why** and full detail live in
[apps/api/README.md](../../../apps/api/README.md), [docs/architecture.md](../../architecture.md)
and the ADRs in [docs/adr/](../../adr/). Recipes (plain markdown, usable by any agent; the
Claude skills of the same name load them):

| Task                      | Recipe                                                  |
| ------------------------- | ------------------------------------------------------- |
| New business module       | [docs/recipes/new-module.md](../../recipes/new-module.md)     |
| New command or query      | [docs/recipes/new-use-case.md](../../recipes/new-use-case.md) |
| Schema change / migration | [docs/recipes/db-change.md](../../recipes/db-change.md)       |

Reference implementation: `apps/api/src/fragancia_api/modules/catalog/`. Imitate it by name.

## Non-negotiables (enforced by `uv run just arch` — import-linter — where marked ⚙)

- ⚙ Layers: `domain/` (pure) ← `application/` (use cases + ports) ← `infrastructure/`
  (adapters) · `http/`. The domain imports nothing outside itself and `shared.kernel`; the
  kernel uses only the standard library.
- ⚙ Modules are independent: another module is used only through its `__init__.py`, and only
  from `infrastructure/` (an adapter implementing a port the consumer owns), with that exact
  import allowed in `.importlinter`.
- ⚙ SQLAlchemy only in `infrastructure/`; only `container.py` and `main/` know the
  composition root and create adapters.
- CQRS-lite: commands → aggregate + `XxxRepository` inside `TransactionRunner.run(...)` →
  `Result`; queries → `XxxQueries` port (`Database.reader()`) → response model.
- Contracts first: request/response models in the module's `contracts.py` (Pydantic v2); after
  any contract or route change run `uv run just openapi` and commit `apps/api/openapi.json`.
  Route function names are the operation ids (unique, readable).
- Declared access: routes live in `public_router()` or `admin_router()`, never a bare
  `APIRouter`.
- Expected errors: `Err(DomainError)` subclass (`NotFoundError`, `ConflictError`,
  `InvalidValueError`, …) with a stable SCREAMING_SNAKE `code` `<MODULE>_<THING>_<PROBLEM>`;
  routers call `unwrap(result)`. Raise only for the unexpected (ADR 0008).
- Events that must not be lost go through the outbox
  (`EventPublisher.publish(aggregate.pull_events())` inside the same `run`); subscribers are
  idempotent (ADR 0006).
- Money: `Money` (integer cents, `MXN`), never `float`. Datetimes UTC, from the `Clock` port;
  ids from `new_id()` (UUID v7).
- Persistence: SQLAlchemy Core tables + explicit mappers (ADR 0007); one PostgreSQL schema per
  module; no cross-schema foreign keys; new Alembic migrations only, reversible.
- Wiring: use cases registered in the module's `module.py` (`services.add(...)`); the module
  appended to `MODULES` in `container.py` (hot file, append-only).
- Settings: a field in `config.py`, a line in `apps/api/.env.example`, a validator when a bad
  value must stop startup.
- mypy strict, ruff; identifiers, comments and docs in English; customer-facing messages in
  Spanish.
