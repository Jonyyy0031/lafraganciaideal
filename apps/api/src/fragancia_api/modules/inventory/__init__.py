"""Inventory: stock per in-stock presentation, an append-only ledger of movements and low-stock
thresholds.

Events (published through the outbox; other modules subscribe by name, never by import):

- `inventory.stock.changed`: after every movement, every threshold change and, for every stock
  item, every change of the global threshold. Payload: `presentation_id` (UUID string),
  `available` (int; on hand for now), `threshold` (int; the effective one) and `as_of` (ISO
  datetime of the change; consumers keep the newest).

Public API for other modules: nothing yet. The composition root wires `module.module`.
"""
