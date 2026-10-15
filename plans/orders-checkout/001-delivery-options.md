---
status: testing
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
- The layered tests for orders (`tests/unit/orders/`, `tests/integration/orders/`) are not
  written: they are the tester's phase.

## Test coverage

## Review findings

## Verification
