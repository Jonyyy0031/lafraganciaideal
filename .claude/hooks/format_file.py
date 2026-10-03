#!/usr/bin/env python3
"""PostToolUse(Edit|Write|MultiEdit): format the edited Python file with ruff.

Never blocks (always exits 0): formatting must not interrupt the work. Uses the project's
`.venv/bin/ruff` and does nothing when it is missing.
"""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 - fixed argv, no shell
import sys


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
        file_path = payload["tool_input"]["file_path"]
        if not isinstance(file_path, str) or not file_path.endswith(".py"):
            return
        project_dir = os.path.abspath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
        abs_path = os.path.abspath(os.path.join(project_dir, file_path))
        # Only repository files: never reformat memory, scratchpad or other projects.
        if os.path.commonpath([project_dir, abs_path]) != project_dir or not os.path.isfile(
            abs_path
        ):
            return
        ruff = os.path.join(project_dir, ".venv", "bin", "ruff")
        if not os.access(ruff, os.X_OK):
            return
        # Fix first, then format: fixes such as removing an unused import can leave blank lines.
        for argv in (
            [ruff, "check", "--fix", "--force-exclude", "--quiet", abs_path],
            [ruff, "format", "--force-exclude", "--quiet", abs_path],
        ):
            subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv,
                cwd=project_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
    except Exception:  # noqa: BLE001, S110 - silent on purpose
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
