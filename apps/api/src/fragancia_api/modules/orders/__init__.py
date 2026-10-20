"""Orders: delivery options (home, local, pickup), the server-side cart, the checkout and the
order lifecycle in the panel.

Events (published through the outbox; other modules subscribe by name, never by import):

- `orders.order.placed`: when the checkout creates an order. Payload: `order_id` (UUID string)
  and `number` (int; the order is shown as `LFI-<number>`).
- `orders.order.status_changed`: on every move of an order (paid, preparing, shipped, ready for
  pickup, delivered, cancelled) and when a refund is marked done (then `from_status` and
  `to_status` are both `cancelled`). Payload: `order_id` (UUID string), `number` (int),
  `from_status` and `to_status` (status names such as `pending_payment` or `paid`).

Orders itself consumes both events in the worker to send plain-text emails (decisions 44 to 46):
the customer's "pedido recibido" and one email per status (none for the refund mark), and a
"pedido pagado" email to each active owner. Customer links point to
`{STOREFRONT_URL}/pedido#token=...`.

The lifecycle lives in the panel (`/api/v1/admin/orders`): an order only moves forward
(pending_payment to paid or cancelled; paid to preparing, shipped, ready_for_pickup or
cancelled; preparing to shipped or ready_for_pickup; shipped or ready_for_pickup to delivered).
Marking paid commits the reservation, cancelling an unpaid order releases it and cancelling a
paid one returns the units through inventory and leaves the refund pending. The worker job
`orders_cancel_overdue` cancels unpaid orders past their `payment_due_at`. Permissions:
`orders:manage` (owners and staff) and `orders:finance` (owners: pay, cancel a paid order,
refund).

The checkout (`POST /api/v1/checkout`) turns the cart into an order in `pending_payment`: it
reserves the in-stock units for 30 minutes through inventory's `StockReservations`, takes the
next gapless number, freezes the prices and empties the cart.

Public API for other modules: nothing yet.

The cart is identified by the `fragancia_cart` cookie, shows current prices and availability, and
never reserves stock. The worker job `orders_purge_carts` deletes carts untouched for 30 days.

The composition root wires `module.module`.
"""
