"""Orders: delivery options (home, local, pickup), the server-side cart and, in later plans, the
checkout and the order lifecycle.

Public API for other modules: nothing yet.

The cart is identified by the `fragancia_cart` cookie, shows current prices and availability, and
never reserves stock. The worker job `orders_purge_carts` deletes carts untouched for 30 days.

The composition root wires `module.module`.
"""
