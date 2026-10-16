"""Catalog: brands, olfactory families, concentrations, perfumes and their presentations (sizes
with price, sale and availability).

REFERENCE MODULE — new modules copy its shape by name (docs/recipes/new-module.md).
Public API for other modules:
- `PresentationDirectory` (+ `PresentationRef`): look presentations up by id, or list the
  in-stock ones, with the names that identify them.
- `PresentationOffers` (+ `PresentationOffer`): what a buyer sees of presentations, priced at a
  given moment: label, slug, card photo, effective and regular price, availability, lead times,
  visibility and the available units (from the stock copy).

Events (published through the outbox; other modules subscribe by name, never by import):

- `catalog.import.applied`: once per applied import, in the confirm's transaction. Payload:
  `import_id` (UUID string), `actor_id` (the confirming user) and `stock_counts` (an object
  from presentation id string to its physical count; empty when the file had no counts).

The composition root wires `module.module`.
"""

from fragancia_api.modules.catalog.application.queries.presentation_directory import (
    PresentationDirectory,
    PresentationRef,
)
from fragancia_api.modules.catalog.application.queries.presentation_offers import (
    PresentationOffer,
    PresentationOffers,
)

__all__ = ["PresentationDirectory", "PresentationOffer", "PresentationOffers", "PresentationRef"]
