"""Plan 007: the whole job against real PostgreSQL and RustFS. A template filled in memory is
uploaded, the worker job validates it (downloading an image from a local server), the staged
WebP renditions land in the bucket, and expiry or an invalid result removes them again."""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from io import BytesIO
from typing import Any

import pytest
from openpyxl import Workbook  # type: ignore[import-untyped]

from fragancia_api.config import Settings
from fragancia_api.container import Container, build_container
from fragancia_api.modules.catalog.application.commands.catalog_imports import (
    ExpireCatalogImports,
    StartCatalogImport,
    ValidateNextCatalogImport,
)
from fragancia_api.modules.catalog.application.queries.catalog_imports import GetCatalogImport
from fragancia_api.modules.catalog.domain.import_cells import COLUMNS
from fragancia_api.shared.infrastructure.in_memory import FixedClock
from fragancia_api.shared.kernel import Ok
from tests.integration.catalog import images
from tests.integration.catalog.conftest import Bucket
from tests.integration.catalog.import_fixtures import (
    BRAND_NAME,
    CONCENTRATION_NAME,
    FAMILY_NAME,
    OLD_FAMILY_NAME,
    World,
    world,  # noqa: F401  (a fixture)
)

pytestmark = pytest.mark.integration

JPEG = images.jpeg((1400, 1000))


@pytest.fixture
async def image_server() -> AsyncIterator[str]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        path = head.split(" ")[1]
        if path == "/photo.jpg":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(JPEG) + JPEG)
        elif path == "/text":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello")
        else:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    server.close()
    await server.wait_closed()


@pytest.fixture
async def job_container(settings: Settings, world: World) -> AsyncIterator[Container]:  # noqa: F811
    allowed = settings.model_copy(update={"catalog_import_image_allow_private_hosts": True})
    container = build_container(allowed)
    yield container
    await container.close()


def _xlsx(rows: list[dict[str, Any]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(list(COLUMNS))
    for values in rows:
        sheet.append([values.get(column) for column in COLUMNS])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _row(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "Marca": BRAND_NAME,
        "Nombre": "Job Perfume",
        "Concentración": CONCENTRATION_NAME,
        "Género": "Mujer",
        "Familia olfativa": FAMILY_NAME,
        "ML": 100,
        "Precio": 2500,
        "Disponibilidad": "En existencia",
    }
    values.update(overrides)
    return values


async def _upload_and_validate(container: Container, data: bytes) -> Any:
    started = await container.services.get(StartCatalogImport).execute(data, "admin-job")
    assert isinstance(started, Ok)
    assert await container.services.get(ValidateNextCatalogImport).run_once() == 1
    found = await container.services.get(GetCatalogImport).execute(started.value)
    assert isinstance(found, Ok)
    return found.value


def _staged(bucket: Bucket, perfume_prefix: str = "perfumes/") -> list[str]:
    return bucket.keys(perfume_prefix)


async def test_a_valid_file_becomes_ready_stages_the_webp_photos_and_expires_cleanly(
    job_container: Container, bucket: Bucket, image_server: str
) -> None:
    before = set(bucket.keys("perfumes/"))
    data = _xlsx(
        [
            _row(**{"Imagen 1": f"{image_server}/photo.jpg"}),
            _row(ML=50, Precio=1500, **{"Imagen 1": f"{image_server}/photo.jpg"}),
        ]
    )

    report = await _upload_and_validate(job_container, data)

    assert report.status == "ready"
    assert report.error_count == 0
    assert report.row_count == 2
    assert report.summary.model_dump() == {
        "perfumes_new": 1,
        "perfumes_updated": 0,
        "presentations_new": 2,
        "presentations_updated": 0,
        "photos_new": 1,
        "stock_counts": 0,
    }
    assert report.expires_at - report.finished_at == timedelta(minutes=60)
    new_keys = sorted(set(bucket.keys("perfumes/")) - before)
    bucket.created_prefixes.extend({key.rsplit("/", 1)[0] + "/" for key in new_keys})
    assert len(new_keys) == 2
    assert {key.rsplit("-", 1)[1] for key in new_keys} == {"card.webp", "detail.webp"}
    assert bucket.public_get(new_keys[0]).status_code == 200

    expire = ExpireCatalogImports(
        imports=job_container.services.get(ValidateNextCatalogImport)._imports,  # noqa: SLF001
        storage=job_container.services.get(ValidateNextCatalogImport)._storage,  # noqa: SLF001
        transactions=job_container.services.get(ValidateNextCatalogImport)._transactions,  # noqa: SLF001
        clock=FixedClock(report.expires_at + timedelta(seconds=1)),
        stale_after=timedelta(minutes=30),
    )
    assert await expire.run_once() == 1

    after = await job_container.services.get(GetCatalogImport).execute(report.id)
    assert isinstance(after, Ok)
    assert after.value.status == "expired"
    assert set(bucket.keys("perfumes/")) - before == set()  # the staged objects are gone


async def test_an_invalid_file_reports_the_rows_and_leaves_no_staged_objects(
    job_container: Container, bucket: Bucket, image_server: str
) -> None:
    before = set(bucket.keys("perfumes/"))
    data = _xlsx(
        [
            _row(Nombre="Good", **{"Imagen 1": f"{image_server}/photo.jpg"}),
            _row(Nombre="Bad brand", Marca="Nobody"),
            _row(Nombre="Bad family", **{"Familia olfativa": OLD_FAMILY_NAME}),
            _row(Nombre="Bad ml", ML="abc"),
            _row(Nombre="Bad price", Precio="10.555"),
            _row(Nombre="Bad sale", **{"Precio oferta": 9999}),
            _row(Nombre="Bad order", Disponibilidad="Sobre pedido"),
        ]
    )

    report = await _upload_and_validate(job_container, data)

    assert report.status == "invalid"
    assert [(e.row, e.column, e.code) for e in report.errors] == [
        (3, "Marca", "CATALOG_IMPORT_BRAND_UNKNOWN"),
        (4, "Familia olfativa", "CATALOG_IMPORT_FAMILY_ARCHIVED"),
        (5, "ML", "CATALOG_IMPORT_VALUE_INVALID"),
        (6, "Precio", "CATALOG_IMPORT_VALUE_INVALID"),
        (7, "Precio oferta", "CATALOG_PRESENTATION_SALE_INVALID"),
        (8, "Disponibilidad", "CATALOG_PRESENTATION_AVAILABILITY_INVALID"),
    ]
    assert set(bucket.keys("perfumes/")) == before  # the good row's photo was staged, then deleted


async def test_private_image_hosts_are_blocked_on_the_image_column_with_the_default_settings(
    job_container: Container, bucket: Bucket, image_server: str
) -> None:
    data = _xlsx(
        [
            _row(Nombre="Missing", **{"Imagen 1": f"{image_server}/nothing.jpg"}),
            _row(Nombre="Text", **{"Imagen 2": f"{image_server}/text"}),
            _row(Nombre="Blocked", **{"Imagen 3": "http://127.0.0.1:1/a.jpg"}),
        ]
    )
    strict = build_container(
        job_container.settings.model_copy(
            update={"catalog_import_image_allow_private_hosts": False}
        )
    )
    try:
        report = await _upload_and_validate(strict, data)
    finally:
        await strict.close()

    assert report.status == "invalid"
    assert [(e.row, e.column, e.code) for e in report.errors] == [
        (2, "Imagen 1", "CATALOG_IMPORT_IMAGE_HOST_BLOCKED"),
        (3, "Imagen 2", "CATALOG_IMPORT_IMAGE_HOST_BLOCKED"),
        (4, "Imagen 3", "CATALOG_IMPORT_IMAGE_HOST_BLOCKED"),
    ]


async def test_a_file_that_is_not_a_workbook_is_invalid_not_failed(
    job_container: Container,
) -> None:
    report = await _upload_and_validate(job_container, b"PK\x03\x04 not a workbook")

    assert report.status == "invalid"
    assert [e.code for e in report.errors] == ["CATALOG_IMPORT_FILE_UNREADABLE"]


async def test_a_missing_required_header_is_reported_at_file_level(
    job_container: Container,
) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([c for c in COLUMNS if c != "Precio"])
    sheet.append(["x"] * (len(COLUMNS) - 1))
    buffer = BytesIO()
    workbook.save(buffer)

    report = await _upload_and_validate(job_container, buffer.getvalue())

    assert report.status == "invalid"
    assert [(e.row, e.column, e.code) for e in report.errors] == [
        (None, "Precio", "CATALOG_IMPORT_HEADER_MISSING")
    ]
