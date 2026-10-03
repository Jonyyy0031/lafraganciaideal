---
name: new-use-case
description: Add a use case (a state-changing command or a read-only query) to an existing API module end to end — contract, use case, adapters, route, wiring and tests — copying the catalog reference module. Use when asked for a new endpoint or operation in a module that already exists.
---

# Add a use case

Load and follow, in this order:

1. `docs/recipes/new-use-case.md` — the recipe (command vs. query, steps, tests).
2. `apps/api/README.md` — the API conventions.
3. The reference implementations in `apps/api/src/fragancia_api/modules/catalog/`:
   `application/commands/create_brand.py` (command), `application/queries/list_brands.py`
   (query), `http/router.py`, `module.py`, and their tests in `apps/api/tests/unit/catalog/`.

Decide first: **does it change state?** Yes → command (aggregate → repository inside
`TransactionRunner.run(...)`, returns `Result`). No → query (`XxxQueries` port, returns
response models).

Rules that always apply:

- Only inside an approved plan (or the fast lane for a diagnosed fix that touches no contract).
- Routes live in `public_router()` or `admin_router()`, never a bare `APIRouter`.
- A new repository method goes to the port, the SQL adapter and the in-memory adapter.
- Finish with `uv run just openapi` (contract changed), `uv run just check`, and
  `uv run just test-integration` when SQL adapters changed.
