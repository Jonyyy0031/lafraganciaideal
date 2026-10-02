# Recipe — a database change

Model: `modules/catalog/infrastructure/tables.py` + `apps/api/migrations/versions/0002_catalog_brands.py`.

1. **Edit the Core table** in `modules/<m>/infrastructure/tables.py` (shared `metadata`,
   `schema="<m>"`). Constraint names come from the naming convention (`uq_<table>_<column>`,
   `ix_…`, `fk_…`, `pk_<table>`); reference them by name in adapters
   (e.g. `BRAND_SLUG_UNIQUE`).
2. **Generate** the migration (the development database must be at head):

   ```bash
   uv run just db-revision "inventory stock levels"   # → NNNN_inventory_stock_levels.py
   ```

3. **Review it by hand.** Autogenerate does not create schemas: a new module's first
   migration starts with `op.execute('CREATE SCHEMA IF NOT EXISTS "<m>"')` and its downgrade
   ends with `DROP SCHEMA`. Check types, nullability, server defaults and data migrations;
   format it like the existing migrations.
4. **Apply and check**:

   ```bash
   uv run just db-migrate            # development
   uv run just db-migrate --test     # test (test-integration also does it)
   cd apps/api && uv run alembic check   # tables and migrations match
   ```

5. **Reversible**: `uv run alembic -x test=true downgrade -1` then `upgrade head` must work;
   CI runs downgrade base → upgrade head on every push.

Rules:

- No foreign keys across schemas (modules reference each other by id only).
- Never edit a migration that has reached `main`; write a new one.
- Destructive changes (drop column/table) need their own plan and a data decision.
