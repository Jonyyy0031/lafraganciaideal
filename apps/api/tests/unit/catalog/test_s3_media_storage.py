"""Plan 006: how `S3MediaStorage` talks to boto3 (a recording fake client; no network)."""

from collections.abc import Iterator
from typing import Any

import pytest

from fragancia_api.modules.catalog.infrastructure.s3_media_storage import S3MediaStorage


class _FakeClient:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)

    delete_response: dict[str, Any] | None = {}
    delete_responses: Iterator[dict[str, Any]] | None = None

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self.deletes.append(kwargs)
        if self.delete_responses is not None:
            return next(self.delete_responses)
        return self.delete_response or {}


class _Factory:
    """Stands in for `boto3.client` and records how it was called."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.client = _FakeClient()

    def __call__(self, *args: Any, **kwargs: Any) -> _FakeClient:
        self.calls.append((args, kwargs))
        return self.client


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> _Factory:
    factory = _Factory()
    monkeypatch.setattr("boto3.client", factory)
    return factory


def _storage() -> S3MediaStorage:
    return S3MediaStorage(
        endpoint_url="http://s3.test:9100",
        access_key="access",
        secret_key="secret",
        bucket="media-bucket",
        region="eu-west-1",
    )


def test_the_client_is_not_created_until_it_is_used(factory: _Factory) -> None:
    _storage()

    assert factory.calls == []


async def test_the_client_is_created_once_with_the_configured_endpoint_credentials_and_region(
    factory: _Factory,
) -> None:
    storage = _storage()

    await storage.put("perfumes/a/b-card.webp", b"1", "image/webp")
    await storage.put("perfumes/a/b-detail.webp", b"2", "image/webp")
    await storage.delete(["perfumes/a/b-card.webp"])

    [(args, kwargs)] = factory.calls
    assert args == ("s3",)
    assert kwargs["endpoint_url"] == "http://s3.test:9100"
    assert kwargs["aws_access_key_id"] == "access"
    assert kwargs["aws_secret_access_key"] == "secret"
    assert kwargs["region_name"] == "eu-west-1"
    assert kwargs["config"].s3 == {"addressing_style": "path"}


async def test_put_sends_the_bytes_the_content_type_and_an_immutable_cache_header(
    factory: _Factory,
) -> None:
    await _storage().put("perfumes/a/b-card.webp", b"webp-bytes", "image/webp")

    assert factory.client.puts == [
        {
            "Bucket": "media-bucket",
            "Key": "perfumes/a/b-card.webp",
            "Body": b"webp-bytes",
            "ContentType": "image/webp",
            "CacheControl": "public, max-age=31536000, immutable",
        }
    ]


async def test_delete_sends_every_key_in_one_quiet_request(factory: _Factory) -> None:
    await _storage().delete(["k1", "k2"])

    assert factory.client.deletes == [
        {
            "Bucket": "media-bucket",
            "Delete": {"Objects": [{"Key": "k1"}, {"Key": "k2"}], "Quiet": True},
        }
    ]


async def test_delete_without_keys_makes_no_request(factory: _Factory) -> None:
    await _storage().delete([])

    assert factory.client.deletes == []


async def test_delete_splits_more_than_1000_keys_into_batches(factory: _Factory) -> None:
    keys = [f"k{n}" for n in range(2001)]

    await _storage().delete(keys)

    sizes = [len(call["Delete"]["Objects"]) for call in factory.client.deletes]
    assert sizes == [1000, 1000, 1]
    sent = [obj["Key"] for call in factory.client.deletes for obj in call["Delete"]["Objects"]]
    assert sent == keys


async def test_the_client_has_timeouts_and_standard_retries(factory: _Factory) -> None:
    await _storage().put("k", b"x", "image/webp")

    config = factory.calls[0][1]["config"]
    assert config.connect_timeout == 5
    assert config.read_timeout == 30
    assert config.retries == {"max_attempts": 3, "mode": "standard"}


async def test_delete_errors_reported_in_the_response_are_raised_with_key_and_code(
    factory: _Factory,
) -> None:
    """S3 answers 200 with an `Errors` list for per-key failures such as AccessDenied."""
    factory.client.delete_response = {
        "Errors": [
            {"Key": "perfumes/a/b-card.webp", "Code": "AccessDenied", "Message": "no"},
            {"Key": "perfumes/a/b-detail.webp", "Code": "InternalError", "Message": "boom"},
        ]
    }

    with pytest.raises(RuntimeError) as raised:
        await _storage().delete(["perfumes/a/b-card.webp", "perfumes/a/b-detail.webp"])

    message = str(raised.value)
    assert "perfumes/a/b-card.webp: AccessDenied" in message
    assert "perfumes/a/b-detail.webp: InternalError" in message
    assert "2 object(s)" in message


async def test_errors_of_every_batch_are_collected_before_raising(factory: _Factory) -> None:
    responses = iter(
        [
            {"Errors": [{"Key": "k0", "Code": "AccessDenied"}]},
            {},
            {"Errors": [{"Key": "k2000", "Code": "SlowDown"}]},
        ]
    )
    factory.client.delete_response = None
    factory.client.delete_responses = responses

    with pytest.raises(RuntimeError) as raised:
        await _storage().delete([f"k{n}" for n in range(2001)])

    assert len(factory.client.deletes) == 3  # no batch is skipped after a failure
    assert "k0: AccessDenied" in str(raised.value)
    assert "k2000: SlowDown" in str(raised.value)


async def test_an_empty_errors_list_is_success(factory: _Factory) -> None:
    factory.client.delete_response = {"Errors": []}

    await _storage().delete(["k1"])


async def test_a_client_error_propagates_to_the_caller(factory: _Factory) -> None:
    def boom(**kwargs: Any) -> None:
        raise ConnectionError("rustfs down")

    factory.client.put_object = boom  # type: ignore[method-assign]

    with pytest.raises(ConnectionError):
        await _storage().put("k", b"x", "image/webp")
