# Testing contract

## The golden rule: nothing invented

Every test derives from one of two sources, and only these:

1. **Real code**: contracts, aggregates, use cases, adapters, routes (cite `file:line`).
2. **Verifiable local execution**: real endpoint responses, real rows in the test DB, real
   command output.

What you cannot confirm by either source **does not become a test that assumes it**:

| Situation                                                      | Mark                                                                                         |
| -------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Confirmed from code or execution                               | a normal test — **CONFIRMED**                                                                |
| Can't be confirmed locally (Mercado Pago, carriers, WhatsApp)  | `@pytest.mark.skip(reason="NOT CONFIRMED: <why> …")`                                         |
| Plan promises it, code doesn't do it (**GAP**)                 | `@pytest.mark.xfail(strict=True, reason="GAP: <plan ref> …")` — passes while the gap exists and fails (XPASS strict) when someone fixes it, so it gets promoted |

Never weaken an assertion to make a test pass. A test that "passes" on invented behavior is
worse than no test: false confidence that breaks on the first refactor.

## Layers — test at the lowest layer that truly validates

| Layer       | Where                                                       | Validates                                            | Runs in                        |
| ----------- | ----------------------------------------------------------- | ---------------------------------------------------- | ------------------------------ |
| domain      | `apps/api/tests/unit/<m>/test_*_domain.py`                  | invariants, rules, events (pure, no fakes needed)    | `uv run just check`            |
| application | `apps/api/tests/unit/<m>/test_<command>.py`                 | use cases with in-memory adapters + fakes            | `uv run just check`            |
| http        | `apps/api/tests/unit/<m>/test_*_http.py`                    | status codes, error `code`s, validation, access, wiring | `uv run just check`         |
| integration | `apps/api/tests/integration/<m>/` (`@pytest.mark.integration`) | SQL adapters, constraints, outbox against `fragancia_test` | `uv run just test-integration` |
| tooling     | `scripts/test_*.py`, `scripts/**/test_*.py`                 | repo tooling (bootstrap, commits, plans; hooks and harness from plan 002) | `uv run just check`         |
| e2e         | not set up yet                                              | user flows in a browser                              | —                              |

- **http** tests build the app with `build_app(services, routers)` from
  `fragancia_api.main.http`, a `ServiceRegistry` filled with in-memory adapters, and an
  `httpx.AsyncClient(transport=ASGITransport(app=app))` (model:
  `tests/unit/catalog/test_brand_http.py`, which also calls
  `assert_admin_routes_are_protected`).
- **integration** runs against the real `fragancia_test` database; `just test-integration`
  applies the migrations first (`db-migrate --test`). Each module truncates its own tables in
  an autouse fixture.

A field validation is an http test, not e2e. A state transition is a domain test, not only an
http test. Unique constraints, query filters/pagination and outbox rows are integration tests.
E2E infrastructure must be added by a plan before any plan may require the e2e layer.

## Tools and existing infrastructure (don't reinvent)

- Platform fakes: `fragancia_api.shared.infrastructure.in_memory` (`FixedClock`,
  `InMemoryTransactionRunner`, `RecordingEventPublisher`). Module in-memory adapters:
  `<module>/infrastructure/in_memory.py`.
- Helpers: `apps/api/tests/support.py` (`make_settings`, `ADMIN_HEADERS`,
  `assert_admin_routes_are_protected`).
- Integration: `apps/api/tests/integration/conftest.py` binds to `DATABASE_URL_TEST`;
  `Settings` refuses a test URL whose database name does not end in `_test`.
- Composition: `tests/unit/test_container.py` proves the real container resolves; a new
  registration must keep it green.
- Test names describe behavior in English (`test_rejects_a_duplicated_brand_slug`).
  Arrange-Act-Assert, one behavior per test, deterministic (no real clock, network or random
  ids in unit tests), synthetic data only. Async tests need no marker (pytest-asyncio, `asyncio_mode = "auto"` in `pyproject.toml`).

## Coverage matrix (the tester fills `## Test coverage` in the plan)

| Behavior (from plan / code)          | Source (`file:line`) | Layer       | Test                                                    | State               |
| ------------------------------------ | -------------------- | ----------- | ------------------------------------------------------- | ------------------- |
| Rejects a brand whose slug is taken  | `create_brand.py:NN` | application | `test_create_brand.py::test_rejects_duplicated_slug`    | CONFIRMED           |
| …                                    |                      |             |                                                         | GAP / NOT CONFIRMED |

## Forbidden antipatterns

1. Asserting on source text (reading a `.py` file and matching strings).
2. Tests that assume endpoints, fields, columns or error codes from a plan without confirming
   them in code.
3. Weakening assertions to pass; `skip` without a `NOT CONFIRMED:` reason; non-strict `xfail`.
4. Hitting the development database, the network or the real clock from
   domain/application/http tests.
5. Fixture ids or data that collide across tests (per-test data, fresh in-memory stores).
6. Running a malicious or destructive command to prove a hook blocks it: hook tests hand the
   command to the guard process as inert text (`just test-harness`, plan 002).

## Execution budget

Two full runs per phase: a **baseline** before writing tests (`uv run just check`, plus
`uv run just test-integration` when persistence is involved) and a **closing** run. In between
run only what you touch: `uv run pytest <path>::<test>` (integration paths need
`-m integration`, `just up` and `uv run just db-migrate --test`). Details in
[roles/tester.md](../roles/tester.md).
