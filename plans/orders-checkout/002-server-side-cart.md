---
status: done
module: orders
min_implementer: mid
depends_on: ["001"]
---

# 002 — Server-side cart

## Context

A guest builds a cart before checkout (plan 003). It applies README decisions 1, 2, 4, 11, 14,
15 and 21–23:
- the cart lives on the server, identified by a cookie;
- it shows current prices and availability;
- it never reserves stock.

**What exists today:**
- **The catalog's public facade.** `PresentationDirectory` returns names, availability and
  `is_sellable`, but no price, slug, photo or stock
  (`apps/api/src/fragancia_api/modules/catalog/application/queries/presentation_directory.py:12-48`;
  SQL in `modules/catalog/infrastructure/sql_presentation_directory.py:19-100`).
- **The facts the cart needs** already exist in the catalog:
  - the effective price, with the sale window (`Presentation.effective_price`,
    `modules/catalog/domain/perfume.py:313-317`; the SQL mirror `_effective_price` and
    `_sale_active` in `modules/catalog/infrastructure/sql_perfume_queries.py:75-89`);
  - the stock status from the stock copy (`stock_status`, `perfume.py:263-277`);
  - storefront visibility (`_VISIBLE`, `sql_perfume_queries.py:97`);
  - card photo URLs from `media_base_url` (`SqlPerfumeQueries._url`, `:154-160`).
- **The session token pattern.** 256 random bits, and only the SHA-256 digest is stored
  (`modules/identity/infrastructure/secure_session_tokens.py:5-12`). It travels in an
  httpOnly cookie on path `/api/v1`, with `Secure` taken from `settings.is_production`
  (`modules/identity/http/cookies.py:7-35`; `modules/identity/module.py:222-228`).
- **The orders module** exists since plan 001, with its delivery settings.
- **Cross-module calls** follow `docs/recipes/new-module.md` §5: a port, a facade, an adapter
  and two `.importlinter` ignore lines.

**Approach.**
- **A new catalog facade, `PresentationOffers`**, with `offers(ids, now)`. It returns per
  presentation what a buyer needs:
  - the label, slug and card photo;
  - the effective and regular price;
  - the availability and lead times;
  - whether it is visible;
  - the available units, from the stock copy.

  The orders module reads it through its own port `CatalogOffers`.
- **A `Cart` aggregate** in orders, with lines (presentation, quantity, the price the customer
  last saw).
  - The cookie carries a random token, and only its digest is stored.
  - The cart view is computed on every read from the cart plus the current offers, so prices
    and availability are always current.
- **A worker job** deletes carts untouched for 30 days.
- **Why not reuse `PresentationDirectory`?** It serves inventory, which needs no prices.
  Adding prices, photos and stock to it would widen a facade other code already depends on.
- **Rejected:** storing a computed snapshot of the cart. It goes stale, which is exactly what
  decision 2 avoids.

## Out of scope

- **Checkout, orders and reservations** (plan 003). The cart's stock check uses the catalog's
  stock copy, which lags inventory by about 2 s. The authoritative check is the reservation at
  checkout.
- **Merging carts across devices, or saving a cart to an account** (there are no accounts,
  decision 1).
- **Delivery costs in the cart.** The storefront can call plan 001's quote with the cart
  subtotal.
- **Discounts or coupons.**

## Dependencies

- **001 (done):** the `orders` module and its wiring.
- **catalog-perfumes (done):** perfumes, presentations, sales and photos. The stock copy comes
  from inventory-stock/002 (done).

## Steps

1. **Catalog: the `PresentationOffers` facade**
   - Files: `apps/api/src/fragancia_api/modules/catalog/application/queries/presentation_offers.py` (create),
     `apps/api/src/fragancia_api/modules/catalog/application/ports.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/sql_presentation_offers.py` (create),
     `apps/api/src/fragancia_api/modules/catalog/infrastructure/in_memory_perfumes.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/module.py` (modify),
     `apps/api/src/fragancia_api/modules/catalog/__init__.py` (modify)
   - Do:
     - **`PresentationOffer`,** frozen:
       - `presentation_id`, `perfume_id`, `perfume_slug`;
       - `label` (`"{brand} {perfume} {abbreviation} {ml} ml"`, as inventory builds it in
         `modules/inventory/infrastructure/catalog_presentations.py`);
       - `ml`, `card_url: str | None` (the perfume's first photo by position);
       - `price_cents` (effective now) and `regular_price_cents`;
       - `availability` (`in_stock` or `made_to_order`), `lead_time_min_days` and
         `lead_time_max_days`;
       - `is_visible`: the presentation is active, the perfume published and not archived, and
         the brand active;
       - `available_units: int | None`: the stock copy's `stock_available` for in-stock
         presentations, None for made to order.
     - **`PresentationOffers(queries)`,** with `offers(ids, now) -> dict[UUID, PresentationOffer]`.
       Unknown ids are absent. The docstring says the stock figure lags inventory by about 2 s.
     - **Port `PresentationOffersQueries`** in `ports.py`.
     - **SQL** (`sql_presentation_offers.py`):
       - one select joining presentations, perfumes, brands and concentrations, with a lateral or
         correlated first-photo subquery;
       - reuse `_effective_price(now)` and `_VISIBLE` from `sql_perfume_queries.py` by importing
         them. Both live in catalog infrastructure.
       - It takes `media_base_url` like `SqlPerfumeQueries`.
     - **In-memory:** implement it over the in-memory perfume store.
     - **Wiring:** register it in `module.py`. Export `PresentationOffers` and `PresentationOffer`
       from `__init__.py` and extend the "Public API" docstring.
   - Observable result: unit and integration tests return offers with the sale price inside the
     window, the regular price outside it, `is_visible` false for a hidden perfume, and None
     units for made to order.

2. **Orders: the cart domain**
   - Files: `apps/api/src/fragancia_api/modules/orders/domain/cart.py` (create),
     `apps/api/src/fragancia_api/modules/orders/domain/errors.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/domain/repositories.py` (modify)
   - Do:
     - **Constants:** `LINE_QUANTITY_MAX = 10` (decision 14), `CART_LINES_MAX = 30` (a sanity
       bound), `CART_LIFETIME = timedelta(days=30)` (decision 15).
     - **`CartLine(presentation_id, quantity, seen_price_cents, added_at)`.**
     - **`Cart(AggregateRoot)`,** with `id`, `token_digest`, `lines` (ordered by `added_at`),
       `created_at` and `updated_at`. Every change stamps `updated_at`.
       - `Cart.create(token_digest, now)`.
       - `set_line(presentation_id, quantity, current_price_cents, now) -> Result[None,
         CartQuantityInvalid | CartTooManyLines]`:
         - the quantity is 1..`LINE_QUANTITY_MAX`;
         - a new line is refused past `CART_LINES_MAX`;
         - it sets the quantity and `seen_price_cents = current_price_cents`, because changing
           the quantity acknowledges the price (decision 23).
       - `remove_line(presentation_id, now)`, which does nothing if the line is absent.
       - `acknowledge_prices(current: Mapping[UUID, int], now)`: it sets `seen_price_cents` to
         the current price for every line present in `current`.
     - **A pure view builder** `cart_view(cart, offers, now) -> CartView`, as domain
       dataclasses. Each line gets a `status`:
       - `unavailable`: no offer, or `not is_visible`;
       - `sold_out`: in stock with available units ≤ 0;
       - `insufficient_stock`: in stock with `0 < available < quantity`;
       - `ok` otherwise.

       It also computes:
       - `unit_price_cents` (current) and `line_total_cents` = unit × quantity;
       - `price_changed_from_cents` = `seen_price_cents` when it differs from the current price,
         else None;
       - `subtotal_cents`, the sum over `ok` lines only (decision 22);
       - `item_count`, the units over `ok` lines;
       - `has_problems`, true when any line is not `ok`;
       - `lead_time_max_days`, the largest `lead_time_max_days` over `ok` made-to-order lines
         (decision 4), else None.
     - **Errors:**
       - `CartQuantityInvalid(InvalidValueError)`, `ORDERS_CART_QUANTITY_INVALID`, with
         `details {min 1, max 10}`;
       - `CartTooManyLines(InvalidValueError)`, `ORDERS_CART_TOO_MANY_LINES`;
       - `CartPresentationUnavailable(ConflictError)`, `ORDERS_CART_PRESENTATION_UNAVAILABLE`;
       - `CartStockUnavailable(ConflictError)`, `ORDERS_CART_STOCK_UNAVAILABLE`, with
         `details {"available": n}` (decision 21).
     - **`CartRepository`:**
       - `get_by_token_for_update(token_digest) -> Cart | None`;
       - `add(cart)` and `save(cart)`;
       - `delete_untouched_before(cutoff, limit) -> int`.
   - Observable result: domain tests cover every transition and every line status.

3. **Orders: the application, tokens and persistence**
   - Files: `apps/api/src/fragancia_api/modules/orders/application/ports.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/application/commands/cart.py` (create),
     `apps/api/src/fragancia_api/modules/orders/application/queries/cart.py` (create),
     `apps/api/src/fragancia_api/modules/orders/contracts.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/catalog_offers.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/cart_tokens.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_cart_repository.py` (create),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/tables.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/in_memory.py` (modify),
     `apps/api/migrations/versions/0018_orders_carts.py` (create)
   - Do:
     - **Ports:**
       - `CatalogOffers` (`offers(ids, now) -> dict[UUID, Offer]`), with orders' own frozen
         `Offer` carrying the fields above;
       - `CartTokens` (`new() -> str`, `digest(token) -> str`);
       - `CartReader` (`by_token(token_digest) -> Cart | None`, through `Database.reader()`).
     - **Adapters:**
       - `CatalogOffersAdapter(PresentationOffers)` maps `PresentationOffer` to `Offer`;
       - `SecureCartTokens` copies identity's two methods. Modules may not import each other, so
         this is a deliberate copy, noted in a comment.
     - **Commands** (`commands/cart.py`), each in `transactions.run` and each returning the
       fresh `CartView` contract plus the token when a new cart was created:
       - `SetCartLine(token | None, presentation_id, quantity)`:
         1. load or create the cart (a new token when there is none, or it is unknown);
         2. fetch the offer;
         3. refuse with `CartPresentationUnavailable` when it is unknown or not visible;
         4. refuse with `CartStockUnavailable` when it is in stock and
            `quantity > available_units`;
         5. `set_line` with the current price.
       - `RemoveCartLine(token, presentation_id)`. An unknown token returns an empty view.
       - `AcknowledgeCartPrices(token)`.
       - `PurgeCarts.run_once() -> int` deletes up to 500 carts with
         `updated_at < now - CART_LIFETIME`.
     - **Query** `GetCart(token | None)`. With no token or an unknown token it returns the empty
       view, with no write.
     - **Contracts:**
       - `CartLineView` with every field of the domain view; `status` is a Literal;
       - `CartView` with `lines`, `subtotal_cents`, `item_count`, `has_problems` and
         `lead_time_max_days`;
       - `SetCartLineRequest(quantity: int)`, bounded for the payload only.
     - **Tables:**
       - `orders.carts`: `id` Uuid PK, `token_digest` String(64) unique, `created_at`, and
         `updated_at` indexed;
       - `orders.cart_lines`: `cart_id` FK to `carts.id` with `ON DELETE CASCADE`,
         `presentation_id` Uuid, `quantity` Integer with a check 1..10, `seen_price_cents`
         Integer and `added_at`. The PK is `(cart_id, presentation_id)`.
     - **Migration.** Run `uv run just db-revision "orders carts"`; it becomes `0018`.
   - Observable result: application tests cover each command and refusal, including the
     unknown-token paths.

4. **HTTP, cookie, job and wiring**
   - Files: `apps/api/src/fragancia_api/modules/orders/http/cart_cookie.py` (create),
     `apps/api/src/fragancia_api/modules/orders/http/router.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/module.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/__init__.py` (modify),
     `apps/api/.importlinter` (modify),
     `apps/api/openapi.json` (modify)
   - Do:
     - **`CartCookie(secure, max_age_seconds)`,** in the style of identity's `SessionCookie`:
       - name `fragancia_cart`, path `/api/v1`, httpOnly;
       - **`SameSite=Lax`**, so the cart survives a link from WhatsApp or email;
       - `Secure` from `settings.is_production`;
       - max age `CART_LIFETIME`, refreshed on every write.
     - **`public_router(prefix="/cart", tags=["orders · cart"])`:**
       - `GET ""` returns `CartView`;
       - `PUT /lines/{presentation_id}` with `SetCartLineRequest` returns `CartView`, sets the
         cookie when a cart was created, and answers 409 `ORDERS_CART_PRESENTATION_UNAVAILABLE`
         or `ORDERS_CART_STOCK_UNAVAILABLE`, or 422 `ORDERS_CART_QUANTITY_INVALID` or
         `ORDERS_CART_TOO_MANY_LINES`;
       - `DELETE /lines/{presentation_id}` returns `CartView`;
       - `POST /acknowledge-prices` returns `CartView`.

       The docstrings state decisions 14 and 21–23, and that the cart never reserves stock.
     - **The job:** `jobs=(ScheduledJob("orders_purge_carts", lambda s:
       s.get(PurgeCarts).run_once(), 60, 120),)` on the `AppModule`.
     - **Wiring** (`module.py`):
       - `catalog = CatalogOffersAdapter(services.get(PresentationOffers))`;
       - register the commands, the query, the cookie and `PurgeCarts`.
     - **`.importlinter`:** add the two ignore lines,
       `orders.infrastructure.catalog_offers -> catalog` and `orders.module -> catalog`.
     - Run `uv run just openapi`.
   - Observable result: a browser session can add, change, remove and acknowledge, keeping the
     cart across requests through the cookie.

5. **Docs and tests**
   - Files: `docs/architecture.md` (modify), `apps/api/tests/unit/orders/` (modify),
     `apps/api/tests/unit/catalog/` (modify), `apps/api/tests/integration/orders/` (modify),
     `apps/api/tests/integration/catalog/` (modify)
   - Do:
     - **`docs/architecture.md`:** the orders row gets the cart, and the catalog row gets the
       `PresentationOffers` facade.
     - **Tests:** the tester writes the layered tests. The orders integration fixture truncates
       the new tables before and after each test.
   - Observable result: `uv run just check` and `uv run just test-integration` are green.

6. **Repair round (review findings 1 to 3)**
   - Files: `apps/api/src/fragancia_api/modules/orders/application/commands/cart.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_cart_repository.py` (modify),
     `apps/api/src/fragancia_api/modules/orders/contracts.py` (modify),
     `apps/api/openapi.json` (modify),
     `apps/api/tests/unit/orders/test_cart_application.py` (modify),
     `apps/api/tests/unit/orders/test_cart_http.py` (modify),
     `apps/api/tests/integration/orders/test_sql_carts.py` (modify)
   - Do: see `## Repair log`.

## Acceptance criteria

Run with the API on the development database, using the QA perfumes from the inventory plans.

- [ ] **Empty.** `GET /api/v1/cart` with no cookie returns an empty cart (`lines []`,
  `subtotal_cents 0`) and sets no cookie.
- [ ] **Add.**
  - `PUT /api/v1/cart/lines/{id}` with quantity 2, for a visible in-stock presentation with
    enough units, returns the line `ok` with the current unit price and line total.
  - The response sets the `fragancia_cart` cookie (httpOnly, `SameSite=Lax`, path `/api/v1`).
  - A following `GET` with the cookie returns the same cart.
- [ ] **Limits.**
  - Quantity 11 answers 422 `ORDERS_CART_QUANTITY_INVALID`.
  - More than the available units answers 409 `ORDERS_CART_STOCK_UNAVAILABLE`, with
    `details.available`.
  - A made-to-order presentation accepts up to 10 regardless of stock.
  - A hidden or unknown presentation answers 409 `ORDERS_CART_PRESENTATION_UNAVAILABLE`.
- [ ] **Price change.**
  - After the catalog price of a line changes, `GET` shows the new unit price and
    `price_changed_from_cents` = the old one.
  - `POST /acknowledge-prices` clears it.
  - Changing the quantity also clears it.
- [ ] **Line goes bad.**
  - When a line's presentation sells out, it shows `sold_out`.
  - When its perfume is hidden, it shows `unavailable`.
  - Either way the line is excluded from `subtotal_cents`, and `has_problems` is true.
  - When the stock comes back, it is `ok` again.
- [ ] **Lead time.** A made-to-order line sets `lead_time_max_days` to its maximum lead time.
- [ ] **Remove.** `DELETE` removes the line. Removing an absent line is a no-op.
- [ ] **Purge.** A cart whose `updated_at` is 31 days old is deleted by the worker job, with its
  lines.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `Cart` transitions (bounds 1/10/11, 30 lines, acknowledge, remove absent); `cart_view` statuses at each boundary (0, < qty, = qty available; invisible; unknown), subtotal over ok lines only, price-changed detection, lead time over ok made-to-order lines |
| application | yes     | Each command: new cart and token, unknown token, refusals, the quantity change acknowledging the price; `GetCart` empty without write; `PurgeCarts` cutoff and limit; `PresentationOffers` facade (in-memory) |
| http        | yes     | The cookie attributes on create and refresh, no cookie on an empty `GET`, every status code, OpenAPI |
| integration | yes     | `SqlPresentationOffers` (sale window, visibility, photo URL, stock copy, made-to-order None); the cart repository and reader; FK cascade; the quantity check constraint; the purge job; the migration round trip on a scratch database |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

- **Step 3, `Offer` placement (cosmetic).** The plan puts orders' frozen `Offer` in
  `application/ports.py`. `cart_view` is a domain function that takes offers, and the domain
  cannot import the application layer, so `Offer` lives in `domain/cart.py` and the ports
  import it from there.
- **Step 3, outcome type (cosmetic).** The commands return `Result[CartOutcome, DomainError]`,
  where `CartOutcome(view, token)` carries the `CartView` contract and the token of the cart
  that exists (new or the caller's), `None` when there is no cart. The router sets the cookie
  whenever `token` is set, which gives the "refreshed on every write" behaviour.
- **Not exercised against the database.** The SQL adapters (`SqlPresentationOffers`,
  `SqlCartRepository`, `SqlCartReader`) ran only through the migration and the existing suites;
  the layered tests and the verification cover them.

## Test coverage

Baseline (`uv run just check`, before any test): green (2649 passed, 3 skipped). Closing of the
first testing phase: `uv run just check` green (2784 passed, 3 skipped) and `uv run just
test-integration` green (801 passed, 5 skipped; none of the skips are mine). **That evidence is
superseded for the code the repair round (2026-10-16) touched: `application/commands/cart.py`,
`infrastructure/sql_cart_repository.py` and `contracts.py`; see "Repair round" below.** Added: 135 unit tests (domain 43,
application 36, http 31, wiring/adapters 14, catalog facade 11) and 50 integration tests
(carts and use cases 25, migration 12, catalog SQL facade 13). No GAP and no NOT CONFIRMED:
every plan behavior has code behind it. Files under `apps/api/tests/`: `unit/orders/`
`test_cart_domain.py`, `test_cart_application.py`, `test_cart_http.py`, `test_cart_wiring.py`
(+ builders in `support.py`), `unit/catalog/test_presentation_offers.py`,
`integration/orders/` `test_sql_carts.py`, `test_migration_0018_orders_carts.py` (+ `conftest.py`
truncates `orders.carts` and shares the scratch-database fixture),
`integration/catalog/test_sql_presentation_offers.py`.

| Behavior | Source | Layer | Test | State |
| -------- | ------ | ----- | ---- | ----- |
| Quantity 1..10 accepted, 0/-1/11/100 refused with `details {min 1, max 10}` | `domain/cart.py` `set_line` | domain | `test_cart_domain.py::test_set_line_accepts_a_quantity_from_1_to_10`, `::test_set_line_rejects_a_quantity_outside_1_to_10` | CONFIRMED |
| 30 lines allowed, 31st new line refused, existing line still editable when full | `cart.py` `set_line` | domain | `test_set_line_allows_exactly_30_lines`, `test_set_line_refuses_a_new_line_past_30`, `test_a_full_cart_still_accepts_a_quantity_change_on_an_existing_line` | CONFIRMED |
| Changing quantity acknowledges the price (decision 23); `added_at` kept | `cart.py` `set_line` | domain | `test_changing_a_quantity_acknowledges_the_current_price_and_keeps_the_added_at` | CONFIRMED |
| `remove_line` drops it; absent line is a no-op (no `updated_at` stamp) | `cart.py` `remove_line` | domain | `test_remove_line_drops_the_line_and_stamps_the_change`, `test_remove_line_of_an_absent_presentation_changes_nothing` | CONFIRMED |
| `acknowledge_prices` for lines present in the mapping only | `cart.py` `acknowledge_prices` | domain | `test_acknowledge_prices_*` | CONFIRMED |
| Line status: unknown/invisible = unavailable, 0 = sold_out, < qty = insufficient_stock, = qty ok, made to order always ok | `cart.py` `_status` | domain | `test_an_in_stock_line_is_classified_by_available_units_against_its_quantity` (7 boundary cases) + 4 others | CONFIRMED |
| Subtotal, item_count and lead time over ok lines only; `has_problems`; price_changed_from | `cart.py` `cart_view` | domain | `test_the_subtotal_and_item_count_add_up_only_the_ok_lines`, `test_the_lead_time_*`, `test_a_changed_price_*` | CONFIRMED |
| New cart and token, only the digest stored; same token on later writes | `commands/cart.py` `SetCartLine` | application | `test_cart_application.py::test_the_first_line_creates_a_cart_and_a_token_stores_only_its_digest`, `::test_a_second_line_goes_in_the_callers_cart_and_returns_the_same_token` | CONFIRMED |
| Unknown token gets a new cart | `SetCartLine` | application | `::test_an_unknown_token_gets_a_new_cart_and_a_fresh_token` | CONFIRMED |
| Unknown/hidden presentation refused; stock refused with `details.available` (negative copy reads 0); exactly-available accepted; made to order up to 10 | `SetCartLine` | application | `::test_an_unknown_presentation_is_refused_*`, `::test_a_hidden_*`, `::test_more_units_than_available_*`, `::test_a_negative_stock_copy_*`, `::test_made_to_order_accepts_up_to_ten_*` | CONFIRMED |
| Quantity and line-count refusals leave nothing stored | `SetCartLine` | application | `::test_a_quantity_of_eleven_*`, `::test_a_thirty_first_line_is_refused` | CONFIRMED |
| Remove / acknowledge with no token or unknown token: empty view, no cookie token, no write | `RemoveCartLine`, `AcknowledgeCartPrices` | application | `::test_removing_*`, `::test_acknowledging_*` | CONFIRMED |
| `GetCart` computes current view; empty without write for no/unknown token | `queries/cart.py` | application | `::test_getting_*` | CONFIRMED |
| `PurgeCarts`: older than 30 days deleted, exact cutoff kept, batch of 500 | `commands/cart.py` `PurgeCarts` | application | `::test_the_purge_*`, `::test_a_cart_updated_exactly_at_the_cutoff_is_kept` | CONFIRMED |
| `PresentationOffers`: sale window (start inclusive, end exclusive), visibility (unpublished, archived, inactive presentation, inactive brand), photo, units, made to order None, unknown ids absent | `presentation_offers.py`, `in_memory_perfumes.py` | application | `unit/catalog/test_presentation_offers.py` (11) | CONFIRMED |
| Cookie `fragancia_cart`: httpOnly, `SameSite=Lax`, path `/api/v1`, max-age 30 days, Secure by setting; refreshed on write; none on empty GET / absent cart / refused PUT | `http/cart_cookie.py`, `http/router.py` | http | `test_cart_http.py::test_creating_a_cart_sets_the_cookie_with_its_attributes`, `::test_a_write_with_the_cookie_refreshes_it`, `::test_an_empty_get_*`, `::test_a_refused_put_sets_no_cookie_*` | CONFIRMED |
| 200/409/422 per rule with error `code` and `details`; malformed body and bad uuid 422; no session needed | `router.py` | http | `test_cart_http.py::test_a_quantity_of_eleven_answers_422_*`, `::test_more_than_the_available_units_answers_409_*`, `::test_a_hidden_or_unknown_*`, `::test_a_malformed_body_*`, `::test_the_cart_routes_need_no_session` | CONFIRMED |
| Price change, acknowledge, sold out / hidden, remove through HTTP; OpenAPI declares the operations | `router.py`, `contracts.py` | http | `::test_a_price_change_shows_*`, `::test_a_line_that_sells_out_*`, `::test_delete_removes_*`, `::test_the_openapi_document_declares_the_cart_operations` | CONFIRMED |
| Container resolves the cart services; cookie Secure only in production; the job is scheduled (60/120) and runs the purge; token and digest (SHA-256); catalog adapter maps every field | `module.py`, `cart_tokens.py`, `catalog_offers.py` | application | `test_cart_wiring.py` (14) | CONFIRMED |
| `SqlPresentationOffers`: sale window, visibility, first photo URL, stock copy, made to order None, unknown/empty ids | `sql_presentation_offers.py` | integration | `integration/catalog/test_sql_presentation_offers.py` (13) | CONFIRMED |
| Cart repository and reader: round trip, line order, save replaces lines, rollback keeps nothing, row lock serializes writers | `sql_cart_repository.py` | integration | `integration/orders/test_sql_carts.py` | CONFIRMED |
| Unique token digest, quantity check 0/-1/11 refused and 1/10 accepted, PK per cart+presentation, FK cascade | `tables.py`, migration 0018 | integration | `test_sql_carts.py::test_the_database_rejects_*`, `::test_deleting_a_cart_cascades_*` | CONFIRMED |
| Purge: cutoff, oldest first up to limit, lines cascade; job runs it (31-day cart deleted, 29-day kept) | `delete_untouched_before`, `module.py` job | integration | `test_sql_carts.py::test_delete_untouched_before_*`, `::test_the_purge_job_*`, `::test_the_scheduled_orders_job_*` | CONFIRMED |
| Full flow on real adapters and real catalog: add, over-stock refusal with `available`, made to order lead time, price change then acknowledge, sold out then back | `commands/cart.py` over SQL | integration | `test_sql_carts.py::test_the_whole_cart_flow_*`, `::test_a_price_change_shows_*`, `::test_a_line_that_sells_out_*` | CONFIRMED |
| Migration 0018 round trip on a scratch database: absent before, columns/types/lengths, `updated_at` index, constraints, downgrade drops only the cart tables | `migrations/versions/0018_orders_carts.py` | integration | `test_migration_0018_orders_carts.py` (12) | CONFIRMED |

### Repair round (2026-10-16)

Baseline: the implementer's green run after the repair (2787 passed unit, 802 integration).
Closing: `uv run just check` green (2790 passed, 3 skipped; harness 685 passed) and
`uv run just test-integration` green (802 passed, 5 skipped). Four regression tests came with the
repair; this phase added 3 unit tests (the `AcknowledgeCartPrices` window) and hardened one
integration test. No GAP and no NOT CONFIRMED.

| Behavior | Source | Layer | Test | State |
| -------- | ------ | ----- | ---- | ----- |
| `SetCartLine` reads the catalog outside the transaction | `commands/cart.py` `SetCartLine` | application | `test_cart_application.py::test_setting_a_line_reads_the_catalog_outside_the_transaction` | CONFIRMED |
| `AcknowledgeCartPrices` reads the catalog outside the transaction | `commands/cart.py` `AcknowledgeCartPrices` | application | `::test_acknowledging_reads_the_catalog_outside_the_transaction` | CONFIRMED |
| A line added between listing the ids and locking keeps the price its buyer saw (still flagged `price_changed_from`); the listed line is acknowledged | `AcknowledgeCartPrices`, `cart.py` `acknowledge_prices` | application | `::test_a_line_added_while_acknowledging_keeps_the_price_its_buyer_saw` (catalog fake mutates the stored cart during the read) | CONFIRMED |
| A line removed in that window is ignored; the remaining line is acknowledged | `AcknowledgeCartPrices` | application | `::test_a_line_removed_while_acknowledging_is_ignored` | CONFIRMED |
| A cart purged in that window answers the empty cart, no cookie, no write | `AcknowledgeCartPrices` | application | `::test_a_cart_purged_while_acknowledging_answers_the_empty_cart_without_a_cookie` | CONFIRMED |
| A purge waiting on a cart touched meanwhile keeps it (outer `WHERE` repeats the age test). The test first polls `pg_stat_activity` until the `DELETE` is blocked on a `Lock` wait and fails with a clear message otherwise, so it cannot pass vacuously | `sql_cart_repository.py` `delete_untouched_before` | integration | `test_sql_carts.py::test_a_purge_waiting_on_a_cart_that_is_touched_meanwhile_keeps_it` (helper `wait_until_a_delete_waits_on_a_lock`) | CONFIRMED |
| `CartLineView` text: hidden or archived keeps catalog fields and price; only a vanished presentation leaves them `null` | `contracts.py`, `openapi.json` | http | `test_cart_http.py::test_the_line_contract_describes_hidden_lines_as_keeping_their_catalog_fields` | CONFIRMED |

Not exercised by tests: the SQL `Secure` cookie flag behind a real HTTPS proxy; a failure of the
view's catalog read after the commit (review Info 3: a 500 after a stored write, accepted); the
older row-lock test still waits a fixed `WAIT` instead of polling the lock. Races beyond one
cart row are not promised by the plan.

## Review findings

Reviewed 2026-10-16, diff `main..HEAD` on `feat/orders-cart` (clean worktree).

**Checklist: 14/15 passed, 1 not applicable, 0 failed.**

- [x] `plans-scope`: every change is inside the plan. Hot file `.importlinter`: append-only (the
  two planned ignore lines at the end). `container.py` and `docs/modules.json` are untouched.
- [x] `uv run just check` green (2784 passed, 3 skipped; harness 685 passed; OpenAPI drift test
  green).
- [x] `uv run just test-integration` green (801 passed, 5 skipped).
- [x] Business rules are in `domain/cart.py` (bounds, line statuses, totals). The visibility
  and stock refusals live in `SetCartLine`, as step 3 prescribes. Routers, mappers and adapters
  hold no rules.
- [x] CQRS-lite: the commands run in `transactions.run` and return `Result`. `GetCart` reads
  through `CartReader` (`Database.reader()`) and returns the contract. The repository has no
  screen-specific methods.
- [x] Contracts live in `orders/contracts.py`; `openapi.json` matches the generator
  (`test_committed_document_is_up_to_date`).
- [x] Errors are `Err(DomainError)` with codes `ORDERS_CART_*`; the messages are in Spanish.
- [x] Money is integer cents with no `float`. The cart domain uses raw `int` cents rather than
  `Money`, as the plan's field names prescribe; the only arithmetic is a multiply and a sum.
  Time comes from `Clock` and ids from `new_id()`.
- [x] Migration 0018 is new. It has no drops, no cross-schema FK (`presentation_id` has no FK,
  with a comment), the schema already exists, and the downgrade works (12 round-trip tests).
- [x] Routes use `public_router(prefix="/cart")`; there is no bare `APIRouter` and no admin route.
- [x] Wiring resolves (`test_container.py`, `test_cart_wiring.py`). Adapters are created only
  in `module.py`. The catalog is registered before orders (`services.get(PresentationOffers)`
  works).
- [x] No secrets or personal data in code, tests or the plan.
- [x] `## Deviations` is honest. Spot check: `Offer` really lives in `domain/cart.py:20-35`, and
  `application/ports.py` imports it from there.
- [x] Docs: `docs/architecture.md` rows updated. `apps/api/README.md`, the recipes and
  `docs/modules.json` have nothing stale.
- [ ] N/A: the PR body. No PR exists yet for `feat/orders-cart` (`gh pr list` is empty), so
  check it when the PR opens.

**Findings: 0 high, 1 medium, 2 low.** Each needs a code change, so the status stays `review`.

### Medium

1. **Pool starvation: a second connection is taken while a row lock is held.**
   - Where: `apps/api/src/fragancia_api/modules/orders/application/commands/cart.py:56-66`
     (`SetCartLine`) and `:169-173` (`AcknowledgeCartPrices`).
   - What fails: inside `transactions.run`, the command first locks the cart
     (`get_by_token_for_update`, on the unit-of-work connection). It then calls
     `self._offers.offers(...)`. That reaches `SqlPresentationOffers.offers`, which opens
     `Database.reader()`
     (`modules/catalog/infrastructure/sql_presentation_offers.py:85`), a **second** pooled
     connection, while the first one stays checked out with the lock held. The engine uses
     SQLAlchemy's default pool (`shared/infrastructure/database.py:22`: 5 connections plus 10
     overflow, 30 s timeout).
   - Scenario: 15 buyers with existing carts each press "+" on the same page during a promotion.
     Every request holds one connection and waits for a second. The pool is empty, so all of them
     (and every other API request) block for 30 s and then fail with `TimeoutError` / 500.
   - Precedent: inventory's in-transaction path avoids this with `find_for_update` on the active
     session (`sql_presentation_directory.py:65-72`).
   - Fix options, for the implementer to choose:
     - read the offers before opening the transaction. `RemoveCartLine` already computes its
       view after the commit;
     - or make the catalog adapter join the active session when there is one.
   - Confidence: high on the mechanism. Not reproduced under load.

### Low

2. **The purge can delete a cart that was just written.**
   - Where: `apps/api/src/fragancia_api/modules/orders/infrastructure/sql_cart_repository.py:95-104`.
   - What fails: `DELETE FROM carts WHERE id IN (SELECT id … WHERE updated_at < cutoff … LIMIT
     500)` puts the age condition only in the subquery. Under READ COMMITTED, when the DELETE
     waits on a row that a concurrent `SetCartLine` holds `FOR UPDATE`, PostgreSQL's re-check
     after that commit re-evaluates the outer WHERE only (`id IN (<ids already computed>)`). It
     does not re-check the new `updated_at`, so the cart that was just touched is deleted anyway.
   - Scenario: a buyer comes back on day 30 and adds a line while the minute's purge runs. The
     PUT answers 200 with the line and refreshes the cookie, and the next `GET` shows an empty
     cart.
   - Fix: repeat `carts.c.updated_at < cutoff` in the outer DELETE's WHERE, or use
     `FOR UPDATE SKIP LOCKED` in the subquery.
   - Confidence: medium. This is PostgreSQL EvalPlanQual behaviour for a target row joined to an
     already-evaluated subquery. It is not reproduced; a two-connection integration test would
     confirm it.

3. **The contract text says the opposite of what the code does for hidden lines.**
   - Where: `apps/api/src/fragancia_api/modules/orders/contracts.py:70-74` (and its copy in
     `apps/api/openapi.json`, the `CartLineView` description).
   - What fails: the text says that for `unavailable` lines ("unknown, archived or hidden") "the
     catalog fields are `null`". But `cart_view` (`domain/cart.py`) fills label, slug, photo,
     price and line total for any line whose offer exists but is not visible. Only lines with no
     offer get nulls. `test_cart_domain.py:246`
     (`test_an_invisible_offer_is_unavailable_but_keeps_its_facts_and_price`) asserts the code's
     behaviour.
   - Scenario: the phase-3 web client trusts the published contract. It renders an archived
     perfume's line as "producto desconocido" with no name, or it skips the null checks only for
     unknown lines and shows a price where it expected none.
   - Fix: correct the docstring to "null only when the presentation no longer exists" (or change
     the behaviour, if nulls were intended), then run `uv run just openapi`.

No out-of-scope findings were filed.

### Re-review after the repair round (2026-10-16)

The first review above is superseded for `application/commands/cart.py`,
`infrastructure/sql_cart_repository.py` and `contracts.py`. This round covers the repair commit
`d2527b6` on `feat/orders-cart` (clean worktree). It also re-runs the whole checklist.

**Checklist: 14/15 passed, 1 not applicable, 0 failed.**

- [x] `plans-scope`: every change is inside the plan. `.importlinter` is still append-only (the
  repair did not touch it).
- [x] `uv run just check` green (2787 passed, 3 skipped; harness 685 passed).
- [x] `uv run just test-integration` green (802 passed, 5 skipped).
- [x] Rules stay in `domain/cart.py`. The visibility and stock refusals moved out of
  `work()` but stay in `SetCartLine`, as step 3 prescribes.
- [x] CQRS-lite holds: the writes run in `transactions.run` and return `Result`.
- [x] Contracts: `openapi.json` was regenerated (drift test green). The new text matches
  `cart_view`.
- [x] Error codes are unchanged, and so is the refusal order (unavailable, then stock, then
  quantity or line count).
- [x] Money, time and ids: no change.
- [x] Migrations: none in the repair.
- [x] Routes: no change.
- [x] Wiring: no change; `test_container.py` is green.
- [x] No secrets.
- [x] Deviations and the repair log are honest. Spot check: no `self._offers.offers` call is
  left inside a `work()` closure (`commands/cart.py:56`, `:97`, `:188`, `:205`).
- [x] Docs: nothing stale. The architecture rows do not describe the transaction boundary.
- [ ] N/A: the PR body. There is still no PR for `feat/orders-cart`.

**The three earlier findings are fixed:**

1. **Pool starvation (Medium): fixed.** `SetCartLine` and `AcknowledgeCartPrices` no longer read
   the catalog while the unit-of-work connection is checked out. `GetCart` and `RemoveCartLine`
   were already sequential, and `SqlPresentationOffers` releases its reader before returning.
   The regression tests are `test_cart_application.py:577` and `:586`. They count catalog reads
   made while the runner's window is open. The pre-repair code called `offers()` inside
   `work()`, so both would fail on it (checked by reading the old code, not by re-running it).
2. **Purge (Low): fixed.** The outer `DELETE … WHERE` now repeats `updated_at < cutoff`. The
   regression test is `test_sql_carts.py:524`. It uses two connections and asserts
   `purged == Ok(0)` with the cart kept with two lines. Without the fix, the EvalPlanQual
   re-check would delete the cart, so the test would fail. The implementer reports running it
   red without the fix; I did not re-run that.
3. **Contract text (Low): fixed.** `contracts.py:71-75` and `openapi.json` agree with
   `cart_view`. The regression test is `test_cart_http.py`
   (`test_the_line_contract_describes_hidden_lines_as_keeping_their_catalog_fields`), which
   asserts phrases the old text did not contain.

**Checking the repair for new defects** (stale offers, races, lock order):

- `SetCartLine`: the offer is read before the lock. A price change in that window stores the
  old seen price, and the after-commit view then flags `price_changed_from`. That is visible
  and safe. The window already existed, because catalog rows are never locked. Stock is still
  not reserved, as planned.
- `AcknowledgeCartPrices`:
  - A line added between step 1 and step 3 is left alone (`acknowledge_prices` uses
    `.get(pid, seen)`).
  - A line removed in between is ignored.
  - A cart purged in between returns the empty cart with no cookie.
  - The step-2 offers can be older than a concurrent `SetCartLine`'s price. In that case the
    acknowledgement writes the older price back, and the view flags the change again. That is
    a benign extra flag; a change is never hidden.
- Lock order: each command locks at most one cart row and never two at once (step 1's lock is
  released at its commit). No new deadlock path.

**New findings: 0 high, 0 medium, 1 low, 2 info.** The low finding is test-only, so it goes to
the tester, not the implementer. The status goes to `review` and stays there until it is
closed.

#### Low

1. **The testing phase for the repaired code is incomplete.**
   - Where: `## Test coverage` in this plan, and `test_cart_application.py`.
   - What fails, part 1: `## Test coverage` was not updated for the repair.
     - It still cites 2784/801 passing.
     - It has no rows for the four regression tests.
     - Its "Not exercised" note still says no multi-connection race is covered, which is no
       longer true.
     - It does not mark the old evidence as superseded for the touched code.

     The workflow requires `## Test coverage` to be filled before `review`. The plan reached
     this review in `testing`.
   - What fails, part 2: a behaviour the repair log asserts has no test. "A line added in
     between simply keeps its seen price" is the new step-1/step-3 interleaving of
     `AcknowledgeCartPrices`. A line removed or a cart purged in that window is not tested
     either.
   - Scenario: a later change gives `acknowledge_prices` an explicit-mapping overwrite. A line
     added in the window would then get a price nobody read for it, and no test would catch it.
     The in-memory fakes make this easy to test, for example with a `WatchedOffers` that
     mutates the stored cart during `offers()`.
   - To close it, the tester should:
     - add those cases;
     - update `## Test coverage`, keeping the dated baseline and marking it superseded for the
       touched code;
     - move the plan on.

#### Info (no action required)

2. **The purge regression test can pass without the fix on a slow host.**
   - Where: `test_sql_carts.py:554`.
   - It sleeps `WAIT` (0.5 s) and then checks only that the purge is not done. It does not
     check that the purge is waiting on the row lock. If the purge's `DELETE` has not started
     before `release`, it runs after the touch commits, and it passes with or without the
     outer clause. It can give a false pass but never a false failure. The pattern is the same
     as the existing lock test at `:360`.
   - Optional hardening: poll `pg_stat_activity.wait_event_type = 'Lock'` before releasing.
3. **A failure after commit is now a 500 after a stored write.**
   - Where: `commands/cart.py:96-101` and `:204-208`.
   - If the view's catalog read raises after the commit, the write is kept but the response is
     a 500. For a new cart, the cookie is never sent, so the cart is orphaned until the 30-day
     purge. A retry is harmless because setting a quantity is idempotent, and `RemoveCartLine`
     already worked this way.
   - Uncertain whether it matters: a database read failing right after a commit on the same
     database is improbable.

Also noted, with no action: step 1 of `AcknowledgeCartPrices` (`commands/cart.py:174-178`)
takes a `FOR UPDATE` lock only to list ids. `CartReader.by_token` would do without the lock.
The lock is harmless because it is released at its own commit.

No out-of-scope findings were filed.

**Closed (2026-10-16, main session).** Low 1 is resolved: the tester added the three
acknowledge-window tests and updated `## Test coverage` (repair-round subsection). Info 2 is
resolved: the purge regression test now waits until `pg_stat_activity` shows the `DELETE`
blocked on a lock. Info 3 is left as is (retry is safe; `RemoveCartLine` already behaves so).
The new changes are tests and plan text only, so no further review round; moved to `verify`.

## Repair log

Repair round 2026-10-16, requested by the main session for the three review findings above.
The plan was in `review` when dispatched (the main session had not moved it to `implementing`);
I treated the explicit repair instruction as that transition. The review and any earlier
verification above are superseded for the touched code and must be repeated.

1. **Pool starvation (Medium), fixed.** `SetCartLine` now reads the one offer it validates
   before the transaction, and reads the view's offers after the commit. `AcknowledgeCartPrices`
   runs three steps: a short transaction that lists the cart's presentation ids, the catalog
   read outside any transaction, then a transaction that locks and acknowledges. A line added in
   between simply keeps its seen price. No catalog read now happens while the row lock is held.
   Regression: `test_cart_application.py::test_setting_a_line_reads_the_catalog_outside_the_transaction`
   and `::test_acknowledging_reads_the_catalog_outside_the_transaction`.
2. **Purge deleting a just-written cart (Low), fixed.** The outer DELETE repeats
   `updated_at < cutoff`. Regression: `test_sql_carts.py::test_a_purge_waiting_on_a_cart_that_is_touched_meanwhile_keeps_it`
   (two connections). Confirmed it fails without the fix (temporarily removed, then restored) and
   passes with it.
3. **Contract text (Low), fixed.** `CartLineView` now says a hidden or archived presentation
   keeps its catalog fields and price, and only a presentation that no longer exists leaves them
   `null`; `openapi.json` regenerated with `uv run just openapi`. Regression:
   `test_cart_http.py::test_the_line_contract_describes_hidden_lines_as_keeping_their_catalog_fields`.

Commands: `uv run just check` green (2787 passed, 3 skipped; harness 685 passed);
`uv run just test-integration` green (802 passed, 5 skipped).

## Verification

2026-10-16, main session, live against the development database (API on :8100 and the arq
worker running, at migration 0018). QA script: `scratchpad/qa_inv/qa_cart.py`. It used the QA
perfume `qa-versace-qa-confirma-edp`: 50 ml in stock (6 available), 100 ml (1), 75 ml (0) and
200 ml made to order (10 days). Every catalog value it changed was restored afterwards and
checked by SQL.

- [x] **Empty.** `GET /cart` without a cookie: 200, `lines []`, `subtotal_cents 0`, no
  `Set-Cookie`.
- [x] **Add.** `PUT /cart/lines/{50 ml}` with 2: 200, line `ok`, unit 189950, total 379900.
  `Set-Cookie: fragancia_cart=…; HttpOnly; Max-Age=2592000; Path=/api/v1; SameSite=lax` (no
  `Secure` in development, as planned). A following `GET` with the cookie returned the same cart.
- [x] **Limits.**
  - Made-to-order quantity 11 and quantity 0: 422 `ORDERS_CART_QUANTITY_INVALID`,
    `{"min": 1, "max": 10}`. Made-to-order quantity 10 with 0 stock: 200 `ok`.
  - 100 ml × 2 with 1 available: 409 `ORDERS_CART_STOCK_UNAVAILABLE`, `{"available": 1}`;
    75 ml × 1 with 0 available: 409, `{"available": 0}`.
  - Unknown presentation: 409 `ORDERS_CART_PRESENTATION_UNAVAILABLE`. A hidden perfume takes the
    same path (the facade reports it not visible).
  - Observed: 50 ml × 11 with 6 available answers 409 `ORDERS_CART_STOCK_UNAVAILABLE`
    (`{"available": 6}`), not 422, because the stock check runs before the quantity bound. Both
    refuse, and the stock message is the more useful one. Quantity 11 answers 422 whenever the
    stock does not already refuse it.
- [x] **Price change.** The 100 ml price raised by 10000: `GET` showed unit 259900 and
  `price_changed_from_cents` 249900. `POST /acknowledge-prices` cleared it. Raised again: flagged
  259900 → 269900; setting the quantity again cleared it. Restoring the price flags it again
  (seen 269900), as expected.
- [x] **Line goes bad.** 50 ml stock copy set to 0: the line was `sold_out`, the subtotal dropped
  it (3629800 → 3249900) and `has_problems` was true; with the stock back it was `ok` and the
  subtotal 3629800 again. The perfume unpublished: every line `unavailable`, subtotal 0,
  `has_problems` true, and the lines kept `perfume_slug` and `label`; published again, all `ok`.
- [x] **Lead time.** With the made-to-order line, `lead_time_max_days` was 10; after removing it,
  `null`.
- [x] **Remove.** `DELETE` of the made-to-order line: 200 without it; deleting it again: 200,
  unchanged.
- [x] **Purge.** The cart's `updated_at` set 31 days back (2 lines): within one minute the
  worker's `orders_purge_carts` deleted the cart and its lines (0 and 0 rows). A `GET` with the
  old cookie then answered an empty cart.

NOT VERIFIED live: the `Secure` cookie flag behind HTTPS in production; pool behaviour under
concurrent load (covered only by the unit test of the call order).
