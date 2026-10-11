import asyncio
from collections.abc import Sequence
from typing import Any

import boto3  # type: ignore[import-untyped]  # no stubs; the client is typed Any below
from botocore.config import Config  # type: ignore[import-untyped]

_DELETE_BATCH = 1000  # S3 `delete_objects` accepts at most 1000 keys per call.


class S3MediaStorage:
    """S3-compatible object storage (RustFS locally). boto3 is blocking, so every call runs in a
    worker thread (`asyncio.to_thread`). The client is created lazily, on first use."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str | None,
        secret_key: str | None,
        bucket: str,
        region: str,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._region = region
        self._client: Any = None

    def _s3(self) -> Any:
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                region_name=self._region,
                config=Config(
                    s3={"addressing_style": "path"},
                    connect_timeout=5,
                    read_timeout=30,
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        return self._client

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self._s3().put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            # Keys contain a fresh photo id, so an object never changes.
            CacheControl="public, max-age=31536000, immutable",
        )

    async def delete(self, keys: Sequence[str]) -> None:
        """Delete the keys; raises, naming each failed key with its S3 code, if any failed."""
        failed: list[str] = []
        for start in range(0, len(keys), _DELETE_BATCH):
            batch = keys[start : start + _DELETE_BATCH]
            response = await asyncio.to_thread(
                self._s3().delete_objects,
                Bucket=self._bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
            failed.extend(
                f"{error.get('Key')}: {error.get('Code')}" for error in response.get("Errors", [])
            )
        if failed:
            raise RuntimeError(f"S3 failed to delete {len(failed)} object(s): {'; '.join(failed)}")
