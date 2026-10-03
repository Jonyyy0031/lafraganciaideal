import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

import bootstrap
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


def test_ensure_bucket_treats_no_such_bucket_as_missing():
    class NoSuchS3(FakeS3):
        def head_bucket(self, Bucket):  # noqa: N803
            raise ClientError({"Error": {"Code": "NoSuchBucket"}}, "HeadBucket")

    s3 = NoSuchS3(existing=set())

    assert ensure_bucket(s3, "fragancia-media") is True
    assert s3.created == ["fragancia-media"]


def test_ensure_database_in_container_propagates_command_failures():
    def failing(command, **_):
        raise subprocess.CalledProcessError(1, command)

    with pytest.raises(subprocess.CalledProcessError):
        ensure_database_in_container(COMPOSE, "fragancia_test", run=failing)


def test_ensure_database_in_container_runs_psql_with_the_containers_own_credentials():
    compose = FakeCompose(existing=set())

    ensure_database_in_container(COMPOSE, "fragancia_test", run=compose)

    check = compose.commands[0]
    assert check[: len(COMPOSE) + 3] == [*COMPOSE, "exec", "-T", "postgres"]
    script = check[check.index("-c") + 1]
    assert "$POSTGRES_USER" in script and "$POSTGRES_DB" in script


def test_ensure_env_keys_keeps_existing_values_when_the_file_lacks_a_trailing_newline(
    tmp_path: Path,
):
    example = tmp_path / ".env.example"
    example.write_text("A=1\nB=2\n")
    target = tmp_path / ".env"
    target.write_text("A=custom")

    assert ensure_env_keys(example, target) == ["B"]
    assert read_env(target) == {"A": "custom", "B": "2"}


def test_ensure_env_keys_skips_commented_example_keys(tmp_path: Path):
    example = tmp_path / ".env.example"
    example.write_text("# OPTIONAL=1\nA=1\n")
    target = tmp_path / ".env"
    target.write_text("")

    assert ensure_env_keys(example, target) == ["A"]
    assert "OPTIONAL" not in read_env(target)


def test_the_api_env_example_carries_the_s3_settings():
    values = read_env(bootstrap.API_DIR / ".env.example")

    assert {"S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET"} <= values.keys()
    assert values["S3_ENDPOINT_URL"] == "http://127.0.0.1:9100"
    assert values["S3_BUCKET"] == "fragancia-media"


class BootstrapWorld:
    """Runs bootstrap.main() in a throwaway layout with every external effect faked."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.root = tmp_path
        self.api = tmp_path / "apps/api"
        self.api.mkdir(parents=True)
        (self.api / ".env.example").write_text(
            "S3_ENDPOINT_URL=http://127.0.0.1:9100\nS3_ACCESS_KEY=k\nS3_SECRET_KEY=s\n"
            "S3_BUCKET=fragancia-media\n"
        )
        self.databases: set[str] = set()
        self.buckets: set[str] = set()
        self.commands: list[list[str]] = []
        monkeypatch.setattr(bootstrap, "ROOT", self.root)
        monkeypatch.setattr(bootstrap, "API_DIR", self.api)
        monkeypatch.setattr(bootstrap, "missing_tools", lambda tools: [])
        monkeypatch.setattr(
            bootstrap, "run", lambda command, cwd=None: self.commands.append(command)
        )
        monkeypatch.setattr(
            bootstrap.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0)
        )
        monkeypatch.setattr(bootstrap.boto3, "client", lambda *a, **k: FakeS3(self.buckets))
        monkeypatch.setattr(bootstrap, "ensure_database_in_container", self.ensure_database)
        for name in bootstrap.PORTS:
            monkeypatch.delenv(name, raising=False)

    def ensure_database(self, compose, name):
        created = name not in self.databases
        self.databases.add(name)
        return created


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BootstrapWorld:
    return BootstrapWorld(tmp_path, monkeypatch)


def test_bootstrap_works_without_a_root_env_and_is_idempotent(
    world: BootstrapWorld, capsys: pytest.CaptureFixture[str]
):
    bootstrap.main()
    first = capsys.readouterr().out
    bootstrap.main()
    second = capsys.readouterr().out

    assert "+ apps/api/.env created" in first and "+ fragancia_test created" in first
    assert "= apps/api/.env already exists" in second
    assert "= fragancia_test already exists" in second
    assert "= bucket fragancia-media already exists" in second
    assert not (world.root / ".env").exists()


def test_bootstrap_mentions_but_never_deletes_a_leftover_root_env(
    world: BootstrapWorld, capsys: pytest.CaptureFixture[str]
):
    (world.root / ".env").write_text("POSTGRES_PORT=1\n")

    bootstrap.main()

    assert "root .env is no longer used" in capsys.readouterr().out
    assert (world.root / ".env").read_text() == "POSTGRES_PORT=1\n"


def test_bootstrap_adds_missing_s3_keys_to_an_existing_api_env(
    world: BootstrapWorld, capsys: pytest.CaptureFixture[str]
):
    (world.api / ".env").write_text("DATABASE_URL=custom\n")

    bootstrap.main()

    assert "gained new settings: S3_ENDPOINT_URL" in capsys.readouterr().out
    assert read_env(world.api / ".env")["DATABASE_URL"] == "custom"


def test_bootstrap_prints_exported_ports_over_the_compose_defaults(
    world: BootstrapWorld, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    bootstrap.main()
    assert "127.0.0.1:5433" in capsys.readouterr().out

    monkeypatch.setenv("POSTGRES_PORT", "5544")
    bootstrap.main()
    assert "127.0.0.1:5544" in capsys.readouterr().out


def test_bootstrap_stops_when_a_tool_is_missing(
    world: BootstrapWorld, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(bootstrap, "missing_tools", lambda tools: ["docker"])

    with pytest.raises(SystemExit) as exit_info:
        bootstrap.main()

    assert exit_info.value.code == 1
    assert "Missing tools: docker" in capsys.readouterr().err
    assert world.commands == []


def test_bootstrap_stops_when_the_docker_daemon_is_down(
    world: BootstrapWorld, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))

    with pytest.raises(SystemExit) as exit_info:
        bootstrap.main()

    assert exit_info.value.code == 1
    assert "daemon is not running" in capsys.readouterr().err


def test_bootstrap_migrates_the_development_and_the_test_database(world: BootstrapWorld):
    bootstrap.main()

    migrations = [c for c in world.commands if "alembic" in c]
    assert migrations == [
        ["uv", "run", "alembic", "upgrade", "head"],
        ["uv", "run", "alembic", "-x", "test=true", "upgrade", "head"],
    ]


@pytest.mark.skip(
    reason="NOT CONFIRMED: a fresh volume getting fragancia_test from "
    "infra/docker/postgres/01-create-test-db.sql needs a real docker volume; covered by the CI "
    "`infra` job and the verifier, not by unit tooling tests"
)
def test_a_fresh_postgres_volume_creates_the_test_database(): ...


@pytest.mark.skip(
    reason="NOT CONFIRMED: `POSTGRES_PORT=5544 just up` publishing on 5544 needs a running docker "
    "daemon and touches the development stack; verifier-only"
)
def test_exporting_postgres_port_publishes_postgres_on_that_port(): ...


@pytest.mark.skip(
    reason="NOT CONFIRMED: `just psql` and the aborted `just db-reset --test` run inside the "
    "running postgres container (development stack); verifier-only"
)
def test_psql_and_db_reset_use_the_containers_own_credentials(): ...
