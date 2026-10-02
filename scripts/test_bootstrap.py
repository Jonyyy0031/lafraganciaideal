from pathlib import Path

from botocore.exceptions import ClientError

from infra import ensure_bucket, ensure_database, ensure_env_file, missing_tools, read_env


class FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConnection:
    """Minimal stand-in for a psycopg connection: knows which databases exist."""

    def __init__(self, existing: set[str]):
        self.existing = existing
        self.created: list[str] = []

    def execute(self, query, params=None):
        if params is not None:  # existence check
            return FakeCursor((1,) if params[0] in self.existing else None)
        self.created.append(query.as_string(None))
        return FakeCursor(None)


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


def test_read_env_ignores_comments_and_blank_lines(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("# comment\n\nA=1\nB = two words \nC=\n")

    assert read_env(env) == {"A": "1", "B": "two words", "C": ""}


def test_missing_tools_reports_only_absent_ones():
    available = {"docker", "uv"}

    assert missing_tools(["docker", "uv", "just"], which=lambda t: t in available or None) == [
        "just"
    ]


def test_ensure_database_creates_when_missing():
    conn = FakeConnection(existing={"fragancia"})

    assert ensure_database(conn, "fragancia_test", owner="fragancia") is True
    assert conn.created == ['CREATE DATABASE "fragancia_test" OWNER "fragancia"']


def test_ensure_database_is_idempotent():
    conn = FakeConnection(existing={"fragancia", "fragancia_test"})

    assert ensure_database(conn, "fragancia_test", owner="fragancia") is False
    assert conn.created == []


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
