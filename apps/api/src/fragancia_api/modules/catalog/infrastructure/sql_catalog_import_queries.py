from uuid import UUID

from sqlalchemy import select

from fragancia_api.modules.catalog.contracts import (
    CatalogImportIssue,
    CatalogImportResponse,
    CatalogImportSummary,
)
from fragancia_api.modules.catalog.infrastructure.tables import imports
from fragancia_api.shared.infrastructure.database import Database


class SqlCatalogImportQueries:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self, import_id: UUID) -> CatalogImportResponse | None:
        statement = select(
            imports.c.id,
            imports.c.status,
            imports.c.created_at,
            imports.c.finished_at,
            imports.c.expires_at,
            imports.c.row_count,
            imports.c.summary,
            imports.c.error_count,
            imports.c.errors,
        ).where(imports.c.id == import_id)
        async with self._database.reader() as session:
            row = (await session.execute(statement)).mappings().first()
        if row is None:
            return None
        summary = row["summary"]
        return CatalogImportResponse(
            id=row["id"],
            status=row["status"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
            expires_at=row["expires_at"],
            row_count=row["row_count"],
            summary=CatalogImportSummary.model_validate(summary) if summary is not None else None,
            error_count=row["error_count"] or 0,
            errors=[CatalogImportIssue.model_validate(issue) for issue in row["errors"] or []],
        )
