"""Plan 003, wiring: the container resolves the reservation commands and the facade, and the
inventory module declares the expiry job."""

from fragancia_api.container import build_container
from fragancia_api.modules.inventory import StockReservations
from fragancia_api.modules.inventory.application.commands.reservations import (
    CommitReservation,
    ExpireReservations,
    ExtendReservation,
    ReleaseReservation,
    ReserveStock,
)
from fragancia_api.modules.inventory.application.queries.stock import ListReservationHolds
from fragancia_api.modules.inventory.module import module as inventory_module
from fragancia_api.shared.http.services import ServiceRegistry
from tests.support import make_settings


async def test_the_container_resolves_the_reservation_services_without_connecting() -> None:
    container = build_container(make_settings())

    for service in (
        ReserveStock,
        ExtendReservation,
        CommitReservation,
        ReleaseReservation,
        ExpireReservations,
        StockReservations,
        ListReservationHolds,
    ):
        assert isinstance(container.services.get(service), service)
    await container.close()


def test_the_module_declares_the_expiry_job_once_a_minute() -> None:
    [job] = inventory_module.jobs

    assert (job.name, job.every_seconds, job.timeout_seconds) == (
        "inventory_expire_reservations",
        60,
        120,
    )


async def test_the_expiry_job_runs_the_command_and_returns_its_count() -> None:
    [job] = inventory_module.jobs

    class _Expire:
        async def run_once(self) -> int:
            return 7

    services = ServiceRegistry()
    services.add(ExpireReservations, _Expire())

    assert await job.run(services) == 7
