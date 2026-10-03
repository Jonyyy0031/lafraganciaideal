---
name: db-change
description: Change the database schema safely (SQLAlchemy Core tables + Alembic on PostgreSQL) — add tables, columns, indexes or a new module schema, and generate the migration. Use whenever an infrastructure/tables.py or apps/api/migrations is touched.
---

# Change the database schema

Load and follow, in this order:

1. `docs/recipes/db-change.md` — the recipe.
2. `docs/adr/0007-sqlalchemy-core-and-mappers.md` — Core tables + explicit mappers.
3. The reference: `apps/api/src/fragancia_api/modules/catalog/infrastructure/tables.py` and the
   catalog migration `apps/api/migrations/versions/0002_catalog_brands.py`.

Rules that always apply (enforced by the guard hooks where possible):

- **Never** edit an existing migration: always a new one (`uv run just db-revision "msg"`),
  then review the generated file by hand — unexpected drops are a stop.
- One PostgreSQL schema per module; no foreign keys across modules (reference by id).
- Apply with `uv run just db-migrate` and `uv run just db-migrate --test`; never
  `alembic downgrade` the development database; `just db-reset` only with `--test`.
- A migration that drops or rewrites data needs the user's explicit approval in the plan.
- Finish with `uv run just check` and `uv run just test-integration`.
