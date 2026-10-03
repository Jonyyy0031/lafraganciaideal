"""Does the diff stay inside the plan? `uv run just plans-scope <plan> [--base main]`.

Changed files = commits since `merge-base <base> HEAD` + staged + unstaged + untracked (both
paths of renames). A change is allowed when the plan declares it on a `Files:` line (exactly,
or under a declared `dir/`), or it is the plan itself, its initiative README, a finding, a hot
file (append-only: review its content), or a generated companion of a declared file.
Exit 0 when everything is in scope, 1 when something is out of scope, 2 on usage/git errors.
Only paths are checked: whether hot-file edits are append-only is a manual reviewer check.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from plans.lib import FINDINGS_DIR, PLANS_DIR, ROOT, declared_files, parse_document, rel

HOT_FILES = (
    "apps/api/src/fragancia_api/container.py",
    "docs/modules.json",
    "apps/api/.importlinter",
)
MIGRATIONS = "apps/api/migrations/versions/"
OPENAPI = "apps/api/openapi.json"


class GitError(RuntimeError):
    pass


def git(args: list[str], root: Path) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def _name_status(output: str) -> set[str]:
    """Paths from `git diff --name-status -z` (renames/copies contribute both paths)."""
    paths: set[str] = set()
    fields = [f for f in output.split("\0") if f]
    i = 0
    while i < len(fields):
        status = fields[i]
        if status[:1] in ("R", "C"):
            paths.update(fields[i + 1 : i + 3])
            i += 3
        else:
            paths.add(fields[i + 1])
            i += 2
    return paths


def changed_files(base: str, root: Path) -> set[str]:
    if not base or base.startswith("-"):
        raise GitError(f"invalid base {base!r}")
    git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], root)
    git(["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], root)
    merge_base = git(["merge-base", base, "HEAD"], root).strip()
    diff = ["diff", "--name-status", "-z", "--find-renames"]
    paths = _name_status(git([*diff, merge_base, "HEAD"], root))
    paths |= _name_status(git([*diff, "--cached", "HEAD"], root))
    paths |= _name_status(git(diff, root))
    paths |= {
        p for p in git(["ls-files", "--others", "--exclude-standard", "-z"], root).split("\0") if p
    }
    return paths


def is_allowed(path: str, declared: list[str], plan_path: str) -> bool:
    if path in declared or any(d.endswith("/") and path.startswith(d) for d in declared):
        return True
    initiative = plan_path.rsplit("/", 1)[0]
    if path in (plan_path, f"{initiative}/README.md"):
        return True
    if path.startswith(f"{PLANS_DIR}/{FINDINGS_DIR}/") or path in HOT_FILES:
        return True
    if path.startswith(MIGRATIONS) and any(
        d.endswith("infrastructure/tables.py") for d in declared
    ):
        return True
    if path == OPENAPI and any(d.endswith(("contracts.py", "http/router.py")) for d in declared):
        return True
    return path == "uv.lock" and any(d.endswith("pyproject.toml") for d in declared)


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description="Check the diff against a plan's Files: lines")
    parser.add_argument("plan", help="path to the plan, e.g. plans/catalog-perfumes/001-x.md")
    parser.add_argument("--base", default="main", help="base branch or commit (default: main)")
    args = parser.parse_args(argv)

    plan_file = (root / args.plan).resolve()
    if not plan_file.is_file():
        print(f"✘ plan not found: {args.plan}", file=sys.stderr)
        return 2
    plan_path = rel(plan_file, root)
    declared = declared_files(parse_document(plan_file))
    try:
        changed = changed_files(args.base, root)
    except GitError as error:
        print(f"✘ {error}", file=sys.stderr)
        return 2

    q = json.dumps
    print(
        f"Plan {q(plan_path)}: {len(declared)} declared, {len(changed)} changed (base {args.base})"
    )
    hot = sorted(p for p in changed if p in HOT_FILES)
    if hot:
        print("Hot files (review that edits are append-only): " + ", ".join(map(q, hot)))
    unchanged = sorted(d for d in declared if not d.endswith("/") and d not in changed)
    if unchanged:
        print("Declared but unchanged: " + ", ".join(map(q, unchanged)))
    outside = sorted(p for p in changed if not is_allowed(p, declared, plan_path))
    if outside:
        print("✘ Out of scope:", file=sys.stderr)
        for path in outside:
            print(f"  {q(path)}", file=sys.stderr)
        return 1
    print("✔ Every change is inside the plan")
    return 0


if __name__ == "__main__":
    sys.exit(main())
