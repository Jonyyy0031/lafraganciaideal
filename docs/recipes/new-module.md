# Recipe — a new business module

Copy `catalog` (the reference module) by name. Example: `inventory`.
Paths below are relative to `apps/api/src/fragancia_api/`.

## 1. Register it

- `docs/modules.json`: set the module to `"status": "active"` (it is also a commit scope).
- The plan that creates the module lists every file below in its `Files:` lines.

## 2. Create the layers

| Create                                         | Copy from                                                  |
| ---------------------------------------------- | ---------------------------------------------------------- |
| `modules/inventory/__init__.py`                | `modules/catalog/__init__.py` (docstring = public API)     |
| `modules/inventory/domain/<aggregate>.py`      | `modules/catalog/domain/brand.py` (value objects, aggregate, events) |
| `modules/inventory/domain/errors.py`           | `modules/catalog/domain/errors.py` (codes `INVENTORY_*`)   |
| `modules/inventory/domain/repositories.py`     | `modules/catalog/domain/repositories.py`                   |
| `modules/inventory/contracts.py`               | `modules/catalog/contracts.py`                             |
| `modules/inventory/application/ports.py`       | `modules/catalog/application/ports.py` (`XxxQueries`)      |
| `modules/inventory/application/commands/*.py`  | `modules/catalog/application/commands/create_brand.py`     |
| `modules/inventory/application/queries/*.py`   | `modules/catalog/application/queries/list_brands.py`       |
| `modules/inventory/infrastructure/tables.py`   | `modules/catalog/infrastructure/tables.py` (`schema="inventory"`) |
| `modules/inventory/infrastructure/sql_*.py`    | `sql_brand_repository.py`, `sql_brand_queries.py`          |
| `modules/inventory/infrastructure/in_memory.py`| `modules/catalog/infrastructure/in_memory.py`              |
| `modules/inventory/http/router.py`             | `modules/catalog/http/router.py` (`public_router` / `admin_router`) |
| `modules/inventory/module.py`                  | `modules/catalog/module.py`                                |

Every layer folder has an `__init__.py` with a one-line docstring.

## 3. Wire it

- `container.py`: import `module` from `modules/inventory/module.py` and append it to `MODULES`.
- `apps/api/.importlinter`: add `fragancia_api.modules.inventory` to the `containers` of the
  `module-layers` contract. The domain-purity, framework-free and independence contracts use
  `modules.*` and cover the new module automatically.

## 4. Database

Follow [db-change.md](db-change.md): tables in `infrastructure/tables.py`, then
`uv run just db-revision "inventory stock"`, add `CREATE SCHEMA` / `DROP SCHEMA` by hand.

## 5. Talking to another module

Never import another module's internals. If `orders` needs prices from `catalog`:

1. `orders/application/ports.py` declares a port in **orders' terms** (`ProductPrices`).
2. `catalog/__init__.py` exposes a small facade (a use case or query) as its public API.
3. `orders/infrastructure/catalog_product_prices.py` implements the port by calling that
   facade (anti-corruption layer), and `orders/module.py` wires it.
4. In `.importlinter`, allow exactly that import in the `modules-independent` contract
   (`ignore_imports = fragancia_api.modules.orders.infrastructure.catalog_product_prices -> fragancia_api.modules.catalog`).

For reactions ("when an order is paid, commit stock") use events instead: subscribe in
`register()` with `platform.subscriptions.subscribe("orders.order.paid", handler)`; handlers
must be idempotent.

## 6. Tests

| Layer       | Where                                    | Copy from                                    |
| ----------- | ---------------------------------------- | -------------------------------------------- |
| domain      | `tests/unit/inventory/test_*_domain.py`  | `tests/unit/catalog/test_brand_domain.py`    |
| application | `tests/unit/inventory/test_<command>.py` | `tests/unit/catalog/test_create_brand.py`    |
| http        | `tests/unit/inventory/test_*_http.py`    | `tests/unit/catalog/test_brand_http.py` (keeps `assert_admin_routes_are_protected`) |
| integration | `tests/integration/inventory/`           | `tests/integration/catalog/test_sql_brands.py` (truncate your tables in an autouse fixture) |

## 7. Done when

`uv run just check` and `uv run just test-integration` pass, the endpoints answer in
`uv run just api`, and `docs/architecture.md` lists the module as built.
