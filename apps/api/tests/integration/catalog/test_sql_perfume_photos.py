"""Plan 006: perfume photos against `fragancia_test`: the table, the repository (load, save,
deferrable reorder, removal), the lock under concurrent uploads and the read-side URLs."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from fragancia_api.config import Settings
from fragancia_api.container import Container
from fragancia_api.modules.catalog.application.commands.photos import (
    AddPhoto,
    RemovePhoto,
    ReorderPhotos,
)
from fragancia_api.modules.catalog.application.ports import PublicPerfumeFilters
from fragancia_api.modules.catalog.application.queries.perfumes import GetAdminPerfume
from fragancia_api.modules.catalog.contracts import AdminPerfume
from fragancia_api.modules.catalog.domain.naming import slugify
from fragancia_api.modules.catalog.domain.perfume import Perfume, photo_keys
from fragancia_api.modules.catalog.infrastructure.in_memory_media import (
    FakeImageProcessor,
    InMemoryMediaStorage,
)
from fragancia_api.modules.catalog.infrastructure.pillow_image_processor import (
    PillowImageProcessor,
)
from fragancia_api.modules.catalog.infrastructure.s3_media_storage import S3MediaStorage
from fragancia_api.modules.catalog.infrastructure.sql_perfume_queries import SqlPerfumeQueries
from fragancia_api.modules.catalog.infrastructure.sql_perfume_repository import (
    SqlPerfumeRepository,
)
from fragancia_api.modules.catalog.infrastructure.tables import (
    brands,
    concentrations,
    olfactory_families,
    perfume_photos,
    perfume_slug_history,
    perfumes,
    presentations,
)
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.infrastructure.in_memory import FixedClock
from fragancia_api.shared.kernel import Err, Ok, new_id
from tests.integration.catalog import images
from tests.integration.catalog.conftest import Bucket

pytestmark = pytest.mark.integration

CREATED = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
MEDIA = "http://media.test/bucket"
UNKNOWN_ID = UUID(int=404)
NO_FILTERS = PublicPerfumeFilters(
    q=None,
    brands=(),
    families=(),
    genders=(),
    min_price_cents=None,
    max_price_cents=None,
    sort="name",
    page=1,
    size=48,
)


@dataclass(frozen=True)
class Refs:
    brand: UUID
    family: UUID
    concentration: UUID
    queries: SqlPerfumeQueries


@pytest.fixture
async def refs(container: Container) -> AsyncIterator[Refs]:
    """Owns one brand, family and concentration; empties the perfume tables around each test."""
    brand, family, concentration = new_id(), new_id(), new_id()
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE catalog.perfumes CASCADE"))
        await connection.execute(
            insert(brands),
            {
                "id": brand,
                "name": "Zz Photo Brand",
                "slug": slugify("Zz Photo Brand"),
                "is_active": True,
                "created_at": CREATED,
            },
        )
        await connection.execute(
            insert(olfactory_families),
            {
                "id": family,
                "name": "Zz Photo Family",
                "slug": slugify("Zz Photo Family"),
                "is_active": True,
                "created_at": CREATED,
            },
        )
        await connection.execute(
            insert(concentrations),
            {
                "id": concentration,
                "name": "Zz Photo Concentration",
                "slug": slugify("Zz Photo Concentration"),
                "abbreviation": "ZPC",
                "abbreviation_slug": "zpc",
                "is_active": True,
                "created_at": CREATED,
            },
        )
    yield Refs(
        brand, family, concentration, SqlPerfumeQueries(container.database, media_base_url=MEDIA)
    )
    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE catalog.perfumes CASCADE"))
        await connection.execute(delete(brands).where(brands.c.id == brand))
        await connection.execute(
            delete(olfactory_families).where(olfactory_families.c.id == family)
        )
        await connection.execute(delete(concentrations).where(concentrations.c.id == concentration))


# --- seeding and reading helpers --------------------------------------------------------------


async def _perfume(
    container: Container,
    refs: Refs,
    name: str = "Eros",
    *,
    published: bool = False,
    archived: bool = False,
    with_presentation: bool = False,
) -> UUID:
    perfume_id = new_id()
    async with container.database.engine.begin() as connection:
        await connection.execute(
            insert(perfumes).values(
                {
                    "id": perfume_id,
                    "brand_id": refs.brand,
                    "concentration_id": refs.concentration,
                    "family_id": refs.family,
                    "name": name,
                    "name_slug": slugify(name),
                    "slug": f"{slugify(name)}-{perfume_id}",
                    "gender": "men",
                    "description": "",
                    "top_notes": [],
                    "heart_notes": [],
                    "base_notes": [],
                    "is_published": published,
                    "first_published_at": CREATED if published else None,
                    "is_archived": archived,
                    "created_at": CREATED,
                    "updated_at": CREATED,
                }
            )
        )
        if with_presentation:
            await connection.execute(
                insert(presentations).values(
                    {
                        "id": new_id(),
                        "perfume_id": perfume_id,
                        "ml": 100,
                        "price_cents": 250_000,
                        "availability": "in_stock",
                        "is_active": True,
                        "created_at": CREATED,
                    }
                )
            )
    return perfume_id


async def _photo(
    container: Container, perfume_id: UUID, position: int, *, photo_id: UUID | None = None, **o: Any
) -> UUID:
    row_id = photo_id or new_id()
    card_key, detail_key = photo_keys(perfume_id, row_id)
    values: dict[str, Any] = {
        "id": row_id,
        "perfume_id": perfume_id,
        "position": position,
        "card_key": card_key,
        "detail_key": detail_key,
        "width": 1200,
        "height": 900,
        "created_at": CREATED + timedelta(minutes=position),
    }
    values.update(o)
    async with container.database.engine.begin() as connection:
        await connection.execute(insert(perfume_photos).values(values))
    return row_id


async def _rows(container: Container, perfume_id: UUID) -> list[dict[str, Any]]:
    async with container.database.reader() as session:
        statement = (
            select(perfume_photos)
            .where(perfume_photos.c.perfume_id == perfume_id)
            .order_by(perfume_photos.c.position)
        )
        return [dict(row) for row in (await session.execute(statement)).mappings().all()]


async def _positions(container: Container, perfume_id: UUID) -> list[tuple[UUID, int]]:
    return [(row["id"], row["position"]) for row in await _rows(container, perfume_id)]


async def _change(container: Container, perfume_id: UUID, mutate: Any) -> Perfume:
    """Load the aggregate under lock, let `mutate` change it, save it, return the saved one."""
    repository = SqlPerfumeRepository(container.database)
    saved: list[Perfume] = []

    async def work() -> Ok[None]:
        perfume = await repository.get_for_update(perfume_id)
        assert perfume is not None
        mutate(perfume)
        assert isinstance(await repository.save(perfume), Ok)
        saved.append(perfume)
        return Ok(None)

    await SqlTransactionRunner(container.database).run(work)
    return saved[0]


async def _load(container: Container, perfume_id: UUID) -> Perfume:
    return await _change(container, perfume_id, lambda perfume: None)


# --- the table --------------------------------------------------------------------------------


async def test_a_position_is_unique_within_a_perfume_but_not_across_perfumes(
    container: Container, refs: Refs
) -> None:
    first, second = await _perfume(container, refs, "One"), await _perfume(container, refs, "Two")
    await _photo(container, first, 1)
    await _photo(container, second, 1)  # same position, another perfume: fine

    with pytest.raises(IntegrityError, match="uq_perfume_photos_position"):
        await _photo(container, first, 1)


async def test_the_position_constraint_is_deferred_until_commit(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs)
    a = await _photo(container, perfume_id, 1)
    b = await _photo(container, perfume_id, 2)

    async with container.database.engine.begin() as connection:
        # Between these two statements both rows hold position 2: only a deferred check allows it.
        await connection.execute(
            update(perfume_photos).where(perfume_photos.c.id == a).values(position=2)
        )
        await connection.execute(
            update(perfume_photos).where(perfume_photos.c.id == b).values(position=1)
        )

    assert await _positions(container, perfume_id) == [(b, 1), (a, 2)]


async def test_a_duplicate_position_left_at_commit_is_rejected_and_nothing_is_written(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs)
    a = await _photo(container, perfume_id, 1)
    b = await _photo(container, perfume_id, 2)

    with pytest.raises(IntegrityError, match="uq_perfume_photos_position"):
        async with container.database.engine.begin() as connection:
            await connection.execute(
                update(perfume_photos).where(perfume_photos.c.id == b).values(position=1)
            )

    assert await _positions(container, perfume_id) == [(a, 1), (b, 2)]


async def test_a_photo_needs_an_existing_perfume(container: Container, refs: Refs) -> None:
    with pytest.raises(IntegrityError, match="foreign key"):
        await _photo(container, UNKNOWN_ID, 1)


async def test_truncating_the_perfumes_cascades_to_their_photos(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs)
    await _photo(container, perfume_id, 1)

    async with container.database.engine.begin() as connection:
        await connection.execute(text("TRUNCATE catalog.perfumes CASCADE"))

    assert await _rows(container, perfume_id) == []


# --- repository: load -------------------------------------------------------------------------


async def test_get_for_update_loads_the_photos_ordered_by_position_with_every_field(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs)
    third = await _photo(container, perfume_id, 3, width=700, height=1000)
    first = await _photo(container, perfume_id, 1)
    second = await _photo(container, perfume_id, 2)

    perfume = await _load(container, perfume_id)

    assert [(p.id, p.position) for p in perfume.photos] == [(first, 1), (second, 2), (third, 3)]
    last = perfume.photos[2]
    assert (last.width, last.height) == (700, 1000)
    assert (last.card_key, last.detail_key) == photo_keys(perfume_id, third)
    assert last.created_at == CREATED + timedelta(minutes=3)
    assert perfume.removed_photo_ids == []


async def test_get_for_update_loads_only_the_photos_of_that_perfume(
    container: Container, refs: Refs
) -> None:
    mine, other = await _perfume(container, refs, "Mine"), await _perfume(container, refs, "Other")
    own = await _photo(container, mine, 1)
    await _photo(container, other, 1)
    await _photo(container, other, 2)

    perfume = await _load(container, mine)

    assert [p.id for p in perfume.photos] == [own]


async def test_a_perfume_without_photos_loads_an_empty_list(
    container: Container, refs: Refs
) -> None:
    assert (await _load(container, await _perfume(container, refs))).photos == []


# --- repository: save -------------------------------------------------------------------------


async def test_save_inserts_a_new_photo_and_keeps_the_existing_ones(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs)
    existing = await _photo(container, perfume_id, 1)
    new_photo = new_id()
    card_key, detail_key = photo_keys(perfume_id, new_photo)

    saved = await _change(
        container,
        perfume_id,
        lambda perfume: perfume.add_photo(
            new_photo, card_key, detail_key, 640, 960, created_at=NOW
        ),
    )

    rows = await _rows(container, perfume_id)
    assert [(row["id"], row["position"]) for row in rows] == [(existing, 1), (new_photo, 2)]
    added = rows[1]
    assert (added["card_key"], added["detail_key"]) == (card_key, detail_key)
    assert (added["width"], added["height"], added["created_at"]) == (640, 960, NOW)
    assert saved.removed_photo_ids == []


async def test_reordering_rewrites_every_position_despite_the_unique_constraint(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs)
    a, b, c = [await _photo(container, perfume_id, n) for n in (1, 2, 3)]

    await _change(container, perfume_id, lambda perfume: perfume.reorder_photos([c, b, a]))

    assert await _positions(container, perfume_id) == [(c, 1), (b, 2), (a, 3)]
    assert [p.id for p in (await _load(container, perfume_id)).photos] == [c, b, a]


async def test_swapping_two_photos_is_saved(container: Container, refs: Refs) -> None:
    perfume_id = await _perfume(container, refs)
    a, b = await _photo(container, perfume_id, 1), await _photo(container, perfume_id, 2)

    await _change(container, perfume_id, lambda perfume: perfume.reorder_photos([b, a]))

    assert await _positions(container, perfume_id) == [(b, 1), (a, 2)]


async def test_removing_a_photo_deletes_its_row_renumbers_the_rest_and_clears_the_removed_ids(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs)
    a, b, c = [await _photo(container, perfume_id, n) for n in (1, 2, 3)]

    saved = await _change(container, perfume_id, lambda perfume: perfume.remove_photo(a))

    assert await _positions(container, perfume_id) == [(b, 1), (c, 2)]
    assert saved.removed_photo_ids == []


async def test_removing_the_last_photo_leaves_none(container: Container, refs: Refs) -> None:
    perfume_id = await _perfume(container, refs)
    only = await _photo(container, perfume_id, 1)

    await _change(container, perfume_id, lambda perfume: perfume.remove_photo(only))

    assert await _rows(container, perfume_id) == []


async def test_removing_and_adding_in_one_save_reuses_the_freed_position(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs)
    a, b = await _photo(container, perfume_id, 1), await _photo(container, perfume_id, 2)
    new_photo = new_id()
    card_key, detail_key = photo_keys(perfume_id, new_photo)

    def mutate(perfume: Perfume) -> None:
        perfume.remove_photo(a)
        perfume.add_photo(new_photo, card_key, detail_key, 800, 800, created_at=NOW)

    await _change(container, perfume_id, mutate)

    assert await _positions(container, perfume_id) == [(b, 1), (new_photo, 2)]


async def test_removing_a_photo_does_not_touch_other_perfumes(
    container: Container, refs: Refs
) -> None:
    mine, other = await _perfume(container, refs, "Mine"), await _perfume(container, refs, "Other")
    own = await _photo(container, mine, 1)
    kept = await _photo(container, other, 1)

    await _change(container, mine, lambda perfume: perfume.remove_photo(own))

    assert await _positions(container, other) == [(kept, 1)]


async def test_save_only_updates_the_position_of_an_existing_photo(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs)
    photo = await _photo(container, perfume_id, 1, width=1200, height=900)

    def mutate(perfume: Perfume) -> None:
        perfume.photos[0].width = 1
        perfume.photos[0].card_key = "tampered"

    await _change(container, perfume_id, mutate)

    [row] = await _rows(container, perfume_id)
    assert (row["id"], row["width"], row["card_key"]) == (
        photo,
        1200,
        photo_keys(perfume_id, photo)[0],
    )


# --- the lock under concurrent uploads --------------------------------------------------------


async def test_concurrent_uploads_to_one_perfume_are_serialized_by_the_row_lock(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs)
    storage = InMemoryMediaStorage()
    add = AddPhoto(
        perfumes=SqlPerfumeRepository(container.database),
        images=FakeImageProcessor(),
        storage=storage,
        transactions=SqlTransactionRunner(container.database),
        clock=FixedClock(),
    )

    results = await asyncio.gather(*(add.execute(perfume_id, b"raw") for _ in range(5)))

    oks = [result for result in results if isinstance(result, Ok)]
    errors = [result for result in results if isinstance(result, Err)]
    assert len(oks) == 3
    assert {error.error.code for error in errors} == {"CATALOG_PHOTO_LIMIT_REACHED"}
    assert [position for _, position in await _positions(container, perfume_id)] == [1, 2, 3]
    stored = {row["card_key"] for row in await _rows(container, perfume_id)} | {
        row["detail_key"] for row in await _rows(container, perfume_id)
    }
    assert set(storage.objects) == stored  # the two refused uploads were compensated
    assert len(storage.deleted) == 4


# --- read side: URLs --------------------------------------------------------------------------


async def test_get_admin_lists_the_photos_by_position_with_urls_built_from_the_base_url(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs)
    second = await _photo(container, perfume_id, 2, width=800, height=1200)
    first = await _photo(container, perfume_id, 1)

    admin = await refs.queries.get_admin(perfume_id)

    assert admin is not None
    assert [(p.id, p.position) for p in admin.photos] == [(first, 1), (second, 2)]
    prefix = f"{MEDIA}/perfumes/{perfume_id}"
    assert admin.photos[0].card_url == f"{prefix}/{first}-card.webp"
    assert admin.photos[0].detail_url == f"{prefix}/{first}-detail.webp"
    assert (admin.photos[1].width, admin.photos[1].height) == (800, 1200)
    assert admin.photos[1].created_at == CREATED + timedelta(minutes=2)


async def test_get_admin_of_a_perfume_without_photos_has_an_empty_list(
    container: Container, refs: Refs
) -> None:
    admin = await refs.queries.get_admin(await _perfume(container, refs))

    assert admin is not None
    assert admin.photos == []


async def test_a_trailing_part_of_the_base_url_is_kept_verbatim(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs)
    photo = await _photo(container, perfume_id, 1)
    queries = SqlPerfumeQueries(container.database, media_base_url="https://cdn.example.test/m")

    admin = await queries.get_admin(perfume_id)

    assert admin is not None
    assert admin.photos[0].card_url == (
        f"https://cdn.example.test/m/perfumes/{perfume_id}/{photo}-card.webp"
    )


async def test_list_public_shows_the_card_url_of_position_1_or_none(
    container: Container, refs: Refs
) -> None:
    with_photos = await _perfume(container, refs, "Aaa", published=True, with_presentation=True)
    await _perfume(container, refs, "Bbb", published=True, with_presentation=True)
    await _photo(container, with_photos, 2)  # position 2 is not the card photo
    first = await _photo(container, with_photos, 1)

    page = await refs.queries.list_public(NO_FILTERS, now=NOW)

    cards = {card.name: card for card in page.items}
    assert cards["Aaa"].photo_url == f"{MEDIA}/perfumes/{with_photos}/{first}-card.webp"
    assert cards["Bbb"].photo_url is None


async def test_list_public_follows_a_reorder(container: Container, refs: Refs) -> None:
    perfume_id = await _perfume(container, refs, published=True, with_presentation=True)
    a, b = await _photo(container, perfume_id, 1), await _photo(container, perfume_id, 2)

    await _change(container, perfume_id, lambda perfume: perfume.reorder_photos([b, a]))

    [card] = (await refs.queries.list_public(NO_FILTERS, now=NOW)).items
    assert card.photo_url == f"{MEDIA}/perfumes/{perfume_id}/{b}-card.webp"


async def test_each_card_gets_the_photo_of_its_own_perfume(
    container: Container, refs: Refs
) -> None:
    ids = [
        await _perfume(container, refs, name, published=True, with_presentation=True)
        for name in ("Aaa", "Bbb", "Ccc")
    ]
    firsts = {perfume_id: await _photo(container, perfume_id, 1) for perfume_id in ids}

    page = await refs.queries.list_public(NO_FILTERS, now=NOW)

    assert page.total == 3
    assert [card.photo_url for card in page.items] == [
        f"{MEDIA}/perfumes/{perfume_id}/{firsts[perfume_id]}-card.webp" for perfume_id in ids
    ]


async def test_list_public_repeats_no_card_for_a_perfume_with_several_photos(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs, published=True, with_presentation=True)
    for position in (1, 2, 3):
        await _photo(container, perfume_id, position)

    page = await refs.queries.list_public(NO_FILTERS, now=NOW)

    assert (page.total, len(page.items)) == (1, 1)


async def test_get_public_lists_the_photos_in_order_with_only_the_two_urls(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs, published=True, with_presentation=True)
    a, b, c = [await _photo(container, perfume_id, n) for n in (1, 2, 3)]
    await _change(container, perfume_id, lambda perfume: perfume.reorder_photos([c, a, b]))
    slug = await _slug(container, perfume_id)

    public = await refs.queries.get_public(slug, now=NOW)

    assert public is not None
    prefix = f"{MEDIA}/perfumes/{perfume_id}"
    assert [p.model_dump() for p in public.photos] == [
        {"card_url": f"{prefix}/{pid}-card.webp", "detail_url": f"{prefix}/{pid}-detail.webp"}
        for pid in (c, a, b)
    ]


async def test_get_public_through_a_retired_slug_still_has_the_photos(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs, published=True, with_presentation=True)
    photo = await _photo(container, perfume_id, 1)
    async with container.database.engine.begin() as connection:
        await connection.execute(
            insert(perfume_slug_history).values(
                slug="old-slug", perfume_id=perfume_id, retired_at=CREATED
            )
        )

    public = await refs.queries.get_public("old-slug", now=NOW)

    assert public is not None
    assert [p.card_url.rsplit("/", 1)[1] for p in public.photos] == [f"{photo}-card.webp"]


async def test_a_hidden_perfume_does_not_leak_its_photos_publicly(
    container: Container, refs: Refs
) -> None:
    perfume_id = await _perfume(container, refs, published=False, with_presentation=True)
    await _photo(container, perfume_id, 1)

    assert await refs.queries.get_public(await _slug(container, perfume_id), now=NOW) is None
    assert (await refs.queries.list_public(NO_FILTERS, now=NOW)).items == []


async def _slug(container: Container, perfume_id: UUID) -> str:
    async with container.database.reader() as session:
        return str(await session.scalar(select(perfumes.c.slug).where(perfumes.c.id == perfume_id)))


# --- the real stack: Pillow + S3MediaStorage on RustFS + PostgreSQL, as wired in the container --


def _watch(bucket: Bucket, perfume_id: UUID) -> str:
    """The key prefix of a perfume's photos, registered for cleanup after the test."""
    prefix = f"perfumes/{perfume_id}/"
    bucket.created_prefixes.append(prefix)
    return prefix


async def _get(bucket: Bucket, key: str) -> Any:
    return await asyncio.to_thread(bucket.public_get, key)


async def _upload(container: Container, perfume_id: UUID, data: bytes) -> Any:
    return await container.services.get(AddPhoto).execute(perfume_id, data)


async def _admin(container: Container, perfume_id: UUID) -> AdminPerfume:
    result = await container.services.get(GetAdminPerfume).execute(perfume_id)
    assert isinstance(result, Ok), result
    return result.value


def _code(result: Any) -> str:
    assert isinstance(result, Err), result
    return str(result.error.code)


async def test_an_uploaded_jpeg_is_stored_as_two_public_webp_renditions_and_listed(
    container: Container, refs: Refs, bucket: Bucket, settings: Settings
) -> None:
    perfume_id = await _perfume(container, refs)
    prefix = _watch(bucket, perfume_id)

    result = await _upload(container, perfume_id, images.jpeg((2000, 1500)))

    assert isinstance(result, Ok), result
    admin = await _admin(container, perfume_id)
    [photo] = admin.photos
    assert photo.id == result.value
    assert (photo.position, photo.width, photo.height) == (1, 1200, 900)
    assert photo.card_url == f"{settings.media_public_base_url}/{prefix}{photo.id}-card.webp"
    assert photo.detail_url == f"{settings.media_public_base_url}/{prefix}{photo.id}-detail.webp"
    assert bucket.keys(prefix) == [
        f"{prefix}{photo.id}-card.webp",
        f"{prefix}{photo.id}-detail.webp",
    ]
    for url, size in ((photo.card_url, (400, 300)), (photo.detail_url, (1200, 900))):
        response = await asyncio.to_thread(httpx.get, url)  # no credentials
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/webp"
        assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
        decoded = images.open_webp(response.content)
        assert (decoded.format, decoded.size) == ("WEBP", size)
        assert dict(decoded.getexif()) == {}
        assert "icc_profile" not in decoded.info


async def test_a_png_with_transparency_stays_transparent_in_the_stored_object(
    container: Container, refs: Refs, bucket: Bucket
) -> None:
    perfume_id = await _perfume(container, refs)
    _watch(bucket, perfume_id)

    result = await _upload(container, perfume_id, images.png_with_transparency((1000, 800)))

    assert isinstance(result, Ok), result
    [photo] = (await _admin(container, perfume_id)).photos
    stored = images.open_webp((await asyncio.to_thread(httpx.get, photo.detail_url)).content)
    assert stored.mode == "RGBA"
    assert stored.getpixel((100, 400))[3] == 0  # type: ignore[index]
    assert stored.getpixel((900, 400))[3] == 255  # type: ignore[index]


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (images.gif((900, 900)), "CATALOG_PHOTO_FORMAT_INVALID"),
        (b"just text renamed .jpg", "CATALOG_PHOTO_FORMAT_INVALID"),
        (images.jpeg((500, 800)), "CATALOG_PHOTO_TOO_SMALL"),
    ],
    ids=["gif", "text", "tiny"],
)
async def test_a_refused_file_leaves_no_object_and_no_row(
    container: Container, refs: Refs, bucket: Bucket, data: bytes, code: str
) -> None:
    perfume_id = await _perfume(container, refs)
    prefix = _watch(bucket, perfume_id)

    result = await _upload(container, perfume_id, data)

    assert _code(result) == code
    assert bucket.keys(prefix) == []
    assert await _rows(container, perfume_id) == []


async def test_the_fourth_photo_is_refused_and_no_new_object_is_left_in_the_bucket(
    container: Container, refs: Refs, bucket: Bucket
) -> None:
    perfume_id = await _perfume(container, refs)
    prefix = _watch(bucket, perfume_id)
    for _ in range(3):
        assert isinstance(await _upload(container, perfume_id, images.jpeg((900, 900))), Ok)
    before = bucket.keys(prefix)
    assert len(before) == 6

    result = await _upload(container, perfume_id, images.jpeg((900, 900)))

    assert _code(result) == "CATALOG_PHOTO_LIMIT_REACHED"
    assert bucket.keys(prefix) == before
    assert len(await _rows(container, perfume_id)) == 3


async def test_an_archived_perfume_refuses_every_photo_operation_and_keeps_its_objects(
    container: Container, refs: Refs, bucket: Bucket
) -> None:
    perfume_id = await _perfume(container, refs)
    prefix = _watch(bucket, perfume_id)
    first = await _upload(container, perfume_id, images.jpeg((900, 900)))
    second = await _upload(container, perfume_id, images.jpeg((900, 900)))
    assert isinstance(first, Ok) and isinstance(second, Ok)
    async with container.database.engine.begin() as connection:
        await connection.execute(
            update(perfumes).where(perfumes.c.id == perfume_id).values(is_archived=True)
        )
    before = bucket.keys(prefix)

    added = await _upload(container, perfume_id, images.jpeg((900, 900)))
    removed = await container.services.get(RemovePhoto).execute(perfume_id, first.value)
    reordered = await container.services.get(ReorderPhotos).execute(
        perfume_id, [second.value, first.value]
    )

    assert [_code(added), _code(removed), _code(reordered)] == ["CATALOG_PERFUME_ARCHIVED"] * 3
    assert bucket.keys(prefix) == before
    assert await _positions(container, perfume_id) == [(first.value, 1), (second.value, 2)]


async def test_uploading_to_an_unknown_perfume_leaves_no_object(
    container: Container, refs: Refs, bucket: Bucket
) -> None:
    prefix = _watch(bucket, UNKNOWN_ID)

    result = await _upload(container, UNKNOWN_ID, images.jpeg((900, 900)))

    assert _code(result) == "CATALOG_PERFUME_NOT_FOUND"
    assert bucket.keys(prefix) == []


async def test_removing_a_photo_renumbers_the_rest_and_deletes_both_objects_from_the_bucket(
    container: Container, refs: Refs, bucket: Bucket
) -> None:
    perfume_id = await _perfume(container, refs)
    prefix = _watch(bucket, perfume_id)
    ids = [(await _upload(container, perfume_id, images.jpeg((900, 900)))).value for _ in range(3)]
    gone = (await _admin(container, perfume_id)).photos[1]

    result = await container.services.get(RemovePhoto).execute(perfume_id, ids[1])

    assert isinstance(result, Ok)
    assert await _positions(container, perfume_id) == [(ids[0], 1), (ids[2], 2)]
    assert len(bucket.keys(prefix)) == 4
    assert (await _get(bucket, f"{prefix}{ids[1]}-card.webp")).status_code == 404
    assert (await _get(bucket, f"{prefix}{ids[1]}-detail.webp")).status_code == 404
    assert gone.id == ids[1]
    for kept in (ids[0], ids[2]):
        assert (await _get(bucket, f"{prefix}{kept}-card.webp")).status_code == 200


async def test_removing_an_unknown_photo_is_not_found_and_deletes_nothing(
    container: Container, refs: Refs, bucket: Bucket
) -> None:
    perfume_id = await _perfume(container, refs)
    prefix = _watch(bucket, perfume_id)
    assert isinstance(await _upload(container, perfume_id, images.jpeg((900, 900))), Ok)
    before = bucket.keys(prefix)

    result = await container.services.get(RemovePhoto).execute(perfume_id, UNKNOWN_ID)

    assert _code(result) == "CATALOG_PHOTO_NOT_FOUND"
    assert bucket.keys(prefix) == before


async def test_removing_a_photo_whose_objects_are_already_gone_still_succeeds(
    container: Container, refs: Refs, bucket: Bucket
) -> None:
    perfume_id = await _perfume(container, refs)
    prefix = _watch(bucket, perfume_id)
    uploaded = await _upload(container, perfume_id, images.jpeg((900, 900)))
    assert isinstance(uploaded, Ok)
    await bucket.storage.delete(bucket.keys(prefix))

    result = await container.services.get(RemovePhoto).execute(perfume_id, uploaded.value)

    assert isinstance(result, Ok)
    assert await _rows(container, perfume_id) == []


async def test_reordering_through_the_container_changes_the_positions_of_the_admin_detail(
    container: Container, refs: Refs, bucket: Bucket
) -> None:
    perfume_id = await _perfume(container, refs)
    _watch(bucket, perfume_id)
    ids = [(await _upload(container, perfume_id, images.jpeg((900, 900)))).value for _ in range(3)]

    reorder = container.services.get(ReorderPhotos)
    assert isinstance(await reorder.execute(perfume_id, ids[::-1]), Ok)
    assert [(p.id, p.position) for p in (await _admin(container, perfume_id)).photos] == [
        (ids[2], 1),
        (ids[1], 2),
        (ids[0], 3),
    ]

    refused = await reorder.execute(perfume_id, [ids[0], ids[1], UNKNOWN_ID])
    assert _code(refused) == "CATALOG_PHOTO_ORDER_INVALID"
    assert [p.id for p in (await _admin(container, perfume_id)).photos] == ids[::-1]


async def test_a_storage_failure_raises_and_leaves_no_row(
    container: Container, refs: Refs, bucket: Bucket, settings: Settings
) -> None:
    perfume_id = await _perfume(container, refs)
    prefix = _watch(bucket, perfume_id)
    add = AddPhoto(
        perfumes=SqlPerfumeRepository(container.database),
        images=PillowImageProcessor(max_bytes=5_000_000),
        storage=S3MediaStorage(
            endpoint_url=settings.s3_endpoint_url,
            access_key=settings.s3_access_key,
            secret_key="definitely-not-the-secret",
            bucket=settings.s3_bucket,
            region=settings.s3_region,
        ),
        transactions=SqlTransactionRunner(container.database),
        clock=FixedClock(),
    )

    with pytest.raises(ClientError):
        await add.execute(perfume_id, images.jpeg((900, 900)))

    assert await _rows(container, perfume_id) == []
    assert bucket.keys(prefix) == []
