"""Inventory: stock per in-stock presentation, an append-only ledger of movements, low-stock
thresholds and reservations (units held while a customer pays).

Events (published through the outbox; other modules subscribe by name, never by import):

- `inventory.stock.changed`: after every movement, every threshold change, every reservation
  change (reserve, commit, release, expiry) and, for every stock item, every change of the
  global threshold. Payload: `presentation_id` (UUID string), `available` (int; on hand minus
  reserved, never below 0), `threshold` (int; the effective one) and `as_of` (ISO datetime of
  the change; consumers keep the newest).

Inventory subscribes to `catalog.import.applied` (by name) and records one `count` movement per
entry of its `stock_counts`, about one relay cycle after the import is confirmed.

Public API for other modules:
- `StockReservations`: reserve units of one or more presentations until an expiry the caller
  gives, extend, commit (a `sale` movement per presentation) or release them. Its docstring
  states the rules.

The composition root wires `module.module`.
"""

from fragancia_api.modules.inventory.application.stock_reservations import StockReservations

__all__ = ["StockReservations"]
