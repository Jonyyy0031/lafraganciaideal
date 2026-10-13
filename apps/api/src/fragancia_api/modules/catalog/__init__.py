"""Catalog: brands, olfactory families, concentrations, perfumes and their presentations (sizes
with price, sale and availability).

REFERENCE MODULE — new modules copy its shape by name (docs/recipes/new-module.md).
Public API for other modules:
- `PresentationDirectory` (+ `PresentationRef`): look presentations up by id, or list the
  in-stock ones, with the names that identify them.
The composition root wires `module.module`.
"""

from fragancia_api.modules.catalog.application.queries.presentation_directory import (
    PresentationDirectory,
    PresentationRef,
)

__all__ = ["PresentationDirectory", "PresentationRef"]
