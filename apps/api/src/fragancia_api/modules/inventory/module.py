"""Wiring of the inventory module for the composition root."""

from fragancia_api.modules.catalog import PresentationDirectory
from fragancia_api.modules.inventory.application.commands.reservations import (
    CommitReservation,
    ExpireReservations,
    ExtendReservation,
    ReleaseReservation,
    ReserveStock,
)
from fragancia_api.modules.inventory.application.commands.stock import (
    RecordImportedCounts,
    RecordMovement,
    SetThreshold,
    UpdateInventorySettings,
)
from fragancia_api.modules.inventory.application.handlers.import_applied import (
    IMPORT_APPLIED_EVENT,
    ApplyImportedCounts,
)
from fragancia_api.modules.inventory.application.queries.stock import (
    GetInventorySettings,
    GetStock,
    ListMovements,
    ListReservationHolds,
    ListStock,
)
from fragancia_api.modules.inventory.application.stock_reservations import StockReservations
from fragancia_api.modules.inventory.http.router import routers
from fragancia_api.modules.inventory.infrastructure.catalog_presentations import (
    CatalogPresentations,
)
from fragancia_api.modules.inventory.infrastructure.sql_reservation_repository import (
    SqlReservationRepository,
)
from fragancia_api.modules.inventory.infrastructure.sql_settings_repository import (
    SqlSettingsRepository,
)
from fragancia_api.modules.inventory.infrastructure.sql_stock_queries import SqlStockQueries
from fragancia_api.modules.inventory.infrastructure.sql_stock_repository import (
    SqlStockRepository,
)
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.module import AppModule, Platform, ScheduledJob


def register(platform: Platform, services: ServiceRegistry) -> None:
    stock = SqlStockRepository(platform.database)
    settings = SqlSettingsRepository(platform.database)
    queries = SqlStockQueries(platform.database)
    # The catalog registers before inventory (MODULES order), so its facade is available.
    catalog = CatalogPresentations(services.get(PresentationDirectory))

    services.add(
        RecordMovement,
        RecordMovement(
            stock=stock,
            settings=settings,
            catalog=catalog,
            transactions=platform.transactions,
            events=platform.events,
            clock=platform.clock,
        ),
    )
    record_imported_counts = RecordImportedCounts(
        stock=stock,
        settings=settings,
        catalog=catalog,
        transactions=platform.transactions,
        events=platform.events,
        clock=platform.clock,
    )
    services.add(RecordImportedCounts, record_imported_counts)
    platform.subscriptions.subscribe(
        IMPORT_APPLIED_EVENT, ApplyImportedCounts(record_imported_counts)
    )
    services.add(
        SetThreshold,
        SetThreshold(
            stock=stock,
            settings=settings,
            catalog=catalog,
            transactions=platform.transactions,
            events=platform.events,
            clock=platform.clock,
        ),
    )
    services.add(
        UpdateInventorySettings,
        UpdateInventorySettings(
            settings=settings,
            stock=stock,
            transactions=platform.transactions,
            events=platform.events,
            clock=platform.clock,
        ),
    )
    services.add(GetStock, GetStock(catalog, queries))
    services.add(ListMovements, ListMovements(catalog, queries))
    services.add(ListStock, ListStock(catalog, queries))
    services.add(GetInventorySettings, GetInventorySettings(queries))
    services.add(ListReservationHolds, ListReservationHolds(catalog, queries))

    reservations = SqlReservationRepository(platform.database)
    reserve = ReserveStock(
        reservations=reservations,
        stock=stock,
        settings=settings,
        catalog=catalog,
        transactions=platform.transactions,
        events=platform.events,
        clock=platform.clock,
    )
    extend = ExtendReservation(
        reservations=reservations, transactions=platform.transactions, clock=platform.clock
    )
    commit = CommitReservation(
        reservations=reservations,
        stock=stock,
        settings=settings,
        transactions=platform.transactions,
        events=platform.events,
        clock=platform.clock,
    )
    release = ReleaseReservation(
        reservations=reservations,
        stock=stock,
        settings=settings,
        transactions=platform.transactions,
        events=platform.events,
        clock=platform.clock,
    )
    services.add(ReserveStock, reserve)
    services.add(ExtendReservation, extend)
    services.add(CommitReservation, commit)
    services.add(ReleaseReservation, release)
    services.add(
        ExpireReservations,
        ExpireReservations(
            reservations=reservations,
            stock=stock,
            settings=settings,
            transactions=platform.transactions,
            events=platform.events,
            clock=platform.clock,
        ),
    )
    services.add(
        StockReservations,
        StockReservations(reserve=reserve, extend=extend, commit=commit, release=release),
    )


module = AppModule(
    name="inventory",
    register=register,
    routers=routers,
    jobs=(
        ScheduledJob(
            "inventory_expire_reservations",
            lambda s: s.get(ExpireReservations).run_once(),
            60,
            120,
        ),
    ),
)
