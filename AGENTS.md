# AGENTS.md — La Fragancia Ideal

Operating guide for AI agents (and humans) working in this repo. It is the **source of truth**
for conventions; the process lives in [docs/harness/](docs/harness/HARNESS.md), the code
architecture in [docs/architecture.md](docs/architecture.md) and
[apps/api/README.md](apps/api/README.md), and the decisions in [docs/adr/](docs/adr/).

## What this is

Online perfume store + back office for a business in Mexico (catalog, cart, checkout with
Mercado Pago, order lifecycle, admin panel). Built in phases:

1. **Infrastructure** — repo, local services, tooling, CI. ✔
2. **API** — FastAPI modular monolith. ← _current_
3. **Web** — Angular storefront and admin, starting from a visual design.

Do not start work belonging to a later phase unless a plan for it is approved.

## The three rules that govern everything

Read [docs/harness/HARNESS.md](docs/harness/HARNESS.md) and
[docs/harness/workflow.md](docs/harness/workflow.md) first.

1. **Work that changes behavior is plan-driven**: brainstorm with the user → plan in
   `plans/<module>-<topic>/NNN-<slug>.md` (one initiative per directory, with its `README.md`
   of decisions; no separate spec) → the user approves → implement → tests → review → verify
   → the user marks it done. The `status:` in the frontmatter is the orchestration state; the
   evidence of each phase is written IN the plan. Never implement against code that a
   non-`done` plan only promises (`depends_on`). Discoveries outside the scope go to
   `plans/findings/`, never fixed in passing. Full semantics:
   [conventions/plans.md](docs/harness/conventions/plans.md). **Exception**: small diagnosed
   fixes that meet EVERY fast-lane criterion (skill `fix`) — no plan, but never without
   conventions, a regression test and verification in the running app.
2. **New code follows the architecture and the conventions** below and in
   `docs/harness/conventions/`. The `catalog` module is the reference; imitate it by name.
3. **Destructive actions are forbidden** (see "Forbidden actions"): nothing outside the plan's
   file list is deleted, reverted or "cleaned up". The hooks in `.claude/hooks/` block the
   known forms mechanically.

### Subagent dispatch (Claude Code / Codex)

With a plan in `approved`/`implementing`, `testing`, `review` or `verify`, the routing table
in [workflow.md](docs/harness/workflow.md) names the subagent (`implementer`, `tester`,
`reviewer`, `verifier`) and the model according to `min_implementer`.

- **With the user present (default): ASK before dispatching** with the question tool ("plan
  004 is in `testing`: dispatch `tester` or do it here?"). Never silently.
- **Unattended**: dispatch without asking only when the user explicitly handed over a batch
  ("continue the plans without asking me", a loop).
- Always in the main chat: bug diagnosis, anything that needs conversation context, and
  writing plans (the architect talks with the user).
- **Role purity also inline**: whoever writes tests does not fix product code; a fix that grows
  beyond the fast lane stops and goes through a plan.

`uv run just plans-status` shows what needs attention across all plans.

## ⚠️ Before writing code

- **Your training data may be outdated** for this stack (Python 3.14, FastAPI, Pydantic v2,
  SQLAlchemy 2, Alembic, arq, Angular, PostgreSQL 18, Valkey 9, uv, just). Read the
  documentation of the installed version before using an API you are not sure about.
- **Imitate the reference module.** `catalog` (`apps/api/src/fragancia_api/modules/catalog`)
  is the canonical pattern. Recipes: [new module](docs/recipes/new-module.md),
  [new use case](docs/recipes/new-use-case.md), [database change](docs/recipes/db-change.md).
  Read [apps/api/README.md](apps/api/README.md) before touching `apps/api`.

## Repo map

```
apps/api/        FastAPI modular monolith (+ arq worker); see apps/api/README.md
apps/web/        Angular (phase 3) — not created yet
infra/docker/    compose.yaml for local development (PostgreSQL, Valkey, RustFS S3, Mailpit)
scripts/         bootstrap, commit convention, plans/ (lint, status, scope), harness/ (adapters, hook tests)
docs/            architecture.md, adr/, recipes/, modules.json, harness/ (process: roles, workflow)
plans/           plans and initiatives; _TEMPLATE.md, _INITIATIVE.md, _FINDING.md, findings/
.claude/ .codex/ .agents/   hooks, settings and GENERATED subagents/skills (never hand-edit)
justfile         every command; run `uv run just` to list them
pyproject.toml   uv workspace root: repo tooling + dev tools; apps/api is a member
```

## Commands

| Task                                    | Command                                                    |
| --------------------------------------- | ---------------------------------------------------------- |
| Full local setup (idempotent)           | `uv run just bootstrap`                                    |
| Start / stop services (keeps data)      | `uv run just up` / `uv run just down`                      |
| Run the API / the worker                | `uv run just api` (port 8100) / `uv run just worker`        |
| **Full verification (mandatory)**       | `uv run just check`                                        |
| Unit / integration / hook tests         | `uv run just test` / `test-integration` / `test-harness` |
| Types / architecture rules              | `uv run just typecheck` / `uv run just arch`               |
| Regenerate the committed OpenAPI        | `uv run just openapi` (after any contract change)          |
| Migrations                              | `uv run just db-migrate [--test]` · `db-revision "msg"`    |
| SQL shell                               | `uv run just psql [-d fragancia_test]`                     |
| Plans: status / lint / scope            | `uv run just plans-status` / `plans-lint` / `plans-scope <plan>` |
| Regenerate agents and skills            | `uv run just harness-sync` (`harness-check` in `check`)    |

`just` comes from the uv environment (`rust-just` dev dependency). Python dependencies:
`uv add [--dev] <pkg>` (never `pip install`, never npm/yarn/npx; pnpm only for the web app in
phase 3). The only env file is `apps/api/.env` (from `.env.example`); compose has inline
defaults — export a variable to override one, and update the matching URL in `apps/api/.env`.

## Architecture rules (not negotiable)

Enforced by `uv run just arch` (import-linter) where possible. If a rule gets in your way, do
not work around it: explain why to the user and propose an ADR.

1. **Modular monolith**: one deployable, modules with strict boundaries (own PostgreSQL schema,
   public API in the module's `__init__.py`, no cross-module foreign keys or table reads).
   Modules are registered in [docs/modules.json](docs/modules.json).
2. **Hexagonal layers per module**: `domain` (pure, no IO) ← `application` (use cases + ports)
   ← `infrastructure` (adapters) · `http` (routers).
3. **Lightweight CQRS**: commands go through aggregates and repositories and return a
   `Result` inside `TransactionRunner.run(...)`; queries go through `XxxQueries` ports and
   return response models directly.
4. **Contracts first**: Pydantic models in each module's `contracts.py` are the source of
   truth; `apps/api/openapi.json` is generated from them (committed) and the web client from it.
5. **Events that must not be lost use the transactional outbox** (payments, order changes).
6. **The web app has no business logic** and never touches the database.
7. **Declared access**: routes live in `public_router()` or `admin_router()`, never a bare
   `APIRouter`.
8. **Persistence**: SQLAlchemy Core tables + explicit mappers (ADR 0007); SQLAlchemy only in
   `infrastructure/`. Only `container.py` and `main/` create adapters. Schema changes only
   through new Alembic migrations.

## Definition of done

- `uv run just check` green (lint, types, architecture, unit tests, plans-lint, hook tests,
  adapter drift), plus `uv run just test-integration` when persistence changed.
- The plan's evidence sections filled for its status; `plans-scope` clean.
- Docs updated in their canonical source, in English; `openapi.json` regenerated if a contract
  changed.
- Commits per [conventions/commits.md](docs/harness/conventions/commits.md); PRs per
  [conventions/pull-requests.md](docs/harness/conventions/pull-requests.md): what changed,
  why, how, how it was verified, what's missing, risks and rollout. **No AI attribution.**
- Say plainly what was NOT verified.

## Forbidden actions

Never, unless the user explicitly asks for that exact action (then explain the risk and give
them the command to run themselves):

- `docker compose down -v`, `docker volume rm|prune`, `docker system prune` (delete all data);
  `uv run just db-reset` of the development database (agents may use `--test`).
- `git stash`, `git reset --hard`, `git clean -f`, `git checkout -- <path>`, `git restore` of
  the worktree, `git push --force`, pushing to `main`, `--no-verify`, rewriting published
  history, `git add -A` / `git add .` (stage explicit paths).
- Deleting files you did not create — untracked does not mean disposable. Temporary files go
  in the session scratchpad.
- Reading or printing `.env` files (only `.env.example`), keys or other secrets.
- Editing generated files: `.claude/agents/`, generated skills, `.codex/agents/` (use
  `uv run just harness-sync`), `apps/api/openapi.json` (use `uv run just openapi`), `uv.lock`
  (use `uv add`), or an existing migration (create a new one).
- Destructive SQL (`DROP`, `TRUNCATE`, `DELETE` without `WHERE`); `SELECT COUNT(*)` before any
  `DELETE`/`UPDATE`; never write to a non-local environment.
