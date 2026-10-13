"""Builders shared by the inventory application and http tests."""

from uuid import UUID

from fragancia_api.modules.inventory.application.commands.stock import (
    RecordMovement,
    SetThreshold,
    UpdateInventorySettings,
)
from fragancia_api.modules.inventory.application.ports import StockedPresentation
from fragancia_api.modules.inventory.infrastructure.in_memory import (
    InMemoryPresentationCatalog,
    InMemorySettings,
    InMemoryStock,
)
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner

STOCKED = UUID(int=101)
STOCKED_2 = UUID(int=102)
MADE_TO_ORDER = UUID(int=103)
UNKNOWN = UUID(int=999)
PERFUME = UUID(int=201)


def presentation(
    presentation_id: UUID, label: str, *, stocked: bool = True, sellable: bool = True
) -> StockedPresentation:
    return StockedPresentation(
        presentation_id=presentation_id,
        label=label,
        perfume_id=PERFUME,
        stocked=stocked,
        is_sellable=sellable,
    )


def make_catalog() -> InMemoryPresentationCatalog:
    return InMemoryPresentationCatalog(
        presentation(STOCKED, "Versace Eros EDT 50 ml"),
        presentation(STOCKED_2, "Versace Eros EDT 100 ml"),
        presentation(MADE_TO_ORDER, "Versace Eros EDT 200 ml", stocked=False),
    )


class Inventory:
    """The use cases over one in-memory store."""

    def __init__(self, catalog: InMemoryPresentationCatalog | None = None) -> None:
        self.catalog = catalog or make_catalog()
        self.store = InMemoryStock()
        self.clock = FixedClock()
        settings = InMemorySettings(self.store)
        transactions = InMemoryTransactionRunner()
        self.record = RecordMovement(
            stock=self.store,
            settings=settings,
            catalog=self.catalog,
            transactions=transactions,
            clock=self.clock,
        )
        self.set_threshold = SetThreshold(
            stock=self.store, catalog=self.catalog, transactions=transactions, clock=self.clock
        )
        self.update_settings = UpdateInventorySettings(settings=settings, transactions=transactions)
