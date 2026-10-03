#!/usr/bin/env python3
"""PreToolUse(Edit|Write|MultiEdit|NotebookEdit): check every path as a write.

Uncertain input never authorizes a write: exit 2 with `Edit blocked: ...` on stderr.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from guard_paths import inspect_path  # noqa: E402


class Blocked(Exception):  # noqa: N818 - reads as `raise Blocked(reason)`
    """The edit is not allowed."""


def collect_paths(args: object) -> list[object]:
    if not isinstance(args, dict):
        raise Blocked("invalid edit payload")
    parent = args.get("file_path", args.get("notebook_path"))
    files: list[object] = []
    if parent is not None:
        files.append(parent)
    if "edits" in args:
        edits = args["edits"]
        if not isinstance(edits, list) or not edits:
            raise Blocked("empty or invalid MultiEdit")
        for edit in edits:
            if not isinstance(edit, dict):
                raise Blocked("invalid MultiEdit member")
            files.append(edit.get("file_path", edit.get("notebook_path", parent)))
    if not files:
        raise Blocked("edit without recognized paths")
    return files


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise Blocked("invalid hook payload")
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        cwd = payload.get("cwd", project_dir)
        if not isinstance(cwd, str) or not cwd:
            raise Blocked("invalid working directory in the payload")
        for path in collect_paths(payload.get("tool_input")):
            reason = inspect_path(path, project_dir, cwd, write=True)
            if reason:
                raise Blocked(reason)
    except Blocked as error:
        print(f"Edit blocked: {error}.", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - fail closed on anything unexpected
        print(f"Edit blocked: the guard could not inspect it ({error}).", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
