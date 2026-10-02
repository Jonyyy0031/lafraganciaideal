# 0003 — FastAPI for the API, Angular for the web app

- **Status**: Accepted
- **Date**: 2026-10-02

## Context

The architecture comes from `~/codes/web-rh` (Express + Next.js). For this project the author
wants a different stack while keeping the same architecture. The storefront needs SEO; the
admin panel is forms and tables. Decision: [initiative README](../../plans/platform-infraestructura/README.md) #2.

## Decision

- **API**: Python 3.14, FastAPI, Pydantic v2 (contracts), SQLAlchemy 2 with imperative mapping
  (keeps the domain free of ORM code), Alembic (migrations), arq on Valkey (jobs),
  import-linter (boundaries), pytest, ruff and a strict type checker.
- **Web**: Angular with SSR (`@angular/ssr`): server-rendered catalog pages for SEO, lazy
  admin routes. One app for storefront and admin.
- **Contract**: Pydantic models → OpenAPI → generated TypeScript client in
  `packages/api-client`.

## Alternatives considered

- **Express + Next.js**: reuses web-rh code, but the author explicitly wants to vary the stack.
- **Express + Angular / FastAPI + Next.js**: change only one side; rejected in favor of the
  author's preference.

## Consequences

- web-rh code cannot be copied literally; its patterns translate one-to-one (Zod → Pydantic,
  Prisma → SQLAlchemy + Alembic, BullMQ → arq, dependency-cruiser → import-linter).
- No shared TypeScript domain package between API and web; acceptable because the web app has
  no business logic.
- Agents may have outdated knowledge of recent versions; AGENTS.md requires reading the
  installed version's documentation.
