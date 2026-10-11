"""Plan 006: the photo routes (raw-body upload, delete, reorder) and the photo fields of the
admin detail and the public card and detail."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, Response

from fragancia_api.main.http import build_app
from fragancia_api.modules.catalog.application.commands.photos import (
    AddPhoto,
    PhotoUploadLimit,
    RemovePhoto,
    ReorderPhotos,
)
from fragancia_api.modules.catalog.application.queries.perfumes import (
    GetAdminPerfume,
    GetPublicPerfume,
    ListPublicPerfumes,
)
from fragancia_api.modules.catalog.domain.errors import PhotoFormatInvalid, PhotoTooSmall
from fragancia_api.modules.catalog.domain.perfume import Perfume
from fragancia_api.modules.catalog.http.perfume_router import perfume_routers
from fragancia_api.modules.catalog.infrastructure.in_memory_media import (
    FakeImageProcessor,
    InMemoryMediaStorage,
)
from fragancia_api.shared.application.actor import Actor, ActorResolver
from fragancia_api.shared.http import SESSION_COOKIE
from fragancia_api.shared.http.health import HealthChecks
from fragancia_api.shared.http.services import ServiceRegistry
from fragancia_api.shared.infrastructure.in_memory import FixedClock, InMemoryTransactionRunner
from tests.support import ADMIN_HEADERS, TestActorResolver, assert_admin_routes_are_protected
from tests.unit.catalog.perfume_support import (
    NOW,
    World,
    add_presentation,
    make_perfume,
    unwrap_ok,
)

ADMIN = "/api/v1/admin/perfumes"
PUBLIC = "/api/v1/perfumes"
MEDIA = "http://media.test"
MAX_BYTES = 100
UNKNOWN_ID = UUID(int=404)
NO_PERMISSION_SESSION = "admin-without-catalog-manage"
NO_PERMISSION_HEADERS = {"Cookie": f"{SESSION_COOKIE}={NO_PERMISSION_SESSION}"}
JPEG = ("photo.jpg", b"fake-image-bytes", "image/jpeg")


class _Resolver:
    async def resolve(self, token: str) -> Actor | None:
        if token == NO_PERMISSION_SESSION:
            return Actor(id="no-perm", is_admin=True, session_id=UUID(int=2))
        return await TestActorResolver().resolve(token)


@dataclass
class Rig:
    client: AsyncClient
    world: World
    perfume: Perfume
    images: FakeImageProcessor
    storage: InMemoryMediaStorage


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    world = World()
    perfume = world.seed(make_perfume(world.brand, world.family, world.concentration))
    add_presentation(perfume)
    unwrap_ok(perfume.publish(NOW))
    world.perfumes.by_id[perfume.id] = perfume
    images, storage = FakeImageProcessor(), InMemoryMediaStorage()
    transactions, clock = InMemoryTransactionRunner(), FixedClock()
    services = ServiceRegistry()
    services.add(ActorResolver, _Resolver())  # type: ignore[type-abstract]
    services.add(HealthChecks, HealthChecks())
    services.add(
        AddPhoto,
        AddPhoto(
            perfumes=world.perfumes,
            images=images,
            storage=storage,
            transactions=transactions,
            clock=clock,
        ),
    )
    services.add(
        RemovePhoto,
        RemovePhoto(perfumes=world.perfumes, storage=storage, transactions=transactions),
    )
    services.add(ReorderPhotos, ReorderPhotos(perfumes=world.perfumes, transactions=transactions))
    services.add(PhotoUploadLimit, PhotoUploadLimit(max_bytes=MAX_BYTES))
    services.add(GetAdminPerfume, GetAdminPerfume(world.perfumes))
    services.add(ListPublicPerfumes, ListPublicPerfumes(world.perfumes, clock))
    services.add(GetPublicPerfume, GetPublicPerfume(world.perfumes, clock))
    app = build_app(services, perfume_routers)
    assert_admin_routes_are_protected(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield Rig(client, world, perfume, images, storage)


def _code(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


async def _upload(
    rig: Rig, file: tuple[str, bytes, str] | None = JPEG, *, perfume_id: UUID | None = None
) -> Response:
    _, body, content_type = file if file is not None else JPEG
    return await rig.client.post(
        f"{ADMIN}/{perfume_id or rig.perfume.id}/photos",
        content=body,
        headers={**ADMIN_HEADERS, "content-type": content_type},
    )


async def _uploaded(rig: Rig, count: int) -> list[str]:
    ids = []
    for _ in range(count):
        response = await _upload(rig)
        assert response.status_code == 201, response.text
        ids.append(str(response.json()["id"]))
    return ids


async def _admin_photos(rig: Rig) -> list[dict[str, Any]]:
    response = await rig.client.get(f"{ADMIN}/{rig.perfume.id}", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    return list(response.json()["photos"])


# --- access: the 3 new routes -----------------------------------------------------------------

_SOME = str(UNKNOWN_ID)
_ROUTES: list[tuple[str, str, dict[str, Any]]] = [
    ("POST", f"{ADMIN}/{_SOME}/photos", {"content": JPEG[1]}),
    ("DELETE", f"{ADMIN}/{_SOME}/photos/{_SOME}", {}),
    ("PUT", f"{ADMIN}/{_SOME}/photos/order", {"json": {"photo_ids": []}}),
]


@pytest.mark.parametrize(("method", "url", "kwargs"), _ROUTES)
async def test_photo_routes_return_401_without_a_session(
    rig: Rig, method: str, url: str, kwargs: dict[str, Any]
) -> None:
    response = await rig.client.request(method, url, **kwargs)

    assert _code(response) == (401, "AUTHENTICATION_REQUIRED")
    assert rig.images.calls == []


@pytest.mark.parametrize(("method", "url", "kwargs"), _ROUTES)
async def test_photo_routes_return_403_for_an_admin_without_catalog_manage(
    rig: Rig, method: str, url: str, kwargs: dict[str, Any]
) -> None:
    response = await rig.client.request(method, url, headers=NO_PERMISSION_HEADERS, **kwargs)

    assert _code(response) == (403, "FORBIDDEN")
    assert rig.images.calls == []


# --- upload -----------------------------------------------------------------------------------


async def test_a_raw_body_upload_is_201_and_the_admin_detail_lists_the_photo(rig: Rig) -> None:
    response = await _upload(rig)

    assert response.status_code == 201
    photo_id = response.json()["id"]
    assert rig.images.calls == [JPEG[1]]
    [photo] = await _admin_photos(rig)
    assert photo["id"] == photo_id
    assert (photo["position"], photo["width"], photo["height"]) == (1, 1200, 900)
    assert photo["card_url"] == f"{MEDIA}/perfumes/{rig.perfume.id}/{photo_id}-card.webp"
    assert photo["detail_url"] == f"{MEDIA}/perfumes/{rig.perfume.id}/{photo_id}-detail.webp"
    assert set(photo) == {
        "id",
        "position",
        "card_url",
        "detail_url",
        "width",
        "height",
        "created_at",
    }
    assert len(rig.storage.objects) == 2


async def test_an_upload_exactly_at_the_limit_is_accepted(rig: Rig) -> None:
    response = await _upload(rig, ("photo.jpg", b"x" * MAX_BYTES, "image/jpeg"))

    assert response.status_code == 201
    assert rig.images.calls == [b"x" * MAX_BYTES]


async def test_an_upload_over_the_limit_is_422_too_large_without_calling_the_use_case(
    rig: Rig,
) -> None:
    response = await _upload(rig, ("photo.jpg", b"x" * (MAX_BYTES + 1), "image/jpeg"))

    assert _code(response) == (422, "CATALOG_PHOTO_TOO_LARGE")
    assert response.json()["details"] == {"max_bytes": MAX_BYTES}
    assert rig.images.calls == []
    assert rig.storage.objects == {}
    assert await _admin_photos(rig) == []


class _Stream:
    """A chunked request body that counts how many chunks the server pulled from it."""

    def __init__(self, chunks: int, size: int = 1024) -> None:
        self.total = chunks
        self._size = size
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self.total):
            self.pulled += 1
            yield b"x" * self._size


async def _stream_upload(
    rig: Rig, stream: _Stream, headers: dict[str, str] | None = None
) -> Response:
    return await rig.client.post(
        f"{ADMIN}/{rig.perfume.id}/photos",
        content=stream,
        headers={**(ADMIN_HEADERS if headers is None else headers), "content-type": "image/jpeg"},
    )


async def test_a_streamed_body_over_the_limit_is_422_too_large_and_cut_off(rig: Rig) -> None:
    stream = _Stream(chunks=1000)  # 1 MB announced by nobody (chunked), limit is 100 bytes

    response = await _stream_upload(rig, stream)

    assert _code(response) == (422, "CATALOG_PHOTO_TOO_LARGE")
    assert response.json()["details"] == {"max_bytes": MAX_BYTES}
    assert 1 <= stream.pulled < stream.total  # stopped as soon as the limit passed
    assert rig.images.calls == []
    assert rig.storage.objects == {}


async def test_a_streamed_body_exactly_at_the_limit_is_accepted(rig: Rig) -> None:
    response = await _stream_upload(rig, _Stream(chunks=4, size=MAX_BYTES // 4))

    assert response.status_code == 201, response.text
    assert rig.images.calls == [b"x" * MAX_BYTES]


async def test_a_declared_content_length_over_the_limit_is_422_without_reading_the_body(
    rig: Rig,
) -> None:
    stream = _Stream(chunks=3)

    response = await _stream_upload(
        rig, stream, {**ADMIN_HEADERS, "content-length": str(MAX_BYTES + 1)}
    )

    assert _code(response) == (422, "CATALOG_PHOTO_TOO_LARGE")
    assert response.json()["details"] == {"max_bytes": MAX_BYTES}
    assert stream.pulled == 0
    assert rig.images.calls == []


async def test_an_unauthenticated_streamed_body_is_401_and_no_chunk_is_consumed(rig: Rig) -> None:
    stream = _Stream(chunks=1000)

    response = await _stream_upload(rig, stream, headers={})

    assert _code(response) == (401, "AUTHENTICATION_REQUIRED")
    assert stream.pulled == 0
    assert rig.images.calls == []


async def test_a_streamed_body_without_catalog_manage_is_403_and_no_chunk_is_consumed(
    rig: Rig,
) -> None:
    stream = _Stream(chunks=1000)

    response = await _stream_upload(rig, stream, headers=NO_PERMISSION_HEADERS)

    assert _code(response) == (403, "FORBIDDEN")
    assert stream.pulled == 0


async def test_a_json_body_is_422_format_invalid_by_its_content_type(rig: Rig) -> None:
    response = await rig.client.post(
        f"{ADMIN}/{rig.perfume.id}/photos", json={"file": "x"}, headers=ADMIN_HEADERS
    )

    assert _code(response) == (422, "CATALOG_PHOTO_FORMAT_INVALID")
    assert rig.images.calls == []


async def test_an_empty_body_is_422_format_invalid_without_calling_the_processor(
    rig: Rig,
) -> None:
    response = await _upload(rig, ("empty.jpg", b"", "image/jpeg"))

    assert _code(response) == (422, "CATALOG_PHOTO_FORMAT_INVALID")
    assert rig.images.calls == []
    assert rig.storage.objects == {}


async def test_a_content_type_outside_the_three_image_types_is_422_format_invalid(
    rig: Rig,
) -> None:
    response = await _upload(rig, ("notes.txt", b"bytes", "text/plain"))

    assert _code(response) == (422, "CATALOG_PHOTO_FORMAT_INVALID")
    assert rig.images.calls == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (PhotoFormatInvalid(details={"formats": ["jpeg", "png", "webp"]}), "FORMAT_INVALID"),
        (PhotoTooSmall(details={"min_side": 600}), "TOO_SMALL"),
    ],
)
async def test_processor_errors_are_422_with_their_codes(
    rig: Rig, error: PhotoFormatInvalid | PhotoTooSmall, code: str
) -> None:
    rig.images.error = error

    response = await _upload(rig)

    assert _code(response) == (422, f"CATALOG_PHOTO_{code}")
    assert response.json()["details"] == error.details
    assert rig.storage.objects == {}


async def test_the_fourth_upload_is_409_and_leaves_no_new_object(rig: Rig) -> None:
    await _uploaded(rig, 3)
    kept = dict(rig.storage.objects)

    response = await _upload(rig)

    assert _code(response) == (409, "CATALOG_PHOTO_LIMIT_REACHED")
    assert response.json()["details"] == {"max": 3}
    assert rig.storage.objects == kept
    assert len(await _admin_photos(rig)) == 3


async def test_uploading_to_an_unknown_perfume_is_404_and_leaves_no_object(rig: Rig) -> None:
    response = await _upload(rig, perfume_id=UNKNOWN_ID)

    assert _code(response) == (404, "CATALOG_PERFUME_NOT_FOUND")
    assert rig.storage.objects == {}


async def test_uploading_to_an_archived_perfume_is_422(rig: Rig) -> None:
    rig.world.perfumes.by_id[rig.perfume.id].archive()

    response = await _upload(rig)

    assert _code(response) == (422, "CATALOG_PERFUME_ARCHIVED")
    assert rig.storage.objects == {}


async def test_a_malformed_perfume_id_is_a_validation_error(rig: Rig) -> None:
    response = await rig.client.post(
        f"{ADMIN}/not-a-uuid/photos",
        content=JPEG[1],
        headers={**ADMIN_HEADERS, "content-type": "image/jpeg"},
    )

    assert _code(response) == (422, "VALIDATION_ERROR")


# --- delete -----------------------------------------------------------------------------------


async def test_delete_is_204_renumbers_the_rest_and_deletes_the_objects(rig: Rig) -> None:
    first, second, third = await _uploaded(rig, 3)

    response = await rig.client.delete(
        f"{ADMIN}/{rig.perfume.id}/photos/{second}", headers=ADMIN_HEADERS
    )

    assert response.status_code == 204
    assert response.content == b""
    assert [(p["id"], p["position"]) for p in await _admin_photos(rig)] == [(first, 1), (third, 2)]
    assert len(rig.storage.deleted) == 2
    assert all(second in key for key in rig.storage.deleted)


async def test_deleting_an_unknown_photo_is_404_photo_not_found(rig: Rig) -> None:
    await _uploaded(rig, 1)

    response = await rig.client.delete(
        f"{ADMIN}/{rig.perfume.id}/photos/{UNKNOWN_ID}", headers=ADMIN_HEADERS
    )

    assert _code(response) == (404, "CATALOG_PHOTO_NOT_FOUND")
    assert rig.storage.deleted == []


async def test_deleting_a_photo_of_an_unknown_perfume_is_404_perfume_not_found(rig: Rig) -> None:
    response = await rig.client.delete(
        f"{ADMIN}/{UNKNOWN_ID}/photos/{UNKNOWN_ID}", headers=ADMIN_HEADERS
    )

    assert _code(response) == (404, "CATALOG_PERFUME_NOT_FOUND")


async def test_deleting_a_photo_of_an_archived_perfume_is_422(rig: Rig) -> None:
    [photo_id] = await _uploaded(rig, 1)
    rig.world.perfumes.by_id[rig.perfume.id].archive()

    response = await rig.client.delete(
        f"{ADMIN}/{rig.perfume.id}/photos/{photo_id}", headers=ADMIN_HEADERS
    )

    assert _code(response) == (422, "CATALOG_PERFUME_ARCHIVED")
    assert rig.storage.deleted == []


# --- reorder ----------------------------------------------------------------------------------


async def _reorder(rig: Rig, photo_ids: list[str], perfume_id: UUID | None = None) -> Response:
    return await rig.client.put(
        f"{ADMIN}/{perfume_id or rig.perfume.id}/photos/order",
        json={"photo_ids": photo_ids},
        headers=ADMIN_HEADERS,
    )


async def test_reorder_is_204_and_the_positions_follow(rig: Rig) -> None:
    first, second, third = await _uploaded(rig, 3)

    response = await _reorder(rig, [third, second, first])

    assert response.status_code == 204
    assert response.content == b""
    assert [(p["id"], p["position"]) for p in await _admin_photos(rig)] == [
        (third, 1),
        (second, 2),
        (first, 3),
    ]


@pytest.mark.parametrize("shape", ["missing", "foreign", "duplicated", "extra"])
async def test_an_order_that_is_not_the_current_set_is_422(rig: Rig, shape: str) -> None:
    first, second = await _uploaded(rig, 2)
    bodies = {
        "missing": [first],
        "foreign": [first, str(UNKNOWN_ID)],
        "duplicated": [first, first],
        "extra": [first, second, str(UNKNOWN_ID)],
    }

    response = await _reorder(rig, bodies[shape])

    assert _code(response) == (422, "CATALOG_PHOTO_ORDER_INVALID")
    assert [p["id"] for p in await _admin_photos(rig)] == [first, second]


async def test_more_than_three_ids_is_a_validation_error_from_the_contract(rig: Rig) -> None:
    response = await _reorder(rig, [str(UNKNOWN_ID)] * 4)

    assert _code(response) == (422, "VALIDATION_ERROR")


async def test_a_body_without_photo_ids_is_a_validation_error(rig: Rig) -> None:
    response = await rig.client.put(
        f"{ADMIN}/{rig.perfume.id}/photos/order", json={}, headers=ADMIN_HEADERS
    )

    assert _code(response) == (422, "VALIDATION_ERROR")


async def test_a_non_uuid_photo_id_is_a_validation_error(rig: Rig) -> None:
    assert _code(await _reorder(rig, ["nope"])) == (422, "VALIDATION_ERROR")


async def test_reordering_an_unknown_perfume_is_404(rig: Rig) -> None:
    response = await _reorder(rig, [], perfume_id=UNKNOWN_ID)

    assert _code(response) == (404, "CATALOG_PERFUME_NOT_FOUND")


async def test_reordering_an_archived_perfume_is_422(rig: Rig) -> None:
    first, second = await _uploaded(rig, 2)
    rig.world.perfumes.by_id[rig.perfume.id].archive()

    response = await _reorder(rig, [second, first])

    assert _code(response) == (422, "CATALOG_PERFUME_ARCHIVED")


async def test_order_is_a_route_of_its_own_not_a_photo_id(rig: Rig) -> None:
    """`PUT .../photos/order` must not be swallowed by a `{photo_id}` path parameter."""
    response = await _reorder(rig, [])

    assert response.status_code == 204


# --- the photo fields of the read models ------------------------------------------------------


async def test_the_admin_detail_has_an_empty_photo_list_without_photos(rig: Rig) -> None:
    assert await _admin_photos(rig) == []


async def test_the_public_card_has_a_null_photo_url_without_photos(rig: Rig) -> None:
    response = await rig.client.get(PUBLIC)

    [card] = response.json()["items"]
    assert card["photo_url"] is None


async def test_the_public_card_shows_the_card_url_of_position_1_and_follows_a_reorder(
    rig: Rig,
) -> None:
    first, second = await _uploaded(rig, 2)
    prefix = f"{MEDIA}/perfumes/{rig.perfume.id}"

    [card] = (await rig.client.get(PUBLIC)).json()["items"]
    assert card["photo_url"] == f"{prefix}/{first}-card.webp"

    await _reorder(rig, [second, first])

    [card] = (await rig.client.get(PUBLIC)).json()["items"]
    assert card["photo_url"] == f"{prefix}/{second}-card.webp"


async def test_the_public_detail_lists_every_photo_in_order_with_only_the_two_urls(
    rig: Rig,
) -> None:
    first, second, third = await _uploaded(rig, 3)
    await _reorder(rig, [third, first, second])
    prefix = f"{MEDIA}/perfumes/{rig.perfume.id}"

    response = await rig.client.get(f"{PUBLIC}/{rig.perfume.slug}")

    assert response.status_code == 200
    assert response.json()["photos"] == [
        {"card_url": f"{prefix}/{pid}-card.webp", "detail_url": f"{prefix}/{pid}-detail.webp"}
        for pid in (third, first, second)
    ]


async def test_the_public_detail_has_an_empty_photo_list_without_photos(rig: Rig) -> None:
    response = await rig.client.get(f"{PUBLIC}/{rig.perfume.slug}")

    assert response.json()["photos"] == []


async def test_a_removed_photo_disappears_from_the_public_detail(rig: Rig) -> None:
    first, second = await _uploaded(rig, 2)
    await rig.client.delete(f"{ADMIN}/{rig.perfume.id}/photos/{first}", headers=ADMIN_HEADERS)

    body = (await rig.client.get(f"{PUBLIC}/{rig.perfume.slug}")).json()

    assert [p["card_url"].rsplit("/", 1)[1] for p in body["photos"]] == [f"{second}-card.webp"]
