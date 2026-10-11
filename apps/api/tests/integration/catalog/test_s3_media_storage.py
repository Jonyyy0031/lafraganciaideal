"""Plan 006: `S3MediaStorage` against the local RustFS bucket created by `just bootstrap`."""

import asyncio

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from fragancia_api.config import Settings
from fragancia_api.modules.catalog.infrastructure.s3_media_storage import S3MediaStorage
from fragancia_api.shared.kernel import new_id
from tests.integration.catalog import images
from tests.integration.catalog.conftest import Bucket

pytestmark = pytest.mark.integration


def _prefix(bucket: Bucket) -> str:
    prefix = f"perfumes/{new_id()}/"
    bucket.created_prefixes.append(prefix)
    return prefix


async def test_a_put_object_is_served_publicly_without_credentials_with_its_headers(
    bucket: Bucket,
) -> None:
    key = f"{_prefix(bucket)}{new_id()}-card.webp"
    body = images.encode(images.two_tone((50, 50)), "WEBP")

    await bucket.storage.put(key, body, "image/webp")

    response = await asyncio.to_thread(bucket.public_get, key)
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


async def test_delete_removes_the_objects_and_the_public_url_stops_answering(
    bucket: Bucket,
) -> None:
    prefix = _prefix(bucket)
    keep, gone_a, gone_b = (f"{prefix}{name}.webp" for name in ("keep", "a", "b"))
    for key in (keep, gone_a, gone_b):
        await bucket.storage.put(key, b"data", "image/webp")

    await bucket.storage.delete([gone_a, gone_b])

    assert bucket.keys(prefix) == [keep]
    assert (await asyncio.to_thread(bucket.public_get, gone_a)).status_code == 404
    assert (await asyncio.to_thread(bucket.public_get, keep)).status_code == 200


async def test_deleting_a_missing_key_is_not_an_error_even_next_to_a_present_one(
    bucket: Bucket,
) -> None:
    prefix = _prefix(bucket)
    present = f"{prefix}present.webp"
    await bucket.storage.put(present, b"data", "image/webp")

    await bucket.storage.delete([f"{prefix}never-existed.webp", present])
    await bucket.storage.delete([present])  # already gone

    assert bucket.keys(prefix) == []


async def test_deleting_no_keys_is_a_no_op(bucket: Bucket) -> None:
    await bucket.storage.delete([])


async def test_putting_the_same_key_again_replaces_the_object(bucket: Bucket) -> None:
    key = f"{_prefix(bucket)}same.webp"

    await bucket.storage.put(key, b"first", "image/webp")
    await bucket.storage.put(key, b"second", "image/webp")

    assert (await asyncio.to_thread(bucket.public_get, key)).content == b"second"


async def test_a_wrong_secret_key_makes_put_raise_instead_of_silently_succeeding(
    bucket: Bucket, settings: Settings
) -> None:
    storage = S3MediaStorage(
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key="definitely-not-the-secret",
        bucket=settings.s3_bucket,
        region=settings.s3_region,
    )
    key = f"{_prefix(bucket)}denied.webp"

    with pytest.raises(ClientError):
        await storage.put(key, b"data", "image/webp")

    assert bucket.keys(key) == []


async def test_the_public_prefix_does_not_expose_other_prefixes_without_credentials(
    bucket: Bucket,
) -> None:
    """Only `perfumes/*` is public-read (README decision 49): anything else answers 403/404."""
    key = f"not-public/{new_id()}.webp"
    bucket.created_prefixes.append(key)
    await bucket.storage.put(key, b"secret", "image/webp")

    response = await asyncio.to_thread(bucket.public_get, key)

    assert response.status_code in {403, 404}
