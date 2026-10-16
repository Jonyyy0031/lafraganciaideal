---
status: done
module: orders
min_implementer: mid
depends_on: ["platform-api-foundation/004"]
---

# 001 — Delivery options

## Context

The `orders` module does not exist yet: `docs/modules.json:8` lists it as `planned`. This plan
creates it, with its first piece: the delivery options the checkout (plan 003) will price. It
applies README decisions 3, 5, 6, 19 and 20.

The three methods:
- **Home delivery:** a flat rate, free from a minimum subtotal when that is set.
- **Local delivery:** one price, for an editable list of postal codes.
- **Pickup:** one point with an address and hours. It is always free.

Each method has an on/off switch, and all three start off. An owner sets them in the panel with
the new owner-only permission `delivery:manage`. The storefront reads the options and a quote.

**What exists today:**
- **Singleton settings.** Inventory has a one-row settings table:
  - the domain value is `InventorySettings` (`modules/inventory/domain/stock.py:100-101`);
  - the table is `modules/inventory/infrastructure/tables.py:54-60`;
  - the repository inserts the default row and locks it, and saves with an upsert
    (`modules/inventory/infrastructure/sql_settings_repository.py:12-55`).

  This plan imitates it.
- **Money.** `Money` (integer cents, MXN) is in the shared kernel (`shared/kernel/money.py:8-36`).
  Catalog prices are cents with VAT included, up to `PRICE_MAX_CENTS = 10_000_000`
  (`modules/catalog/domain/perfume.py:35`, `:106`).
- **Routers.** Public and admin routers follow `modules/catalog/http/router.py:64-80` and
  `modules/inventory/http/router.py`.
- **Permissions.** They live in `modules/identity/domain/user.py:80-87`. This branch already
  has `OUTBOX_MANAGE` from platform-api-foundation/004.
- **Module registration.** It follows `docs/recipes/new-module.md` §1–4:
  - `MODULES` in `container.py:31`;
  - the `module-layers` containers in `apps/api/.importlinter:84-90`;
  - a schema-creating migration like `apps/api/migrations/versions/0012_inventory_stock.py:20`.

**Approach.**
- **One `DeliverySettings` value** holds the three methods, stored in one row of
  `orders.delivery_settings`.
- **`PUT` replaces the whole value,** validated in the domain. The panel edits it as one form.
- **The quote is a pure domain function,** `quote(settings, postal_code, subtotal)`, which
  returns the available methods with their cost. Plan 003 reuses it. The public route exposes
  it so the storefront can show the options before checkout.
- **Rejected:** one table per method, which means more moving parts for three small values.

## Out of scope

- **Carrier quotes, labels and tracking** (the `shipping` module).
- **Zones or several pickup points** (decision 6 picked one list and one point).
- **Free delivery for local delivery or pickup.** Pickup is always free; the "free from"
  amount is home delivery only (decision 5).
- **The cart, checkout and orders** (plans 002–005).

## Dependencies

- **platform-api-foundation/004 (done, PR open):** its change to `identity/domain/user.py`
  (`OUTBOX_MANAGE`). The branch builds on it.

## Steps

1. **Register the module**
   - Files: `docs/modules.json` (modify), `apps/api/.importlinter` (modify),
     `apps/api/src/fragancia_api/container.py` (modify)
   - Do:
     - In `modules.json`, set `orders` to `"status": "active"`. Edit only that field; it is a
       hot file.
     - In `.importlinter`, add `fragancia_api.modules.orders` to the `module-layers`
       containers.
     - In `container.py`, import `orders` from `modules/orders/module.py` and append it to
       `MODULES`, after `inventory`.
   - Observable result: `uv run just arch` is green, with the new container.

2. **Domain**
   - Files: `apps/api/src/fragancia_api/modules/orders/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/orders/domain/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/orders/domain/delivery.py` (create),
     `apps/api/src/fragancia_api/modules/orders/domain/errors.py` (create),
     `apps/api/src/fragancia_api/modules/orders/domain/repositories.py` (create)
   - Do: in `delivery.py` (pure), frozen dataclasses:
     - **`DeliveryMethod(StrEnum)`:** `home`, `local` and `pickup`.
     - **`PostalCode`:** exactly 5 ASCII digits, after stripping.
     - **`DeliveryPrice`:** a `Money`, from 0 to `DELIVERY_PRICE_MAX_CENTS = 1_000_000` (a
       sanity bound of $10,000, not a business price).
     - **`HomeDelivery(enabled: bool, price: DeliveryPrice, free_from: Money | None)`.**
       `free_from` is 1..`10_000_000` cents when set.
     - **`LocalDelivery(enabled: bool, price: DeliveryPrice, postal_codes: frozenset[PostalCode])`.**
       It holds at most 500 codes, and when enabled it needs at least one.
     - **`Pickup(enabled: bool, address: str, hours: str)`:**
       - the strings are stripped, with inner whitespace kept;
       - when enabled, `address` needs 1..300 characters and `hours` 1..200;
       - when disabled, they may be empty, and the same maximums apply.
     - **`DeliverySettings(home, local, pickup)`,** with:
       - `DeliverySettings.default()`: all three disabled, prices 0, no codes, empty strings
         (decision 20);
       - `DeliverySettings.create(...) -> Result[DeliverySettings, DeliveryOptionsInvalid]`,
         which builds all the parts and collects the first error.
     - **`DeliveryQuote(method, cost: Money)`.**
     - **`quote(settings, postal_code: PostalCode | None, subtotal: Money) -> list[DeliveryQuote]`,**
       in the order home, local, pickup:
       - home, when enabled: cost 0 if `free_from` is set and `subtotal >= free_from`, else the
         price;
       - local, when enabled and `postal_code` is in the list: its price;
       - pickup, when enabled: 0.

       An empty list means nothing applies.
     - **`errors.py`:**
       - `DeliveryOptionsInvalid(InvalidValueError)`, with code `ORDERS_DELIVERY_OPTIONS_INVALID`
         and `details={"field": …, …}`;
       - `PostalCodeInvalid(InvalidValueError)`, with code `ORDERS_POSTAL_CODE_INVALID`.
     - **`repositories.py`:** `DeliverySettingsRepository` with `get()`, `get_for_update()` and
       `save()`, like inventory's `SettingsRepository`.
     - **`__init__.py`:** the module docstring says "Public API for other modules: nothing yet".
       Each package has a one-line docstring.
   - Observable result: domain tests can build valid and invalid settings, and `quote` answers
     at every boundary.

3. **Contracts, application, infrastructure and migration**
   - Files: `apps/api/src/fragancia_api/modules/orders/contracts.py` (create),
     `apps/api/src/fragancia_api/modules/orders/application/ports.py` (create, repair round 1),
     `apps/api/src/fragancia_api/modules/orders/application/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/orders/application/commands/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/orders/application/commands/delivery.py` (create),
     `apps/api/src/fragancia_api/modules/orders/application/queries/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/orders/application/queries/delivery.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/tables.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_delivery_settings_repository.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/in_memory.py` (create),
     `apps/api/migrations/versions/0017_orders_delivery_settings.py` (create)
   - Do:
     - **Contracts:**
       - `HomeDeliveryOptions(enabled, price_cents, free_from_cents: int | None)`;
       - `LocalDeliveryOptions(enabled, price_cents, postal_codes: list[str])`;
       - `PickupOptions(enabled, address, hours)`;
       - `DeliveryOptions(home, local, pickup)`, used both as the admin request and response
         body. Bound the payload only: lists at most 1000 items, strings at most 1000
         characters;
       - `PublicDeliveryOptions`, the enabled methods only: home with its price and
         `free_from_cents`, local with its price and postal codes, pickup with its address and
         hours;
       - `DeliveryQuoteLine(method, cost_cents)` and `DeliveryQuoteResponse(options: list[DeliveryQuoteLine])`.
     - **Commands** (`commands/delivery.py`): `UpdateDeliveryOptions(repository, transactions)`
       builds `DeliverySettings.create(...)` from the request. Inside `transactions.run`, it
       calls `get_for_update()`, then `save()`.
     - **Queries** (`queries/delivery.py`):
       - `GetDeliveryOptions` returns the admin shape;
       - `GetPublicDeliveryOptions` returns the enabled methods;
       - `QuoteDelivery.execute(postal_code: str | None, subtotal_cents: int)` parses the postal
         code (`ORDERS_POSTAL_CODE_INVALID`), bounds the subtotal at 0..`100_000_000` (otherwise
         `ORDERS_DELIVERY_OPTIONS_INVALID` with `field: "subtotal_cents"`), and calls `quote`.

       They read through the repository's `get()` in a reader session, or through an
       `XxxQueries` port if that is simpler for the implementer. Record which in Deviations.
     - **Table** `orders.delivery_settings`:
       - `id` Integer PK (always 1);
       - `home_enabled` Boolean, `home_price_cents` Integer, `home_free_from_cents` Integer
         (nullable);
       - `local_enabled` Boolean, `local_price_cents` Integer, `local_postal_codes`
         `ARRAY(String(5))`;
       - `pickup_enabled` Boolean, `pickup_address` String(300), `pickup_hours` String(200).

       Every column except `home_free_from_cents` is not null.
     - **The SQL repository** copies `SqlSettingsRepository`: insert the default row `ON
       CONFLICT DO NOTHING`, lock it, upsert to save. `InMemoryDeliverySettings` holds one value.
     - **Migration.** Run `uv run just db-revision "orders delivery settings"`; it becomes `0017`.
       - Add `CREATE SCHEMA IF NOT EXISTS "orders"` by hand.
       - Insert the default row: all off, prices 0, no codes, empty strings.
       - The downgrade drops the table and the schema.
   - Observable result: after `db-migrate`, `orders.delivery_settings` has one row with all
     three methods off.

4. **Permission, routes and wiring**
   - Files: `apps/api/src/fragancia_api/modules/identity/domain/user.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/http/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/orders/http/router.py` (create),
     `apps/api/src/fragancia_api/modules/orders/module.py` (create),
     `apps/api/openapi.json` (modify)
   - Do:
     - **Permission** (`user.py`): `DELIVERY_MANAGE = "delivery:manage"`, in `Role.OWNER` only
       (decision 19).
     - **Router** (`http/router.py`). The literal `"delivery:manage"` gets the same comment as
       `modules/inventory/http/router.py`, which uses `INVENTORY_MANAGE`.
       - `admin_router(prefix="/delivery-options", tags=["orders · admin"],
         dependencies=[require_permission("delivery:manage")])`:
         - `GET ""` returns `DeliveryOptions`;
         - `PUT ""` returns 204, with 422 `ORDERS_DELIVERY_OPTIONS_INVALID`.
       - `public_router(prefix="/delivery-options", tags=["orders"])`:
         - `GET ""` returns `PublicDeliveryOptions`;
         - `GET /quote?postal_code=&subtotal_cents=` returns `DeliveryQuoteResponse`, with 422
           `ORDERS_POSTAL_CODE_INVALID` or `ORDERS_DELIVERY_OPTIONS_INVALID`. `postal_code` is
           optional; without it, local delivery never applies.
       - Each docstring states the rules: pickup is free, and home delivery is free from
         `free_from_cents`.
     - **`module.py`:** register the repository, the command and the three queries, with
       `AppModule(name="orders", register=register, routers=routers)`.
     - Run `uv run just openapi`.
   - Observable result:
     - an owner's `PUT` with valid options answers 204, and the public `GET` shows only the
       enabled methods;
     - staff gets 403 on the admin routes;
     - `GET /api/v1/delivery-options/quote?postal_code=77500&subtotal_cents=300000` lists the
       methods that apply, with their costs.

5. **Docs and tests**
   - Files: `docs/architecture.md` (modify), `apps/api/tests/unit/orders/` (create),
     `apps/api/tests/integration/orders/` (create), `apps/api/tests/unit/identity/` (modify),
     `apps/api/tests/unit/` (modify), `apps/api/tests/integration/identity/` (modify)
   - Do:
     - **`docs/architecture.md`:** mark the `orders` row as built ("delivery options; next:
       cart").
     - **Tests.** Existing tests that pin the owner's permission list or the admin route list
       get the new entries. Name the files in Deviations. The tester writes the layered tests;
       each test directory gets an `__init__.py` and an autouse fixture that truncates
       `orders.delivery_settings` and re-inserts the default row.
   - Observable result: `uv run just check` and `uv run just test-integration` are green.

## Acceptance criteria

- [ ] **Default.** After `db-migrate`, the public `GET /api/v1/delivery-options` lists no
  method, and the quote for any postal code returns an empty `options` list.
- [ ] **Configure.** An owner `PUT`s:
  - home on at 18,000 cents, free from 250,000;
  - local on at 9,000 cents for `77500` and `77505`;
  - pickup on, with an address and hours.

  It answers 204. The admin `GET` returns the same values, and the public `GET` lists all three.
- [ ] **Quote.**
  - `postal_code=77500`, `subtotal_cents=100000`: home 18000, local 9000, pickup 0.
  - `subtotal_cents=250000`: home 0.
  - `postal_code=01000`: local is absent.
  - Without `postal_code`: home and pickup only.
- [ ] **Validation.** Each of these answers 422 `ORDERS_DELIVERY_OPTIONS_INVALID`, with the
  field named:
  - local delivery on without postal codes;
  - pickup on with an empty address;
  - a negative price;
  - a postal code of 4 digits.

  A quote with `postal_code=abc` answers 422 `ORDERS_POSTAL_CODE_INVALID`.
- [ ] **Permission.** Staff gets 403 on `GET` and `PUT /api/v1/admin/delivery-options`. An
  anonymous call gets 401. The public routes need no session.
- [ ] **Switch off.** Turning home delivery off removes it from the public `GET` and from
  quotes, and keeps its stored price.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `PostalCode`, `DeliveryPrice` and `free_from` bounds; enabled-method requirements; `default()`; `quote` at every boundary (`subtotal == free_from`, free_from unset, postal code in/out, methods off, no postal code); `ROLE_PERMISSIONS` owner-only `delivery:manage` |
| application | yes     | `UpdateDeliveryOptions` (valid, each invalid field, saves under the lock); the three queries (enabled filter, quote parsing and bounds) |
| http        | yes     | Admin GET/PUT (owner 200/204, staff 403, anonymous 401, 422 shapes), public GET and quote (no session, 422s), admin routes protected, OpenAPI |
| integration | yes     | Migration (schema, table, default row, round trip); SQL repository get/lock/save round trip, including the postal-code array and the null `free_from` |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

- **Queries read through a reader port.** `DeliverySettingsRepository.get()` uses
  `Database.session`, which raises outside a transaction, so the queries cannot use it. Added
  `DeliverySettingsReader.current()` (in `domain/repositories.py`, a listed file), implemented by
  `SqlDeliverySettingsReader` (in `sql_delivery_settings_repository.py`, via
  `Database.reader()`) and by `InMemoryDeliverySettings`. No new files.
- **Existing tests updated for the new owner permission** (step 5): in
  `apps/api/tests/unit/identity/`: `test_auth_http.py`, `test_resolve_session_actor.py`,
  `test_sessions.py`, `test_user_domain.py`; and
  `apps/api/tests/integration/identity/test_sql_identity.py`. No other existing test pinned
  the route list.
- **Cosmetic:** the generated migration was rewritten by hand to the repo's style (double
  quotes), plus the schema creation and default row the plan asked for.
- The layered tests for orders (`tests/unit/orders/`, `tests/integration/orders/`) were written
  in the tester phase (see Test coverage).

### Repair round 1 (2026-10-15, main session: review → implementing)

**Why.** The review found nothing blocking, only three Lows. Following the standing choice, the
user chose to fix all three now.

**Repair spec.**
1. **L1: the read port moves to `application/ports.py`.**
   - Files: `apps/api/src/fragancia_api/modules/orders/application/ports.py` (create),
     `apps/api/src/fragancia_api/modules/orders/domain/repositories.py`,
     `apps/api/src/fragancia_api/modules/orders/application/queries/delivery.py`,
     `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_delivery_settings_repository.py`,
     `apps/api/src/fragancia_api/modules/orders/infrastructure/in_memory.py`,
     `apps/api/src/fragancia_api/modules/orders/module.py`.
   - Move `DeliverySettingsReader` unchanged into the new `ports.py`, with a module docstring as
     in `modules/catalog/application/ports.py`.
   - Remove it from `domain/repositories.py` and update every import.
2. **L2: the subtotal bound lives in the domain.**
   - Files: `apps/api/src/fragancia_api/modules/orders/domain/delivery.py`, the same
     `queries/delivery.py`.
   - Add `QUOTE_SUBTOTAL_MAX_CENTS = 100_000_000` and a frozen `Subtotal` holding a `Money`,
     with `Subtotal.create(cents) -> Result[Subtotal, DeliveryOptionsInvalid]`.
     - It accepts 0..`QUOTE_SUBTOTAL_MAX_CENTS`.
     - Otherwise it returns `details={"field": "subtotal_cents", "min": 0, "max": …}`, the
       same answer as today.
   - `quote(settings, postal_code, subtotal: Subtotal)`. `QuoteDelivery` calls
     `Subtotal.create`, and its own bound check goes. Plan 003 gets the bound from the domain.
3. **L3: the invalid postal code is named.**
   - Files: the same `domain/delivery.py`.
   - When an entry of `local.postal_codes` is invalid, the error's details add `"index"` (its
     position in the request, from 0) and `"value"` (the raw entry, truncated to 20
     characters). The first invalid entry is reported.

**Repair done (implementer).** L1: `DeliverySettingsReader` moved to the new
`application/ports.py` (domain, SQL, in-memory and `module.py` needed no import change: they
satisfy it structurally). L2: `Subtotal` + `Subtotal.create` in `domain/delivery.py`; `quote`
takes a `Subtotal`; `QuoteDelivery` uses it. L3: invalid postal code error details carry
`index` and `value[:20]`. Only mechanical test change: `quote(...)` calls in
`tests/unit/orders/test_delivery_domain.py` wrap the amount in `Subtotal(...)`. No deviations.

**Scope.** These product files only. The implementer updates only the test imports and the call
sites that the moved port and the new `quote` signature break, mechanically. The tester adds the
regression tests.

## Test coverage

Tester run, 2026-10-15. Baseline (before any test): `uv run just check` green (2470 unit
passed, 3 skipped; 685 harness) and `uv run just test-integration` green (716 passed, 5
skipped). Closing: `uv run just check` green (2633 unit passed, 3 skipped; 685 harness) and
`uv run just test-integration` green (751 passed, 5 skipped). New: 163 unit, 35 integration, no
xfail, no skip of its own. No GAP found; nothing NOT CONFIRMED (the migration test skips as
NOT CONFIRMED only if the database user cannot create a scratch database, which it can here).

Files: `tests/unit/orders/` (`support.py`, `test_delivery_domain.py`,
`test_delivery_application.py`, `test_delivery_http.py`, `test_delivery_wiring.py`),
`tests/integration/orders/` (`conftest.py`, `test_sql_delivery_settings.py`,
`test_migration_0017_delivery_settings.py`). The existing identity tests that pin the owner's
permission list were updated by the implementer (Deviations) and pass; the owner-only
`delivery:manage` assertion is in `tests/unit/identity/test_user_domain.py`
(`test_the_owner_has_every_permission_and_staff_only_catalog_and_inventory`).

| Behavior | Source | Layer | Test | State |
| -------- | ------ | ----- | ---- | ----- |
| Postal code: 5 ASCII digits after stripping; leading zero kept; rejects 4/6 digits, letters, blank, non-ASCII digits | `domain/delivery.py:31` | domain | `test_delivery_domain.py::test_a_postal_code_is_five_digits_after_stripping`, `::test_rejects_a_postal_code_that_is_not_five_ascii_digits` | CONFIRMED |
| Price bound 0..1,000,000, field named | `domain/delivery.py:43` | domain | `::test_a_delivery_price_accepts_zero_up_to_the_sanity_bound`, `::test_rejects_a_price_outside_the_bounds` | CONFIRMED |
| `free_from` optional, 1..10,000,000 | `domain/delivery.py:74` | domain | `::test_free_from_accepts_its_bounds`, `::test_rejects_free_from_outside_its_bounds` | CONFIRMED |
| Local on needs a code; max 500 distinct; strip and dedupe; malformed code rejected | `domain/delivery.py:98` | domain | `::test_local_delivery_on_needs_at_least_one_postal_code`, `::test_accepts_exactly_the_maximum_number_of_distinct_postal_codes`, `::test_rejects_more_distinct_postal_codes_than_the_maximum`, `::test_postal_codes_are_stripped_and_deduplicated` | CONFIRMED |
| Pickup: strip, inner whitespace kept, 1..300 / 1..200 when on, 0..max when off | `domain/delivery.py:132` | domain | `::test_pickup_on_needs_an_address`, `::test_pickup_on_needs_hours`, `::test_pickup_off_may_have_empty_address_and_hours`, `::test_pickup_strings_are_stripped_and_inner_whitespace_is_kept`, the two `_maximum_length` tests | CONFIRMED |
| `default()` all off, prices 0, no codes, empty strings; quotes nothing | `domain/delivery.py:158` | domain | `::test_the_default_has_every_method_off_and_empty`, `::test_the_default_quotes_nothing_for_any_input` | CONFIRMED |
| `create` reports the first broken rule in the order home, local, pickup; validates prices while off | `domain/delivery.py:168` | domain | `::test_the_first_broken_rule_is_reported_in_the_order_home_local_pickup`, `::test_prices_are_validated_even_while_the_method_is_off` | CONFIRMED |
| `quote`: order home, local, pickup; `subtotal == free_from` free; one cent below charged; free_from unset never free; code in/out; no code; methods off; pickup 0 | `domain/delivery.py:205` | domain | `::test_quotes_every_method_in_the_order_home_local_pickup`, `::test_home_is_free_when_the_subtotal_equals_free_from`, `::test_home_is_charged_one_cent_below_free_from`, `::test_home_is_never_free_when_free_from_is_unset`, `::test_local_is_absent_for_a_postal_code_off_the_list`, `::test_without_a_postal_code_only_home_and_pickup_apply`, `::test_a_method_that_is_off_is_not_quoted`, `::test_pickup_is_always_free`, `::test_nothing_applies_when_every_method_is_off` | CONFIRMED |
| Owner-only `delivery:manage` | `identity/domain/user.py:84,88` | domain | `tests/unit/identity/test_user_domain.py::test_the_owner_has_every_permission_and_staff_only_catalog_and_inventory` | CONFIRMED |
| `UpdateDeliveryOptions`: valid stores everything; invalid fields rejected without touching the store; `get_for_update` then `save` inside the transaction; replaces, not merges | `application/commands/delivery.py:17` | application | `test_delivery_application.py::test_updating_stores_the_whole_configuration`, `::test_updating_locks_the_row_then_saves_inside_the_transaction`, `::test_updating_rejects_invalid_options_without_touching_the_store`, `::test_updating_replaces_the_previous_configuration_not_merges_it` | CONFIRMED |
| `GetDeliveryOptions` admin shape, sorted codes, off method keeps its price | `application/queries/delivery.py:33` | application | `::test_the_admin_query_*` (3) | CONFIRMED |
| `GetPublicDeliveryOptions` enabled filter, each method hidden alone, `free_from` none | `application/queries/delivery.py:62` | application | `::test_the_public_query_*` (4) | CONFIRMED |
| `QuoteDelivery`: parses the postal code, bounds the subtotal 0..100,000,000 (`field: subtotal_cents`), postal-code error first | `application/queries/delivery.py:96` | application | `::test_rejects_a_malformed_postal_code`, `::test_rejects_a_subtotal_outside_the_bounds_naming_the_field`, `::test_accepts_the_subtotal_bounds`, `::test_a_bad_postal_code_is_reported_before_a_bad_subtotal`, the quote line tests | CONFIRMED |
| Admin routes: 401 anonymous and unknown session; 403 staff on GET and PUT; a 403 PUT changes nothing; all admin operations declare the cookie scheme | `http/router.py:29-47` | http | `test_delivery_http.py::test_the_admin_routes_answer_401_without_a_session`, `::..._for_an_unknown_session`, `::test_the_admin_routes_answer_403_without_the_delivery_permission`, `::test_a_forbidden_put_does_not_change_the_configuration`; `assert_admin_routes_are_protected` in the fixture | CONFIRMED |
| Owner PUT answers 204 with an empty body, GET returns the same values; PUT replaces the whole value | `http/router.py:41-53` | http | `::test_the_owner_saves_with_204_and_reads_back_the_same_values`, `::test_put_replaces_the_whole_configuration`, `::test_the_owner_reads_the_default_configuration` | CONFIRMED |
| 422 `ORDERS_DELIVERY_OPTIONS_INVALID` with `details.field` for: local on without codes, pickup on with empty address / blank hours, negative price, price over the bound, 4-digit code, `free_from` 0; a rejected PUT keeps the old value | `http/router.py:41-53` | http | `::test_a_broken_rule_answers_422_naming_the_field` (8 cases), `::test_a_rejected_put_keeps_the_previous_configuration` | CONFIRMED |
| Malformed bodies and payload bounds (over 1000 codes, strings over 1000, price over 1e9) answer `VALIDATION_ERROR` | `contracts.py:13-39` | http | `::test_a_malformed_body_answers_the_generic_validation_error`, `::test_the_payload_bounds_answer_the_generic_validation_error` | CONFIRMED |
| Public GET needs no session; empty by default; lists enabled methods only; switching home off hides it publicly and keeps the price in the admin view | `http/router.py:56-62` | http | `::test_the_public_routes_need_no_session`, `::test_the_public_list_is_empty_by_default`, `::test_the_public_list_shows_the_three_enabled_methods`, `::test_switching_home_off_removes_it_from_the_public_list_and_keeps_its_price` | CONFIRMED |
| Quote route: acceptance-criteria quotes (77500 at 100000 and 250000, 01000, no code, home off); `postal_code=abc` and empty answer 422 `ORDERS_POSTAL_CODE_INVALID`; subtotal out of range 422 with `field: subtotal_cents`; missing or non-numeric subtotal `VALIDATION_ERROR` | `http/router.py:65-77` | http | `::test_the_quote_*`, `::test_a_malformed_postal_code_answers_422_postal_code_invalid`, `::test_a_subtotal_out_of_range_answers_422_naming_the_field`, `::test_a_missing_subtotal_*`, `::test_a_non_numeric_subtotal_*` | CONFIRMED |
| OpenAPI declares the four operations, 204 and 422 on PUT, no security on the public ones, `subtotal_cents` required and `postal_code` optional | `http/router.py` | http | `::test_the_openapi_document_declares_the_delivery_operations`, `::test_the_admin_delivery_routes_exist_under_the_admin_prefix` | CONFIRMED |
| Real container registers `orders` after `inventory` and resolves the four services | `module.py`, `container.py:34` | http (wiring) | `test_delivery_wiring.py` (5 tests) | CONFIRMED |
| Repository: default row reads as default; `get` with no row writes nothing; `get_for_update` inserts the default when absent; save round trip with the postal-code array (stored sorted), null `free_from`, empty list and blank pickup; upsert keeps one row, id 1; rollback leaves it unchanged | `infrastructure/sql_delivery_settings_repository.py:62-95` | integration | `test_sql_delivery_settings.py` (repository section, 11 tests) | CONFIRMED |
| Reader works outside a transaction | `sql_delivery_settings_repository.py:94` | integration | `::test_the_reader_sees_the_settings_without_an_active_transaction` | CONFIRMED |
| Table constraints: PK on id, every column but `home_free_from_cents` NOT NULL, `varchar(5)` codes, `varchar(300)` and `varchar(200)` | `infrastructure/tables.py`, migration | integration | `::test_the_table_holds_only_one_row_per_id`, `::test_every_column_but_free_from_rejects_null` (8), `::test_the_database_bounds_*` (2) | CONFIRMED |
| `get_for_update` locks the row: a second writer waits until the first commits and then sees its value | `sql_delivery_settings_repository.py:70` | integration | `::test_get_for_update_blocks_a_second_writer_until_the_first_commits` | CONFIRMED |
| Use cases over the real adapters: default state offers nothing and quotes empty; update, read back and quote; an invalid update leaves the stored value; home off keeps the price | `module.py` | integration | `::test_the_default_database_state_offers_nothing_publicly`, `::test_update_then_read_back_and_quote_through_the_container`, `::test_an_invalid_update_leaves_the_stored_configuration_untouched`, `::test_switching_home_off_keeps_the_price_in_the_database` | CONFIRMED |
| Migration 0017 on a scratch database: nothing of orders at 0016; upgrade creates schema, table, columns (types, nullability, lengths), PK and the default row; downgrade drops table and schema; a round trip restores the default row and discards edits; upgrade to head again | `migrations/versions/0017_orders_delivery_settings.py` | integration | `test_migration_0017_delivery_settings.py` (7 tests) | CONFIRMED |
| `fragancia_test` holds only the seeded default row after the suite | `tests/integration/orders/conftest.py` | integration | autouse `clean_orders`; `::test_leaves_only_the_seeded_default_row_between_tests`; checked with `just psql` after the closing run (1 row, all off; 0 `fragancia_mig_*` databases left) | CONFIRMED |
| Acceptance "Default", "Configure", "Quote", "Validation", "Permission", "Switch off" | plan criteria | http + integration | covered by the rows above; the running-app proof is the verifier's | CONFIRMED (tests) |

### Repair round 1 regression tests (tester, 2026-10-16)

Baseline: `uv run just check` green (2633 unit passed, 3 skipped; 685 harness). Closing:
`uv run just check` green (685 harness; the unit step passed) and `uv run just
test-integration` green (751 passed, 5 skipped). A later `uv run just test` run reported 3334
passed, 3 skipped. New: 17 unit tests (domain 13 counting parametrized cases, http 2 plus one
tightened assertion, ports 2), no integration, no xfail, no skip, no GAP. After the run
`fragancia_test` holds one `orders.delivery_settings` row, all methods off.

| Behavior | Source | Layer | Test | State |
| -------- | ------ | ----- | ---- | ----- |
| `Subtotal.create` accepts 0, 1 and `QUOTE_SUBTOTAL_MAX_CENTS`; rejects -1 and max+1 with `details == {field: subtotal_cents, min: 0, max: 100_000_000}` and code `ORDERS_DELIVERY_OPTIONS_INVALID` | `domain/delivery.py:44-52` | domain | `test_delivery_domain.py::test_a_subtotal_accepts_zero_up_to_the_sanity_bound`, `::test_rejects_a_subtotal_outside_the_bounds_naming_the_field`, `::test_the_subtotal_bound_is_the_plan_value` | CONFIRMED |
| `quote()` takes a `Subtotal` (all existing quote tests build one; one uses `Subtotal.create`) | `domain/delivery.py:226` | domain | `::test_quote_prices_against_a_validated_subtotal` and the `quote` section | CONFIRMED |
| `QuoteDelivery` answers 422 with `field: subtotal_cents` and full details for -1 and max+1 | `application/queries/delivery.py:105` | application, http | `test_delivery_application.py::test_rejects_a_subtotal_outside_the_bounds_naming_the_field`; `test_delivery_http.py::test_a_subtotal_out_of_range_answers_422_naming_the_field` (assertion tightened to the whole `details`) | CONFIRMED |
| Invalid postal code details: `index` (position in the request, from 0, unstripped list), `value`, first invalid entry reported, value truncated to 20 chars (20 kept whole, 25 and 30 cut) | `domain/delivery.py:124-136` | domain | `::test_an_invalid_postal_code_is_named_by_index_and_value`, `::test_the_first_invalid_postal_code_is_the_one_reported`, `::test_the_postal_code_index_is_the_position_in_the_request`, `::test_the_reported_postal_code_value_is_truncated_to_twenty_characters`, `::test_a_postal_code_value_of_exactly_twenty_characters_is_kept_whole` | CONFIRMED |
| Same details through `PUT` (422, index 1 value `7750`; 30-char value cut to 20) | `http/router.py` | http | `test_delivery_http.py::test_an_invalid_postal_code_in_the_put_is_named_by_index_and_value`, `::test_the_reported_postal_code_value_is_truncated_to_twenty_characters` | CONFIRMED |
| `DeliverySettingsReader` is defined in and importable from `application/ports.py`; the in-memory adapter satisfies it | `application/ports.py:8` | application | `test_delivery_ports.py` (2 tests) | CONFIRMED |

Observations (not gaps): the admin payload accepts prices up to 1e9 and the domain narrows to
1,000,000, so a price between them is a 422 `ORDERS_DELIVERY_OPTIONS_INVALID`, not
`VALIDATION_ERROR`, which matches the plan ("bound the payload only").

## Review findings

Reviewer run, 2026-10-15, on `git diff main...HEAD` (df0c33d implementation, 32c9d26 tests;
worktree clean).

**Checklist: 14/14 applicable items passed (1 not applicable).**

- [x] `plans-scope`: "Every change is inside the plan" (30 declared, 42 changed). Hot files
  checked by hand: `.importlinter` adds one container line; `container.py` adds one import
  and appends `orders` after `inventory` in `MODULES`; `modules.json` changes only the
  `orders` status to `active` (step 1, recipe §1).
- [x] `uv run just check` green (2633 unit passed, 3 skipped; 685 harness; OpenAPI
  `--check` up to date).
- [x] `uv run just test-integration` green (751 passed, 5 skipped).
- [x] Business rules in `domain/delivery.py` (bounds, enabled-method requirements, `quote`);
  router, mappers and reader hold none. The subtotal sanity bound sits in `QuoteDelivery`, as
  the plan specified (constant from the domain; see L2).
- [x] CQRS-lite: `UpdateDeliveryOptions` validates, then `get_for_update()` + `save()` inside
  `transactions.run`, returns `Result`; queries go through the `DeliverySettingsReader` port
  (`Database.reader()`) and return contract models.
- [x] Models from `orders/contracts.py`; `openapi.json` regenerated (generator `--check` passes).
- [x] Errors `ORDERS_DELIVERY_OPTIONS_INVALID` / `ORDERS_POSTAL_CODE_INVALID` as
  `InvalidValueError` subclasses via `Err`, `details.field` set; no internals leaked.
- [x] Money as `Money` integer cents; no floats; no datetimes or ids involved.
- [x] Migration 0017: new file, `CREATE SCHEMA IF NOT EXISTS "orders"`, table matches
  `tables.py` (types, nullability, lengths, PK), seeded default row all off, no FKs, no drops;
  downgrade drops table then schema (exercised by the migration test on a scratch DB).
  `env.py` derives managed schemas from `metadata`, so `orders` is covered automatically.
- [x] Routes in `admin_router` / `public_router` only; admin router depends on
  `require_permission("delivery:manage")` with the inventory-style comment;
  `assert_admin_routes_are_protected` runs in the HTTP fixture.
- [x] Wiring resolves (`test_delivery_wiring.py`, `test_container.py` green); module
  registered once; adapters created only in `module.py` `register()` like catalog.
- [x] No secrets or real personal data (pickup fixture is a fake address).
- [x] `## Deviations` honest. Spot-checked the reader-port claim: `Database.session`
  (`shared/infrastructure/database.py:26-34`) raises outside a unit of work, so a query
  could not use `get()`.
- [x] Docs: `docs/architecture.md` orders row marked built; `modules.json` active; no other
  doc lists modules or role permissions (grepped `apps/api/README.md`, `docs/`).
- [-] PR body: not applicable yet (no PR open for the branch); the main session writes it per
  `conventions/pull-requests.md`.

**Decisions checked.** 3 (three methods), 5 (home flat rate, free from a minimum, `>=` at the
boundary, editable), 6 (local list + one price, pickup one point and always 0), 19
(`DELIVERY_MANAGE` in `Role.OWNER` only; staff 403 on GET and PUT; anonymous 401) and 20
(migration and `default()` all off; public list and quote empty by default). The public
`GET` returns only enabled methods, without the `enabled` flag, and a disabled method's
stored price never appears publicly; the quote never exposes a disabled method.

**Mutants (edited in place, restored by hand, `git status` clean afterwards), all killed:**
`>=` → `>` on `free_from` (3 tests fail across domain, application, http); staff given
`delivery:manage` (4 identity tests fail); admin router guarded by `catalog:manage` instead
(3 http tests fail); public pickup shown while disabled (4 tests fail); migration seeds
`home_enabled = true` (3 migration tests fail); downgrade keeps the schema (1 migration test
fails). `fragancia_test` afterwards: exactly one `orders.delivery_settings` row (id 1, all off,
prices 0, `{}`, empty strings); 0 `fragancia_mig_*` databases left.

**Findings: 0 critical, 0 high, 0 medium, 3 low (none needs a change for this plan).**

- **L1 (low, informational)** `apps/api/src/fragancia_api/modules/orders/domain/repositories.py:22`:
  the read port `DeliverySettingsReader` lives in `domain/repositories.py`, while the recipe
  puts query ports in `application/ports.py` (`XxxQueries`). Declared in Deviations and
  allowed by the plan ("or through an `XxxQueries` port"); no behavior impact. When plan
  002/003 add an `application/ports.py`, consider moving it there.
- **L2 (low, informational)** `apps/api/src/fragancia_api/modules/orders/application/queries/delivery.py:106`:
  the subtotal bound (0..`QUOTE_SUBTOTAL_MAX_CENTS`) is enforced in `QuoteDelivery`, not in
  the domain `quote()`. Plan-conformant, but plan 003 calls `quote()` directly with a
  server-computed subtotal, so it will not get that bound. Scenario: none today; note for
  plan 003 so it does not rely on `quote()` rejecting a negative subtotal.
- **L3 (low, uncertain/UX)** `apps/api/src/fragancia_api/modules/orders/domain/delivery.py:108-115`:
  a malformed postal code in the PUT reports `details.field = "local.postal_codes"` without
  saying which entry is bad. Scenario: an owner pastes 300 codes with one typo and the panel
  cannot point at it. Meets the plan's acceptance criteria as written; worth deciding before
  the web panel (phase 3) is built.

Observation (not a finding): enabling home or local delivery at price 0 is accepted. The plan
bounds the price at 0..1,000,000 and decision 20 does not require a positive price, so this
matches the plan.

### Re-review after repair round 1 (2026-10-16, main session, inline)

The repair is small, so the main session read the product diff `c8bc226..f66b528` against the
repair spec instead of dispatching the reviewer.
- **L1, fixed.** `DeliverySettingsReader` moved unchanged to `application/ports.py`, with a
  docstring, and was removed from `domain/repositories.py`. The adapters satisfy it
  structurally.
- **L2, fixed.**
  - `Subtotal.create` bounds 0..`QUOTE_SUBTOTAL_MAX_CENTS`, with the same details as before.
  - `quote()` takes a `Subtotal` and compares `subtotal.money >= free_from`, which keeps the
    boundary.
  - `QuoteDelivery` delegates the check.
- **L3, fixed.** The first invalid postal code reports `index` (its position in the request)
  and `value` (cut to 20 characters).
- **No new defect seen.** The regression tests (`aa50176`) cover each point.
- **Suites:** `check` green (2650 unit, 685 harness; `just test` 3334 counts both) and
  `test-integration` 751 passed, 5 skipped, as reported by the tester.

Status → `verify`.

## Verification

Verified by the main session on 2026-10-16, against `feat/orders-delivery-options` at `34f3baf`.
- **Environment:** the API (`just api`, port 8100) ran on the development database, migrated to
  `0017`.
- **Driver:** the session-scratchpad script `qa_inv/qa_delivery.py` called the admin and public
  routes over HTTP.
- **Users:** the owner `verifier007@example.test`, and the QA staff user from inventory-stock/004.

**Suites:** the repair closed with `just check` green (2650 unit, 685 harness) and
`just test-integration` green (751 passed, 5 skipped). No code changed since.

**Flows driven against the acceptance criteria**
- [x] **Default.**
  - `orders.delivery_settings` had one row, `(1, off, off, off)`.
  - The public `GET /api/v1/delivery-options` returned `{home: null, local: null, pickup: null}`.
  - The quote for `77500` returned `[]`.
- [x] **Configure.** The owner `PUT`:
  - home on at 18,000, free from 250,000;
  - local on at 9,000 for `77500` and `77505`;
  - pickup on, with "Av. Tulum 123, Cancún" and "Lun-Sáb 10:00-19:00".

  It answered 204. The admin `GET` returned exactly the same body. The public `GET` listed all
  three without the on/off flags.
- [x] **Quote.**
  - `77500` with subtotal 100,000: home 18000, local 9000, pickup 0.
  - Subtotal 250,000: home 0.
  - `01000`: local absent.
  - No postal code: home and pickup only.
- [x] **Validation.** Each answered 422 `ORDERS_DELIVERY_OPTIONS_INVALID`:
  - local on with no codes: `field local.postal_codes, min 1`;
  - an empty pickup address: `field pickup.address`;
  - a negative price: `field home.price_cents, min 0, max 1000000`;
  - a 4-digit code: `field local.postal_codes, index 1, value "7750"` (the L3 fix).

  A quote with `postal_code=abc` answered 422 `ORDERS_POSTAL_CODE_INVALID`. A subtotal of `-1`
  answered 422, with `field subtotal_cents, min 0, max 100000000` (the L2 fix).
- [x] **Permission.** Staff got 403 on both the admin `GET` and `PUT`, and an anonymous admin
  `GET` got 401. The public routes answered without a session.
- [x] **Switch off.** Turning home off removed it from the public `GET` and from the quote. The
  admin `GET` kept its 18,000 and 250,000.

**After the run:** the settings were restored to the default (all off), and the public `GET` was
empty again. No QA data remains.

**NOT VERIFIED live:** nothing in this plan's criteria. The migration round trip is covered by
the scratch-database integration test.

**Verdict: verified.** The plan is ready for the user to mark it `done`.
