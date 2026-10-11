"""Plan 007: the settings, the module's scheduled jobs and the composition of the import."""

from typing import Any

import pytest
from pydantic import ValidationError

from fragancia_api.container import build_container
from fragancia_api.modules.catalog import module as catalog_module
from fragancia_api.modules.catalog.application.commands.catalog_imports import (
    ExpireCatalogImports,
    ImportUploadLimit,
    StartCatalogImport,
    ValidateNextCatalogImport,
)
from fragancia_api.modules.catalog.application.queries.catalog_imports import (
    DownloadCatalogTemplate,
    GetCatalogImport,
)
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.module import AppModule, ScheduledJob
from tests.support import make_settings


async def _job(registry: ServiceRegistry) -> int:
    return 0


# --- settings (step 3) ------------------------------------------------------------------------


def test_the_import_settings_default_to_the_plan_values() -> None:
    settings = make_settings()

    assert settings.catalog_import_max_bytes == 5_242_880
    assert settings.catalog_import_max_rows == 2000
    assert settings.catalog_import_ttl_minutes == 60
    assert settings.catalog_import_image_allow_private_hosts is False


def test_the_import_settings_can_be_overridden() -> None:
    settings = make_settings(
        catalog_import_max_bytes=10,
        catalog_import_max_rows=3,
        catalog_import_ttl_minutes=1,
        catalog_import_image_allow_private_hosts=True,
    )

    assert settings.catalog_import_max_bytes == 10
    assert settings.catalog_import_max_rows == 3
    assert settings.catalog_import_ttl_minutes == 1
    assert settings.catalog_import_image_allow_private_hosts is True


@pytest.mark.parametrize(
    "name", ["catalog_import_max_bytes", "catalog_import_max_rows", "catalog_import_ttl_minutes"]
)
@pytest.mark.parametrize("value", [0, -1])
def test_the_import_limits_must_be_at_least_one(name: str, value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{name: value})


# --- scheduled jobs (step 9) ------------------------------------------------------------------


@pytest.mark.parametrize("every", [1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60])
def test_a_job_may_run_every_divisor_of_a_minute(every: int) -> None:
    assert ScheduledJob("job", _job, every, 10).every_seconds == every


@pytest.mark.parametrize("every", [0, -5, 7, 8, 25, 45, 61, 120])
def test_a_job_that_does_not_divide_a_minute_is_rejected(every: int) -> None:
    with pytest.raises(ValueError, match="every_seconds must divide 60"):
        ScheduledJob("job", _job, every, 10)


def test_a_module_has_no_jobs_unless_it_declares_them() -> None:
    module = AppModule(name="x", register=lambda platform, services: None)

    assert tuple(module.jobs) == ()


def test_the_catalog_module_declares_the_two_import_jobs() -> None:
    jobs = {job.name: job for job in catalog_module.module.jobs}

    assert set(jobs) == {"catalog_validate_imports", "catalog_expire_imports"}
    validate = jobs["catalog_validate_imports"]
    expire = jobs["catalog_expire_imports"]
    assert (validate.every_seconds, validate.timeout_seconds) == (5, 1800)
    assert (expire.every_seconds, expire.timeout_seconds) == (60, 120)


async def test_each_catalog_job_runs_its_command_and_returns_its_count() -> None:
    class _Command:
        def __init__(self, processed: int) -> None:
            self.processed = processed
            self.runs = 0

        async def run_once(self) -> int:
            self.runs += 1
            return self.processed

    validate_command, expire_command = _Command(3), _Command(7)
    registry = ServiceRegistry()
    registry.add(ValidateNextCatalogImport, validate_command)
    registry.add(ExpireCatalogImports, expire_command)
    jobs = {job.name: job for job in catalog_module.module.jobs}

    assert await jobs["catalog_validate_imports"].run(registry) == 3
    assert await jobs["catalog_expire_imports"].run(registry) == 7
    assert (validate_command.runs, expire_command.runs) == (1, 1)


# --- composition (step 10) --------------------------------------------------------------------


async def test_the_container_registers_every_import_service_with_its_settings() -> None:
    container = build_container(
        make_settings(catalog_import_max_bytes=1234, catalog_import_max_rows=9)
    )
    services: Any = container.services

    for service_type in (
        StartCatalogImport,
        ImportUploadLimit,
        ValidateNextCatalogImport,
        ExpireCatalogImports,
        GetCatalogImport,
        DownloadCatalogTemplate,
    ):
        assert service_type in services
    assert services.get(ImportUploadLimit).max_bytes == 1234
    await container.close()


async def test_the_import_routes_are_mounted_in_the_application() -> None:
    container = build_container(make_settings())

    paths = {
        path
        for router in container.routers
        for path in (route.path for route in router.routes)  # type: ignore[attr-defined]
    }

    assert any(path.endswith("/catalog-imports/template") for path in paths)
    await container.close()
