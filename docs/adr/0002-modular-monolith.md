# 0002 — Modular monolith

- **Status**: Accepted
- **Date**: 2026-10-02

## Context

A small business, one developer, one product. The domain has clear areas (catalog, stock,
orders, payments, notifications, shipping) that interact closely: paying an order touches
payments, orders, inventory and notifications. The author's previous project (`~/codes/web-rh`)
proved this shape works well with AI agents. Decision: [initiative README](../../plans/platform-infraestructura/README.md) #1.

## Decision

A single deployable (`apps/api`) split into **modules with strict boundaries**: own
PostgreSQL schema, public API in `__init__.py`, no cross-module foreign keys or table reads,
communication through the consumer's own ports (adapters call the other module's facade) or
through events. Boundaries and layers are checked by import-linter in CI. The same code runs
as two processes: HTTP and worker. Modules are registered in `docs/modules.json`.

## Alternatives considered

- **Microservices**: distributed transactions for a checkout, far more infrastructure than one
  person can run.
- **Monolith without boundaries**: fast at first, then coupling makes every change risky, and
  AI agents spread logic everywhere.

## Consequences

- Simple local transactions and one deployment.
- A module can be extracted later by replacing its cross-module adapters with HTTP or queues.
- Some duplication of ids/read models across modules is accepted in exchange for independence.
