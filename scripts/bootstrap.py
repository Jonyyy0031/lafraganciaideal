"""One-command local setup: `uv run just bootstrap`.

Idempotent: safe to run any number of times. It never overwrites an existing .env and never
deletes data, databases, buckets or volumes.
"""

import os
import subprocess
import sys
from pathlib import Path

import boto3

from infra import (
    ensure_bucket,
    ensure_database_in_container,
    ensure_env_file,
    ensure_env_keys,
    missing_tools,
    read_env,
)

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "apps/api"
COMPOSE = ["docker", "compose", "-f", str(ROOT / "infra/docker/compose.yaml")]
TEST_DATABASE = "fragancia_test"
# Compose defaults (infra/docker/compose.yaml); exported variables override them.
PORTS = {"POSTGRES_PORT": "5433", "VALKEY_PORT": "6380", "S3_CONSOLE_PORT": "9101",
         "SMTP_PORT": "1026", "MAILPIT_UI_PORT": "8026"}  # fmt: skip


def step(message: str) -> None:
    print(f"\n▸ {message}", flush=True)


def report(created: bool, what: str) -> None:
    print(f"  + {what} created" if created else f"  = {what} already exists")


def run(command: list[str], cwd: Path = ROOT) -> None:
    subprocess.run(command, cwd=cwd, check=True)  # noqa: S603 (fixed, trusted commands)


def fail(message: str) -> None:
    print(f"\n✘ {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    step("Checking tools")
    missing = missing_tools(["docker", "uv", "git"])
    if missing:
        fail(
            f"Missing tools: {', '.join(missing)}. Install them and run `uv run just bootstrap` "
            "(just itself comes from the uv environment)."
        )
    daemon = subprocess.run(["docker", "info"], capture_output=True)  # noqa: S603, S607
    if daemon.returncode != 0:
        fail("Docker is installed but the daemon is not running.")
    print("  docker, uv and git OK")

    step("Environment file (created only if missing)")
    api_env = API_DIR / ".env"
    report(ensure_env_file(API_DIR / ".env.example", api_env), "apps/api/.env")
    added = ensure_env_keys(API_DIR / ".env.example", api_env)
    if added:
        print(f"  + apps/api/.env gained new settings: {', '.join(added)}")
    if (ROOT / ".env").exists():
        print(
            "  ! the root .env is no longer used (compose has inline defaults); you can delete it"
        )
    env = read_env(api_env)
    ports = {name: os.environ.get(name, default) for name, default in PORTS.items()}

    step("Infrastructure (PostgreSQL, Valkey, S3 storage, Mailpit)")
    run([*COMPOSE, "up", "-d", "--wait", "--wait-timeout", "180"])

    step("Integration test database")
    report(ensure_database_in_container(COMPOSE, TEST_DATABASE), TEST_DATABASE)

    step("Database migrations (development and test)")
    run(["uv", "run", "alembic", "upgrade", "head"], cwd=API_DIR)
    run(["uv", "run", "alembic", "-x", "test=true", "upgrade", "head"], cwd=API_DIR)

    step("S3 bucket for product media")
    s3 = boto3.client(
        "s3",
        endpoint_url=env["S3_ENDPOINT_URL"],
        aws_access_key_id=env["S3_ACCESS_KEY"],
        aws_secret_access_key=env["S3_SECRET_KEY"],
        region_name="us-east-1",
    )
    report(ensure_bucket(s3, env["S3_BUCKET"]), f"bucket {env['S3_BUCKET']}")

    if (ROOT / ".git").exists():
        step("Git hooks (pre-commit + commit message)")
        run(["uv", "run", "pre-commit", "install", "--hook-type", "pre-commit",
             "--hook-type", "commit-msg"])  # fmt: skip

    print(
        f"""
✔ Ready.
  PostgreSQL  → 127.0.0.1:{ports["POSTGRES_PORT"]}  (db fragancia, tests {TEST_DATABASE})
  Valkey      → 127.0.0.1:{ports["VALKEY_PORT"]}
  S3 API      → {env["S3_ENDPOINT_URL"]}  (bucket {env["S3_BUCKET"]})
  S3 console  → http://localhost:{ports["S3_CONSOLE_PORT"]}
  Mailpit     → http://localhost:{ports["MAILPIT_UI_PORT"]}  (SMTP 127.0.0.1:{ports["SMTP_PORT"]})

  API         → uv run just api     (http://127.0.0.1:8100/api/v1/docs)
  Worker      → uv run just worker
"""  # noqa: E501
    )


if __name__ == "__main__":
    main()
