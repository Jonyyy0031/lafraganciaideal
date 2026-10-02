# platform-api-foundation — API foundation and the reference module

## Goal

Stand up the FastAPI modular monolith with everything every future module relies on
(configuration, database and transactions, errors, declared access, outbox, worker, layer
checks, Docker image, CI) and prove it end to end with a first business slice — brands in the
`catalog` module — that becomes the reference pattern every later module imitates.

## Plans

| Plan | Title                                    | Depends on | Purpose                                                        |
| ---- | ---------------------------------------- | ---------- | -------------------------------------------------------------- |
| 001  | API platform                             | —          | Skeleton, kernel, DB, errors, access, outbox, worker, CI, image |
| 002  | Catalog brands as the reference module   | 001        | First full slice through every layer; recipes for new modules  |

## Dependency notes

002 needs the kernel, transaction runner, outbox publisher, access dependency and error
mapping that 001 delivers.

## Decisions with the user

1. (2026-10-02) The API is decomposed into initiatives: foundation (this one), catalog,
   identity, inventory, orders, payments, notifications; shipping later.
2. (2026-10-02) The foundation includes a real business slice (catalog brands) as the
   reference module, the way web-rh started with `organization`.
3. (2026-10-02) Admin endpoints are protected from day one with **declared access**: each
   route is public or admin; admin is resolved through an `ActorResolver` port whose first
   adapter checks a development token from `.env`. The identity initiative replaces the
   adapter without touching routes. The development adapter refuses to run in production.
4. (2026-10-02) Technical choices: uv workspace (`apps/api` is a member); async SQLAlchemy 2
   with psycopg 3; one Alembic history with one PostgreSQL schema per module; a small
   `Result` type for expected errors; manual composition root + FastAPI `Depends`; mypy strict;
   import-linter; arq worker in the same image; outbox from the skeleton on.
5. (2026-10-02) The API listens on **port 8100** (8000/8001 are taken on the dev machine;
   web-rh uses 3000/3001).
6. (2026-10-02) Persistence uses **SQLAlchemy Core tables + explicit mappers**, not ORM mapping
   of domain classes (ADR 0007).

## Delivered

- **001** (2026-10-02): FastAPI platform — settings, kernel (`Result`, errors, events, Money),
  async SQLAlchemy + `TransactionRunner`, transactional outbox + arq relay, error shape,
  declared access with the dev-token resolver, health, request ids, logs, import-linter
  contracts, Docker `runtime`/`migrator`, CI jobs. Guide: `apps/api/README.md`; ADRs 0007–0008.
- **002** (2026-10-02): `catalog` brands — create (admin), public and admin lists; reference
  module + recipes in `docs/recipes/`.

## Considered and discarded

- **Platform only, no business slice**: patterns (Result, repositories, queries) would be
  untested against a real case.
- **Leave admin routes open until identity**: routes would have to be touched later.
- **Build identity before catalog**: delays the reference module.
- **ORM imperative mapping of domain classes**: instruments domain classes at runtime and
  conflicts with slotted dataclasses; Core + mappers keeps the domain untouched.
- **dependency-injector library**: a manual composition root is explicit and easy for agents.
