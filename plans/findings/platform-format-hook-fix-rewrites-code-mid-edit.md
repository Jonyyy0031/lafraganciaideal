---
status: resolved
plan: catalog-perfumes/003
module: platform
found: 2026-10-09
---

# The format hook's `ruff check --fix` silently rewrites code between two edits

## Found while

Implementing catalog-perfumes/003 (step 1, `contracts.py`).

## What

`.claude/hooks/format_file.py:34` runs `ruff check --fix` (then `ruff format`) after every
Edit/Write of a `.py` file. When a change is split into several edits, the file is fixed in an
intermediate state:

- An edit that adds imports before the edit that uses them loses those imports (F401 autofix).
- With `Literal` no longer imported, the next edit's `Literal["women", "men", "unisex"]` had its
  quotes stripped (`Literal[women, men, unisex]`): ruff took the strings for quoted annotations
  and unquoted them. The result is still valid Python syntax and only mypy caught it
  (`Name "women" is not defined`).

## Why it matters

The fix changes meaning, not only style, and it happens without the agent seeing it. A
rewrite like this in a value position that mypy does not check could ship. Agents waste turns
re-adding imports.

## Suggested next step

Either restrict the hook's `--fix` to safe formatting rules (for example `--select I` for import
sorting) and leave other fixes to `uv run just check`, or document in the hook's message that
imports must be added in the same edit as their first use. The user decides.

## Resolution (2026-10-12, fast lane)

The user asked for the open findings to be fixed. This one was fixed in the fast lane; there
was no plan. The `plan:` field points to the plan during which it was found, because
`plans-lint` requires one for a resolved finding.

- **Reproduced:** `ruff check --fix` with every rule, on a file whose imports are not used yet,
  removed `import sys` and `import os`. With `--select I` they were kept and sorted.
- **Fix:** `.claude/hooks/format_file.py` runs `ruff check --fix --select I`, so it only sorts
  imports, then `ruff format`. `uv run just check` still reports every other rule.
- **Regression test:**
  `scripts/harness/test_hooks.py::test_format_file_keeps_imports_not_used_yet_and_only_sorts_them`.
- **Verified live:** a real Write of a file holding `import sys`, `import os` and
  `from typing import Literal`, all unused, kept all three. The hook sorted them and formatted
  the code. `uv run just check` is green (1826 passed, 685 harness).
