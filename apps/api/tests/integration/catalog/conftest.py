"""Fixtures for the catalog photo integration tests: the local RustFS bucket from bootstrap."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import boto3  # type: ignore[import-untyped]
import httpx
import pytest
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError, EndpointConnectionError  # type: ignore[import-untyped]

from fragancia_api.config import Settings
from fragancia_api.modules.catalog.infrastructure.s3_media_storage import S3MediaStorage


@dataclass
class Bucket:
    """The test bucket: the adapter under test, a raw boto3 client to inspect it, and the
    public URLs the storefront would load."""

    storage: S3MediaStorage
    client: Any
    name: str
    public_base_url: str
    created_prefixes: list[str]

    def keys(self, prefix: str) -> list[str]:
        response = self.client.list_objects_v2(Bucket=self.name, Prefix=prefix)
        return sorted(item["Key"] for item in response.get("Contents", []))

    def public_get(self, key: str) -> httpx.Response:
        """GET the object through its public URL, with no credentials."""
        return httpx.get(f"{self.public_base_url}/{key}")


def _secret(settings: Settings) -> str | None:
    return settings.s3_secret_key.get_secret_value() if settings.s3_secret_key else None


@pytest.fixture
def bucket(settings: Settings) -> Iterator[Bucket]:
    """The bootstrapped bucket on local RustFS. Skips when it is not reachable."""
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=_secret(settings),
        region_name=settings.s3_region,
        config=Config(
            s3={"addressing_style": "path"},
            connect_timeout=3,
            read_timeout=10,
            retries={"max_attempts": 1},
        ),
    )
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except EndpointConnectionError:
        pytest.skip(
            f"NOT CONFIRMED: RustFS not reachable at {settings.s3_endpoint_url} "
            "(run `uv run just up` and `uv run just bootstrap`)"
        )
    except ClientError as error:
        code = str(error.response["Error"]["Code"])
        if code not in {"404", "NoSuchBucket"}:
            raise  # bad credentials or the like: a real failure, not a missing service
        pytest.skip(
            f"NOT CONFIRMED: bucket {settings.s3_bucket!r} does not exist; "
            "run `uv run just bootstrap`"
        )
    storage = S3MediaStorage(
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=_secret(settings),
        bucket=settings.s3_bucket,
        region=settings.s3_region,
    )
    handle = Bucket(
        storage,
        client,
        settings.s3_bucket,
        settings.media_public_base_url,
        [],
    )
    yield handle
    # Leave nothing behind: delete whatever the test created under its own prefixes.
    for prefix in handle.created_prefixes:
        leftovers = handle.keys(prefix)
        if leftovers:
            client.delete_objects(
                Bucket=handle.name,
                Delete={"Objects": [{"Key": key} for key in leftovers], "Quiet": True},
            )
