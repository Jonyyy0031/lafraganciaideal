"""Idempotent building blocks for the local environment bootstrap.

Every `ensure_*` function returns True when it created something and False when it was
already there. None of them deletes or overwrites anything.
"""

import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError


def ensure_env_file(example: Path, target: Path) -> bool:
    """Copy `example` to `target` only if `target` does not exist yet."""
    if target.exists():
        return False
    shutil.copyfile(example, target)
    return True


def ensure_env_keys(example: Path, target: Path) -> list[str]:
    """Append to `target` the keys of `example` it lacks (with the example's values).

    Existing keys and values are never changed. Returns the keys that were added.
    """
    present = read_env(target)
    missing = [
        line for line in example.read_text().splitlines() if _key(line) not in (None, *present)
    ]
    if missing:
        block = "\n".join(["", "# Added by `just bootstrap` from .env.example", *missing, ""])
        with target.open("a") as handle:
            handle.write(block)
    return [key for line in missing if (key := _key(line))]


def _key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def read_env(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file (comments and blank lines are ignored)."""
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def missing_tools(tools: Iterable[str], which: Callable[[str], Any] = shutil.which) -> list[str]:
    """Return the tools that are not on PATH."""
    return [tool for tool in tools if not which(tool)]


_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
# Runs psql inside the container with the container's own credentials (as web-rh does), so the
# host needs no database settings.
_PSQL = 'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"'


def ensure_database_in_container(
    compose: list[str], name: str, run: Callable[..., Any] = subprocess.run
) -> bool:
    """Create database `name` in the compose `postgres` service if missing."""
    if not _IDENTIFIER.match(name):
        raise ValueError(f"Unsafe database name: {name!r}")
    base = [*compose, "exec", "-T", "postgres", "sh", "-eu", "-c", _PSQL, "bootstrap"]
    options = {"capture_output": True, "text": True, "check": True}
    query = f"SELECT 1 FROM pg_database WHERE datname = '{name}'"  # noqa: S608 (validated name)
    if run([*base, "-tAc", query], **options).stdout.strip() == "1":
        return False
    run([*base, "-c", f'CREATE DATABASE "{name}"'], **options)
    return True


def ensure_bucket(s3: Any, name: str) -> bool:
    """Create S3 bucket `name` if missing. Errors other than "not found" are propagated."""
    try:
        s3.head_bucket(Bucket=name)
        return False
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket"):
            raise
    s3.create_bucket(Bucket=name)
    return True
