from uuid import UUID

from fragancia_api.modules.catalog.application.ports import (
    CatalogImportQueries,
    ConcentrationQueries,
    OlfactoryFamilyQueries,
    TemplateWriter,
)
from fragancia_api.modules.catalog.contracts import CatalogImportResponse
from fragancia_api.modules.catalog.domain.errors import ImportNotFound
from fragancia_api.shared.kernel import Err, Ok, Result


class GetCatalogImport:
    """Query: an import with its status and validation report (the panel polls it)."""

    def __init__(self, queries: CatalogImportQueries) -> None:
        self._queries = queries

    async def execute(self, import_id: UUID) -> Result[CatalogImportResponse, ImportNotFound]:
        found = await self._queries.get(import_id)
        if found is None:
            return Err(ImportNotFound())
        return Ok(found)


class DownloadCatalogTemplate:
    """Query: the .xlsx import template, with dropdowns of the active families and
    concentrations."""

    def __init__(
        self,
        families: OlfactoryFamilyQueries,
        concentrations: ConcentrationQueries,
        writer: TemplateWriter,
    ) -> None:
        self._families = families
        self._concentrations = concentrations
        self._writer = writer

    async def execute(self) -> bytes:
        families = [family.name for family in await self._families.list_active()]
        concentrations = [item.name for item in await self._concentrations.list_active()]
        return await self._writer.build(families, concentrations)
