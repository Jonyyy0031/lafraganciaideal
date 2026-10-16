---
status: approved
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

## Test coverage

## Review findings

## Verification
