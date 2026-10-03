from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from infra import (
    ensure_bucket,
    ensure_database_in_container,
    ensure_env_file,
    ensure_env_keys,
    missing_tools,
    read_env,
)


class FakeCompose:
    """Stand-in for subprocess.run against `docker compose exec postgres psql`."""

    def __init__(self, existing: set[str]):
        self.existing = existing
        self.commands: list[list[str]] = []

    def __call__(self, command, **_):
        self.commands.append(command)
        sql = command[-1]
        if command[-2] == "-tAc":  # existence check
            name = sql.split("'")[1]
            return SimpleNamespace(stdout="1\n" if name in self.existing else "\n")
        return SimpleNamespace(stdout="CREATE DATABASE\n")


class FakeS3:
    def __init__(self, existing: set[str]):
        self.existing = existing
        self.created: list[str] = []

    def head_bucket(self, Bucket):  # noqa: N803 (boto3 signature)
        if Bucket not in self.existing:
            raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")

    def create_bucket(self, Bucket):  # noqa: N803
        self.created.append(Bucket)
        self.existing.add(Bucket)


def test_ensure_env_file_copies_when_missing(tmp_path: Path):
    example = tmp_path / ".env.example"
    example.write_text("A=1\n")
    target = tmp_path / ".env"

    assert ensure_env_file(example, target) is True
    assert target.read_text() == "A=1\n"


def test_ensure_env_file_never_overwrites(tmp_path: Path):
    example = tmp_path / ".env.example"
    example.write_text("A=1\n")
    target = tmp_path / ".env"
    target.write_text("A=custom\n")

    assert ensure_env_file(example, target) is False
    assert target.read_text() == "A=custom\n"


def test_ensure_env_keys_appends_only_missing_keys(tmp_path: Path):
    example = tmp_path / ".env.example"
    example.write_text("# comment\nA=1\nB=2\n\nC=3\n")
    target = tmp_path / ".env"
    target.write_text("A=custom\nC=mine\n")

    assert ensure_env_keys(example, target) == ["B"]
    assert read_env(target) == {"A": "custom", "B": "2", "C": "mine"}
    assert ensure_env_keys(example, target) == []  # idempotent


def test_read_env_ignores_comments_and_blank_lines(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("# comment\n\nA=1\nB = two words \nC=\n")

    assert read_env(env) == {"A": "1", "B": "two words", "C": ""}


def test_missing_tools_reports_only_absent_ones():
    available = {"docker", "uv"}

    assert missing_tools(["docker", "uv", "just"], which=lambda t: t in available or None) == [
        "just"
    ]


COMPOSE = ["docker", "compose", "-f", "compose.yaml"]


def test_ensure_database_in_container_creates_when_missing():
    compose = FakeCompose(existing={"fragancia"})

    assert ensure_database_in_container(COMPOSE, "fragancia_test", run=compose) is True
    create = compose.commands[-1]
    assert create[:7] == [*COMPOSE, "exec", "-T", "postgres"]
    assert create[-2:] == ["-c", 'CREATE DATABASE "fragancia_test"']


def test_ensure_database_in_container_is_idempotent():
    compose = FakeCompose(existing={"fragancia", "fragancia_test"})

    assert ensure_database_in_container(COMPOSE, "fragancia_test", run=compose) is False
    assert len(compose.commands) == 1  # only the existence check


@pytest.mark.parametrize("name", ["x; DROP DATABASE fragancia", "Fragancia", "a-b", ""])
def test_ensure_database_in_container_rejects_unsafe_names(name):
    with pytest.raises(ValueError, match="Unsafe database name"):
        ensure_database_in_container(COMPOSE, name, run=FakeCompose(set()))


def test_ensure_bucket_creates_when_missing():
    s3 = FakeS3(existing=set())

    assert ensure_bucket(s3, "fragancia-media") is True
    assert s3.created == ["fragancia-media"]


def test_ensure_bucket_is_idempotent():
    s3 = FakeS3(existing={"fragancia-media"})

    assert ensure_bucket(s3, "fragancia-media") is False
    assert s3.created == []


def test_ensure_bucket_propagates_unexpected_errors():
    class DeniedS3(FakeS3):
        def head_bucket(self, Bucket):  # noqa: N803
            raise ClientError({"Error": {"Code": "403"}}, "HeadBucket")

    try:
        ensure_bucket(DeniedS3(existing=set()), "fragancia-media")
    except ClientError as error:
        assert error.response["Error"]["Code"] == "403"
    else:
        raise AssertionError("a 403 must not be treated as 'bucket missing'")
