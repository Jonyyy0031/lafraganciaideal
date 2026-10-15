---
status: review
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

Observations (not gaps): the admin payload accepts prices up to 1e9 and the domain narrows to
1,000,000, so a price between them is a 422 `ORDERS_DELIVERY_OPTIONS_INVALID`, not
`VALIDATION_ERROR`, which matches the plan ("bound the payload only").

## Review findings

## Verification
