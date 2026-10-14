"""Outbox subscriber that records the stock counts of an applied catalog import.

It runs in the worker, inside the relay's transaction: the relay calls every handler and then
marks the row published and commits (`shared/infrastructure/outbox.py`), so the handler's writes
and the delivery commit together and a committed delivery is never repeated. A malformed payload
raises, so the relay retries it.
"""

from typing import cast
from uuid import UUID

from fragancia_api.modules.inventory.application.commands.stock import RecordImportedCounts
from fragancia_api.shared.application.events import EventMessage
from fragancia_api.shared.kernel import Err

# The event is published by the catalog module; inventory knows it by name only.
IMPORT_APPLIED_EVENT = "catalog.import.applied"


class ApplyImportedCounts:
    def __init__(self, command: RecordImportedCounts) -> None:
        self._command = command

    async def __call__(self, message: EventMessage) -> None:
        payload = message.payload
        actor_id = payload["actor_id"]
        if not isinstance(actor_id, str):
            raise TypeError("actor_id must be a string")
        raw_counts = cast("dict[str, object]", payload["stock_counts"])
        counts = {UUID(key): int(str(value)) for key, value in raw_counts.items()}
        if not counts:
            return
        # The command joins the relay's transaction.
        match await self._command.execute(counts, actor_id):
            case Err(error):
                raise RuntimeError(f"Recording the imported counts failed: {error.code}")
