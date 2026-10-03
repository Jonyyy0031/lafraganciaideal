#!/usr/bin/env python3
"""PreToolUse(Read): never read secrets nor follow links into them.

Exit 2 with `Read blocked: ...` on stderr blocks the read; uncertain input fails closed.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from guard_paths import inspect_path  # noqa: E402


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict) or not isinstance(payload.get("tool_input"), dict):
            reason: str | None = "invalid hook payload"
        else:
            project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
            cwd = payload.get("cwd", project_dir)
            if not isinstance(cwd, str) or not cwd:
                reason = "invalid working directory in the payload"
            else:
                reason = inspect_path(payload["tool_input"].get("file_path"), project_dir, cwd)
    except Exception as error:  # noqa: BLE001 - fail closed on anything unexpected
        reason = f"the guard could not inspect it ({error})"
    if reason:
        print(f"Read blocked: {reason}.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
