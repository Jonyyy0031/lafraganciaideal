# AGENTS.md — La Fragancia Ideal

Operating guide for AI agents (and humans) working in this repository. It is the **source of
truth** for how work is done here. Details: [docs/architecture.md](docs/architecture.md),
[CONTRIBUTING.md](CONTRIBUTING.md) and the decisions in [docs/adr/](docs/adr/).

## What this is

Online perfume store + back office for a business in Mexico (catalog, cart, checkout with
Mercado Pago, order lifecycle, admin panel). Built in phases:

1. **Infrastructure** — repo, local services, tooling, CI. ← _current_
2. **API** — FastAPI modular monolith.
3. **Web** — Angular storefront and admin, starting from a visual design.

Do not start work belonging to a later phase unless a plan for it is approved.

## The rules that govern everything

1. **Behavior changes are plan-driven.** Brainstorm with the user → write a plan in
   `plans/<module>-<topic>/NNN-<slug>.md` (format: [plans/\_TEMPLATE.md](plans/_TEMPLATE.md);
   one initiative per directory with its `README.md` of decisions) → the user approves → set
   `status: implementing` → implement → verify → `status: done`. No separate spec documents.
   Record anything that differs from the plan under **Deviations**, and the evidence under
   **Verification**, in the plan itself.
2. **Out-of-scope discoveries are written down, not fixed in passing**: add a note to
   [plans/findings/](plans/findings/README.md).
3. **Destructive actions are forbidden** unless the user explicitly asks for that exact action:
   - `docker compose down -v`, `docker volume rm`, `docker system prune` (they delete all data);
   - `git reset --hard`, `git push --force`, `git clean`, `git checkout -- <path>`,
     `git stash` on someone else's work, rewriting published history;
   - deleting or reverting files outside the current plan's file list;
   - `just db-reset` on the development database.
4. **All repository documentation is written in English** (README, AGENTS, docs, ADRs, plans,
   code comments, commit messages). Customer-facing UI copy is Spanish.
5. **Commits follow the convention** in [CONTRIBUTING.md](CONTRIBUTING.md#commits), enforced by
   the `commit-msg` hook and CI. **No AI attribution** (no `Co-Authored-By`, no "Generated with").

## ⚠️ Before writing code

- **Your training data may be outdated** for this stack (Python 3.14, FastAPI, Pydantic v2,
  SQLAlchemy 2, Angular, PostgreSQL 18, Valkey 9, uv, just). Read the documentation of the
  installed version before using an API you are not sure about.
- **Imitate existing patterns.** Once a reference module exists, new code copies its shape.
  Until then, `~/codes/web-rh` (same author, TypeScript) shows the intended architecture.
- **Run `just check` before declaring anything done**, and say plainly what was not verified.

## Repo map

```
apps/            api (phase 2) and web (phase 3) — empty for now
packages/        api-client generated from OpenAPI (phase 3) — empty for now
infra/docker/    compose.yaml for local development (PostgreSQL, Valkey, RustFS S3, Mailpit)
scripts/         bootstrap.py, infra.py, commits.py + their tests (repo tooling, Python)
docs/            architecture.md, adr/, modules.json (module registry = valid commit scopes)
plans/           plans and initiatives; _TEMPLATE.md, _INITIATIVE.md, findings/
justfile         every command; run `just` to list them
pyproject.toml   repo tooling project (not the API)
```

## Commands

| Task                                 | Command                                   |
| ------------------------------------ | ----------------------------------------- |
| Full local setup (idempotent)        | `uv run just bootstrap`                   |
| Start / stop services (keeps data)   | `just up` / `just down`                   |
| Status / logs                        | `just ps` / `just logs [service]`         |
| SQL shell                            | `just psql [-d fragancia_test]`           |
| Recreate a database (asks to confirm)| `just db-reset [--test]`                  |
| **Full verification (mandatory)**    | `just check`                              |
| Check a range of commit messages     | `uv run scripts/commits.py --range a..b`  |

`just` comes from the uv environment (`rust-just` dev dependency): run recipes as
`uv run just <recipe>` or with `.venv` activated. Python dependencies: `uv add --dev <pkg>`
(never `pip install`). Tooling runs with `uv run`.

## Target architecture rules (from phase 2 on)

Summarized here so plans for the API respect them from day one; details and diagrams in
[docs/architecture.md](docs/architecture.md).

1. **Modular monolith**: one deployable, modules with strict boundaries (own PostgreSQL schema,
   public API in the module's `__init__.py`, no cross-module foreign keys or table reads).
   Modules are registered in [docs/modules.json](docs/modules.json).
2. **Hexagonal layers per module**: `domain` (pure, no IO) ← `application` (use cases + ports)
   ← `infrastructure` (adapters) · `http` (routers). Enforced with import-linter.
3. **Lightweight CQRS**: commands go through aggregates and repositories and return a result;
   queries go through `XxxQueries` ports and return response models directly.
4. **Contracts first**: Pydantic request/response models are the source of truth; the OpenAPI
   document is generated from them and the frontend client is generated from OpenAPI.
5. **Events that must not be lost use a transactional outbox** (payments, order state changes).
6. **The web app has no business logic** and never touches the database.
