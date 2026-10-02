"""One-command local setup: `uv run just bootstrap`.

Idempotent: safe to run any number of times. It never overwrites an existing .env and never
deletes data, databases, buckets or volumes.
"""

import subprocess
import sys
from pathlib import Path

import boto3
import psycopg

from infra import ensure_bucket, ensure_database, ensure_env_file, missing_tools, read_env

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ["docker", "compose", "-f", str(ROOT / "infra/docker/compose.yaml")]


def step(message: str) -> None:
    print(f"\n▸ {message}", flush=True)


def report(created: bool, what: str) -> None:
    print(f"  + {what} created" if created else f"  = {what} already exists")


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)  # noqa: S603 (fixed, trusted commands)


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
    env_path = ROOT / ".env"
    report(ensure_env_file(ROOT / ".env.example", env_path), ".env")
    env = read_env(env_path)

    step("Infrastructure (PostgreSQL, Valkey, S3 storage, Mailpit)")
    run([*COMPOSE, "--env-file", str(env_path), "up", "-d", "--wait", "--wait-timeout", "180"])

    step("Integration test database")
    with psycopg.connect(
        host="127.0.0.1",
        port=int(env["POSTGRES_PORT"]),
        user=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
        dbname=env["POSTGRES_DB"],
        autocommit=True,
    ) as conn:
        report(
            ensure_database(conn, env["POSTGRES_TEST_DB"], owner=env["POSTGRES_USER"]),
            env["POSTGRES_TEST_DB"],
        )

    step("S3 bucket for product media")
    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{env['S3_PORT']}",
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
  PostgreSQL  → 127.0.0.1:{env["POSTGRES_PORT"]}  (db {env["POSTGRES_DB"]}, tests {env["POSTGRES_TEST_DB"]})
  Valkey      → 127.0.0.1:{env["VALKEY_PORT"]}
  S3 API      → http://127.0.0.1:{env["S3_PORT"]}  (bucket {env["S3_BUCKET"]})
  S3 console  → http://localhost:{env["S3_CONSOLE_PORT"]}
  Mailpit     → http://localhost:{env["MAILPIT_UI_PORT"]}  (SMTP 127.0.0.1:{env["SMTP_PORT"]})
"""  # noqa: E501
    )


if __name__ == "__main__":
    main()
