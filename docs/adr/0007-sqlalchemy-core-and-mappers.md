# 0007 — SQLAlchemy Core tables and explicit mappers

- **Status**: Accepted (amends the persistence part of [ADR 0003](0003-fastapi-and-angular.md))
- **Date**: 2026-10-02

## Context

ADR 0003 chose SQLAlchemy 2 with *imperative mapping* to keep ORM code out of the domain.
Imperative mapping still instruments the domain classes at runtime (adds attributes and
descriptors), conflicts with slotted/frozen dataclasses, and brings identity-map and
lazy-loading behavior into aggregates. Decision: [initiative README](../../plans/platform-api-foundation/README.md) #6.

## Decision

- Each module declares its tables with **SQLAlchemy Core** (`Table` on the shared
  `MetaData`) in `<module>/infrastructure/tables.py`, in its own PostgreSQL schema.
- Repositories translate rows ↔ aggregates with **explicit mapper functions**; queries select
  columns straight into response models. Domain classes never see SQLAlchemy.
- One Alembic history; `alembic check` in CI ensures tables and migrations match.

## Alternatives considered

- **Imperative ORM mapping**: see Context.
- **Declarative ORM models + mappers**: two class hierarchies per aggregate for no benefit
  over Core when every read goes through explicit queries anyway.

## Consequences

- Mapping code is written by hand (it is short and obvious, and it is where persistence
  concerns such as unique-violation → `ConflictError` live).
- No lazy loading or unit-of-work magic: what a repository does is what its SQL says.
