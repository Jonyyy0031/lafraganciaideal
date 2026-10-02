# Recipe — a new use case in an existing module

## Command (changes state)

Model: `modules/catalog/application/commands/create_brand.py`.

1. **Domain first**: the rule lives in the aggregate or a value object
   (`modules/<m>/domain/`). Expected failures are `Err(<CategoryError>)` with a stable code
   `<MODULE>_<THING>_<PROBLEM>` in `domain/errors.py`.
2. **Ports**: if the command needs new persistence, add the method to the repository port in
   `domain/repositories.py` (write-side only — no "for a screen" methods).
3. **Use case** `application/commands/<verb_noun>.py`: a class with keyword dependencies and
   `async def execute(...) -> Result[T, DomainError]`. Everything happens inside
   `self._transactions.run(work)`:
   validate value objects → check/load → call the aggregate → save through the repository →
   `await self._events.publish(aggregate.pull_events())` → `Ok(...)`.
   Return `Err` *before* writing whenever possible; `run` rolls back on `Err` anyway.
4. **Adapters**: implement the new repository method in `infrastructure/sql_*.py` (explicit
   mapping; unique violations → `Err(ConflictError)` inside a savepoint) and in
   `infrastructure/in_memory.py`.
5. **Contract** in `contracts.py` and **route** in `http/router.py`: request model in, call the
   use case, `unwrap(result)`, response model out. Admin actions go in `admin_router`.
   Document error codes in the docstring and `responses=`.
6. **Wire** it in `module.py` (`services.add(UseCase, UseCase(...))`).

## Query (reads only)

Model: `modules/catalog/application/queries/list_brands.py` + `sql_brand_queries.py`.

1. Response model in `contracts.py`.
2. Method on the `XxxQueries` port (`application/ports.py`) returning that model (or
   `Page[...]` from `fragancia_api.shared.contracts`).
3. SQL implementation with `async with self._database.reader()`, selecting only the needed
   columns; in-memory implementation for unit tests.
4. Thin use case + route (`public_router` or `admin_router`) + wiring.

## Tests

Domain rules (unit) → use case with in-memory adapters (unit) → route status codes, error
codes and access (unit, `httpx.ASGITransport`) → SQL adapter (integration). See the catalog
tests for each.
