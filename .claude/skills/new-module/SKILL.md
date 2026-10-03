---
name: new-module
description: Create a new business module in apps/api (e.g. inventory, orders, payments, shipping) following the repo's hexagonal layers + lightweight CQRS, copying the catalog reference module by name — contracts, domain, use cases, SQL adapters, routes, wiring, migration and tests. Use when asked to "create the X module" or add a new business domain.
---

# Create a business module

Load and follow, in this order:

1. `docs/recipes/new-module.md` — the recipe (what to create, what to copy, done criteria).
2. `apps/api/README.md` — the API conventions.
3. The reference module `apps/api/src/fragancia_api/modules/catalog/` — open the files the
   recipe names and replicate their shape by name. Do not invent new structure.

Rules that always apply:

- Only inside an approved plan that lists every file (`docs/harness/workflow.md`); the module
  must be in `docs/modules.json`.
- Business rules (prices, stock, shipping, payments) are asked, never invented.
- Persistence: follow the `db-change` skill (own PostgreSQL schema, new Alembic migration, no
  cross-module foreign keys).
- Hot files (`container.py`, `docs/modules.json`, `apps/api/.importlinter`) are append-only.
- Finish with `uv run just check` and `uv run just test-integration`; after a contract change
  run `uv run just openapi`.
