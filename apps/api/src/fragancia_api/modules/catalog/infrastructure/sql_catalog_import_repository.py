"""Persistence of `CatalogImport`, with explicit JSON mappings of its summary, issues and plan
(UUIDs and ISO-8601 datetimes as strings; never pickled)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, update

from fragancia_api.modules.catalog.domain.catalog_import import (
    CatalogImport,
    ImportIssue,
    ImportPlan,
    ImportStatus,
    ImportSummary,
    PlannedPerfume,
    PlannedPhoto,
    PlannedPresentation,
)
from fragancia_api.modules.catalog.domain.perfume import Gender
from fragancia_api.modules.catalog.infrastructure.tables import imports
from fragancia_api.shared.infrastructure.database import Database

type Json = dict[str, Any]


def _uuid(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _to_uuid(value: str | None) -> UUID | None:
    return UUID(value) if value is not None else None


def _moment(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _to_moment(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def summary_to_json(summary: ImportSummary) -> Json:
    return {
        "perfumes_new": summary.perfumes_new,
        "perfumes_updated": summary.perfumes_updated,
        "presentations_new": summary.presentations_new,
        "presentations_updated": summary.presentations_updated,
        "photos_new": summary.photos_new,
        "stock_counts": summary.stock_counts,
    }


def summary_from_json(data: Json) -> ImportSummary:
    return ImportSummary(
        perfumes_new=data["perfumes_new"],
        perfumes_updated=data["perfumes_updated"],
        presentations_new=data["presentations_new"],
        presentations_updated=data["presentations_updated"],
        photos_new=data["photos_new"],
        stock_counts=data.get("stock_counts", 0),  # absent in imports stored before plan 005
    )


def issue_to_json(issue: ImportIssue) -> Json:
    return {"row": issue.row, "column": issue.column, "code": issue.code, "message": issue.message}


def issue_from_json(data: Json) -> ImportIssue:
    return ImportIssue(
        row=data["row"], column=data["column"], code=data["code"], message=data["message"]
    )


def _presentation_to_json(presentation: PlannedPresentation) -> Json:
    return {
        "presentation_id": _uuid(presentation.presentation_id),
        "ml": presentation.ml,
        "price_cents": presentation.price_cents,
        "sale_price_cents": presentation.sale_price_cents,
        "sale_starts_at": _moment(presentation.sale_starts_at),
        "sale_ends_at": _moment(presentation.sale_ends_at),
        "availability": presentation.availability,
        "lead_time_min_days": presentation.lead_time_min_days,
        "lead_time_max_days": presentation.lead_time_max_days,
        "row": presentation.row,
        "stock_count": presentation.stock_count,
    }


def _presentation_from_json(data: Json) -> PlannedPresentation:
    return PlannedPresentation(
        presentation_id=_to_uuid(data["presentation_id"]),
        ml=data["ml"],
        price_cents=data["price_cents"],
        sale_price_cents=data["sale_price_cents"],
        sale_starts_at=_to_moment(data["sale_starts_at"]),
        sale_ends_at=_to_moment(data["sale_ends_at"]),
        availability=data["availability"],
        lead_time_min_days=data["lead_time_min_days"],
        lead_time_max_days=data["lead_time_max_days"],
        row=data["row"],
        stock_count=data.get("stock_count"),  # absent in imports stored before plan 005
    )


def _photo_to_json(photo: PlannedPhoto) -> Json:
    return {
        "position": photo.position,
        "photo_id": str(photo.photo_id),
        "source_url": photo.source_url,
        "card_key": photo.card_key,
        "detail_key": photo.detail_key,
        "width": photo.width,
        "height": photo.height,
        "replaces_photo_id": _uuid(photo.replaces_photo_id),
    }


def _photo_from_json(data: Json) -> PlannedPhoto:
    return PlannedPhoto(
        position=data["position"],
        photo_id=UUID(data["photo_id"]),
        source_url=data["source_url"],
        card_key=data["card_key"],
        detail_key=data["detail_key"],
        width=data["width"],
        height=data["height"],
        replaces_photo_id=_to_uuid(data["replaces_photo_id"]),
    )


def plan_to_json(plan: ImportPlan) -> Json:
    return {
        "perfumes": [
            {
                "perfume_id": str(perfume.perfume_id),
                "is_new": perfume.is_new,
                "brand_id": str(perfume.brand_id),
                "concentration_id": str(perfume.concentration_id),
                "family_id": str(perfume.family_id),
                "name": perfume.name,
                "gender": perfume.gender.value,
                "description": perfume.description,
                "top_notes": list(perfume.top_notes),
                "heart_notes": list(perfume.heart_notes),
                "base_notes": list(perfume.base_notes),
                "publish": perfume.publish,
                "first_row": perfume.first_row,
                "presentations": [_presentation_to_json(p) for p in perfume.presentations],
                "photos": [_photo_to_json(photo) for photo in perfume.photos],
            }
            for perfume in plan.perfumes
        ]
    }


def plan_from_json(data: Json) -> ImportPlan:
    return ImportPlan(
        perfumes=tuple(
            PlannedPerfume(
                perfume_id=UUID(perfume["perfume_id"]),
                is_new=perfume["is_new"],
                brand_id=UUID(perfume["brand_id"]),
                concentration_id=UUID(perfume["concentration_id"]),
                family_id=UUID(perfume["family_id"]),
                name=perfume["name"],
                gender=Gender(perfume["gender"]),
                description=perfume["description"],
                top_notes=tuple(perfume["top_notes"]),
                heart_notes=tuple(perfume["heart_notes"]),
                base_notes=tuple(perfume["base_notes"]),
                publish=perfume["publish"],
                first_row=perfume["first_row"],
                presentations=tuple(_presentation_from_json(p) for p in perfume["presentations"]),
                photos=tuple(_photo_from_json(photo) for photo in perfume["photos"]),
            )
            for perfume in data["perfumes"]
        )
    )


def _to_row(catalog_import: CatalogImport) -> Json:
    summary = catalog_import.summary
    plan = catalog_import.plan
    return {
        "id": catalog_import.id,
        "status": catalog_import.status.value,
        "file": catalog_import.file,
        "created_by": catalog_import.created_by,
        "created_at": catalog_import.created_at,
        "started_at": catalog_import.started_at,
        "finished_at": catalog_import.finished_at,
        "expires_at": catalog_import.expires_at,
        "row_count": catalog_import.row_count,
        "error_count": catalog_import.error_count,
        "summary": summary_to_json(summary) if summary is not None else None,
        "errors": [issue_to_json(issue) for issue in catalog_import.errors],
        "plan": plan_to_json(plan) if plan is not None else None,
        "applied_at": catalog_import.applied_at,
        "applied_by": catalog_import.applied_by,
        "discarded_at": catalog_import.discarded_at,
    }


def _to_import(row: Any) -> CatalogImport:
    return CatalogImport(
        id=row["id"],
        status=ImportStatus(row["status"]),
        file=row["file"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        expires_at=row["expires_at"],
        row_count=row["row_count"],
        summary=summary_from_json(row["summary"]) if row["summary"] is not None else None,
        error_count=row["error_count"],
        errors=tuple(issue_from_json(issue) for issue in row["errors"] or []),
        plan=plan_from_json(row["plan"]) if row["plan"] is not None else None,
        applied_at=row["applied_at"],
        applied_by=row["applied_by"],
        discarded_at=row["discarded_at"],
    )


class SqlCatalogImportRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def add(self, catalog_import: CatalogImport) -> None:
        await self._database.session.execute(insert(imports).values(_to_row(catalog_import)))

    async def get_for_update(self, import_id: UUID) -> CatalogImport | None:
        statement = select(imports).where(imports.c.id == import_id).with_for_update()
        row = (await self._database.session.execute(statement)).mappings().first()
        return _to_import(row) if row is not None else None

    async def claim_next_pending(self) -> CatalogImport | None:
        statement = (
            select(imports)
            .where(imports.c.status == ImportStatus.PENDING.value)
            .order_by(imports.c.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        row = (await self._database.session.execute(statement)).mappings().one_or_none()
        return _to_import(row) if row is not None else None

    async def list_stale_validating(self, before: datetime) -> list[CatalogImport]:
        statement = (
            select(imports)
            .where(
                imports.c.status == ImportStatus.VALIDATING.value,
                imports.c.started_at < before,
            )
            .order_by(imports.c.created_at)
            .with_for_update(skip_locked=True)
        )
        rows = (await self._database.session.execute(statement)).mappings().all()
        return [_to_import(row) for row in rows]

    async def list_expired_ready(self, now: datetime) -> list[CatalogImport]:
        statement = (
            select(imports)
            .where(imports.c.status == ImportStatus.READY.value, imports.c.expires_at <= now)
            .order_by(imports.c.created_at)
            .with_for_update(skip_locked=True)
        )
        rows = (await self._database.session.execute(statement)).mappings().all()
        return [_to_import(row) for row in rows]

    async def save(self, catalog_import: CatalogImport) -> None:
        values = _to_row(catalog_import)
        del values["id"], values["created_by"], values["created_at"]
        await self._database.session.execute(
            update(imports).where(imports.c.id == catalog_import.id).values(values)
        )
