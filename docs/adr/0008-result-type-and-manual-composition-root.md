# 0008 — Result type for expected errors, manual composition root

- **Status**: Accepted
- **Date**: 2026-10-02

## Context

Use cases fail in expected ways (invalid name, duplicate, not found) and unexpected ways
(database down). Mixing both in exceptions hides which failures a caller must handle. Wiring
adapters into use cases must be explicit and easy for agents to follow. Decision:
[initiative README](../../plans/platform-api-foundation/README.md) #4.

## Decision

- Expected failures are values: `Ok[T] | Err[DomainError]` (`shared/kernel/result.py`),
  consumed with `match`. Each `DomainError` has a category (not found, conflict, invalid
  value, business rule) that HTTP maps to a status, and a stable `code`.
- Exceptions are only for the unexpected; the HTTP layer answers 500 without details.
- `TransactionRunner.run(work)` commits on `Ok` and rolls back on `Err` or exceptions.
- Dependency injection is a **manual composition root** (`container.py`): each module's
  `register(platform, services)` builds its adapters and use cases into a `ServiceRegistry`
  keyed by type; routers receive them with `Depends(provide(UseCase))`.

## Alternatives considered

- **Exceptions for everything**: the signature no longer says what can fail.
- **A DI library (dependency-injector, punq)**: more indirection than a function that builds
  objects; harder to read for agents.
- **FastAPI dependency overrides in tests**: tests build the registry with in-memory adapters
  instead, which is closer to production wiring.

## Consequences

- Every use case signature documents its failures.
- Adding a module means adding one entry to `MODULES` in `container.py`.
