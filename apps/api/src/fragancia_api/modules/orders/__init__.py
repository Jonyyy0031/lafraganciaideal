"""Orders: delivery options (home, local, pickup), the server-side cart and, in later plans, the
checkout and the order lifecycle.

Events (published through the outbox; other modules subscribe by name, never by import):

- `orders.order.placed`: when the checkout creates an order. Payload: `order_id` (UUID string)
  and `number` (int; the order is shown as `LFI-<number>`).

The checkout (`POST /api/v1/checkout`) turns the cart into an order in `pending_payment`: it
reserves the in-stock units for 30 minutes through inventory's `StockReservations`, takes the
next gapless number, freezes the prices and empties the cart.

Public API for other modules: nothing yet.

The cart is identified by the `fragancia_cart` cookie, shows current prices and availability, and
never reserves stock. The worker job `orders_purge_carts` deletes carts untouched for 30 days.

The composition root wires `module.module`.
"""
