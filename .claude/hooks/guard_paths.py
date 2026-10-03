"""Classify paths without reading them: a guard never opens the secret it protects.

Shared by guard_bash.py, guard_files.py and guard_read.py. Standard library only.
"""

from __future__ import annotations

import os
import re

_GENERATED_SKILLS = "plan|implement|write-tests|review|verify|fix"
_GENERATED_ADAPTER = re.compile(
    rf"^\.(claude|codex)/agents/|^\.(claude|agents)/skills/({_GENERATED_SKILLS})/SKILL\.md$"
)
_MIGRATION = re.compile(r"^apps/api/migrations/versions/[^/]+\.py$")


def _resolve_existing_parent(abs_path: str) -> str:
    """Resolve symlinks through the deepest existing ancestor; keep the missing tail literal."""
    try:
        os.lstat(abs_path)
    except FileNotFoundError:
        parent = os.path.dirname(abs_path)
        if parent == abs_path:
            raise
        return os.path.join(_resolve_existing_parent(parent), os.path.basename(abs_path))
    return os.path.realpath(abs_path, strict=True)


def _is_secret(candidate: str) -> bool:
    segments = candidate.split(os.sep)
    if any(
        (part == ".env" or part.startswith(".env.")) and part != ".env.example" for part in segments
    ):
        return True
    return candidate.lower().endswith((".pem", ".key"))


def inspect_path(
    path: object, project_dir: str, cwd: str | None = None, write: bool = False
) -> str | None:
    """Return the reason a path is protected, or None when it may be used."""
    if not isinstance(path, str) or not path or "\0" in path:
        return "Missing or invalid path. Pass a literal file path"
    project = os.path.abspath(project_dir)
    joined = os.path.join(cwd or project, path)
    abs_path = os.path.abspath(joined)
    try:
        resolved = _resolve_existing_parent(abs_path)
        # The kernel resolves symlinks before `..` (`link/../x` is next to the link target),
        # and the cwd may be a logical path through a symlink (bash's `cd`).
        physical = os.path.realpath(joined)
    except (OSError, RuntimeError, ValueError):
        return "The path could not be resolved safely. Use a plain path without symlink loops"
    try:
        real_root = os.path.realpath(project, strict=True)
    except (OSError, ValueError):
        return "The project directory could not be resolved. Check CLAUDE_PROJECT_DIR"

    for candidate, root in ((abs_path, project), (resolved, real_root), (physical, real_root)):
        if _is_secret(candidate):
            return "Secret content is protected. Read .env.example for the variable names instead"
        if not write:
            continue
        if ".git" in candidate.split(os.sep):
            return "Git internals are protected. Use git commands instead"
        rel = os.path.relpath(candidate, root).replace(os.sep, "/")
        if rel == "uv.lock":
            return "uv.lock is maintained by uv. Use uv add, uv remove or uv lock"
        if rel == "apps/api/openapi.json":
            return "apps/api/openapi.json is generated. Run uv run just openapi"
        if _GENERATED_ADAPTER.search(rel):
            return (
                "Generated adapter. Edit docs/harness/roles or scripts/harness/adapters.py "
                "and run uv run just harness-sync"
            )
        if _MIGRATION.match(rel) and os.path.lexists(candidate):
            return 'Existing migration. Create a new one with uv run just db-revision "msg"'
    return None
