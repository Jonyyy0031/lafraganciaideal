"""Idempotent building blocks for the local environment bootstrap.

Every `ensure_*` function returns True when it created something and False when it was
already there. None of them deletes or overwrites anything.
"""

import shutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError
from psycopg import sql


def ensure_env_file(example: Path, target: Path) -> bool:
    """Copy `example` to `target` only if `target` does not exist yet."""
    if target.exists():
        return False
    shutil.copyfile(example, target)
    return True


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


def ensure_database(conn: Any, name: str, owner: str) -> bool:
    """Create database `name` owned by `owner` if missing. `conn` must be in autocommit."""
    exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    if exists:
        return False
    conn.execute(
        sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(owner))
    )
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
