"""Identity: back-office users, roles and sessions.

Public API for other modules:
- `OwnerDirectory` (+ `OwnerContact`): the active owners (name and email), the only way other
  modules read them.
"""

from fragancia_api.modules.identity.application.queries.owner_directory import (
    OwnerContact,
    OwnerDirectory,
)

__all__ = ["OwnerContact", "OwnerDirectory"]
