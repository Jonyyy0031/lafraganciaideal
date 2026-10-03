"""Commit message convention (Conventional Commits, English).

    type(scope): what changed, in business terms

Used by the `commit-msg` hook (`--file`) and by CI over a PR's commits (`--range`).
Rules:
- type from the conventional list; scope required and registered (docs/modules.json or the
  cross-cutting list below); header at most 100 characters;
- the subject must say what changed, not "changes", "wip", "fix"...;
- no AI attribution trailers (Co-Authored-By, "Generated with ...").
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TYPES = {
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
}
CROSS_CUTTING_SCOPES = {"api", "web", "infra", "ci", "deps", "docs", "repo", "harness"}
MAX_HEADER = 100

HEADER = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^()]*)\))?(?P<breaking>!)?: (?P<subject>.+)$"
)
GENERIC_SUBJECT = re.compile(
    r"^(changes|change|wip|fix|fixes|update|updates|misc|stuff|test|tests|"
    r"cambios|arreglos|varios|prueba)\.?$",
    re.IGNORECASE,
)
AI_ATTRIBUTION = re.compile(
    r"^(co-authored-by:|.*generated with .*(claude|codex|copilot|gpt))",
    re.IGNORECASE | re.MULTILINE,
)
# Messages written by git itself (merges, reverts) are not ours to police.
GIT_GENERATED = re.compile(r'^(Merge |Revert ")')


def load_scopes(registry: Path = ROOT / "docs/modules.json") -> set[str]:
    modules = json.loads(registry.read_text())["modules"]
    return {module["name"] for module in modules} | CROSS_CUTTING_SCOPES


def strip_git_comments(message: str) -> str:
    return "\n".join(line for line in message.splitlines() if not line.startswith("#")).strip()


def validate(message: str, scopes: set[str]) -> list[str]:
    message = strip_git_comments(message)
    header = message.splitlines()[0] if message else ""
    errors: list[str] = []

    if AI_ATTRIBUTION.search(message):
        errors.append(
            "no AI attribution: remove Co-Authored-By trailers and 'Generated with' lines"
        )
    if GIT_GENERATED.match(header):
        return errors

    match = HEADER.match(header)
    if not match:
        errors.append(
            f"header must follow Conventional Commits 'type(scope): subject', got {header!r}"
        )
        return errors

    if len(header) > MAX_HEADER:
        errors.append(f"header is longer than {MAX_HEADER} characters ({len(header)})")
    if match["type"] not in TYPES:
        errors.append(f"unknown type '{match['type']}' (allowed: {', '.join(sorted(TYPES))})")
    if not match["scope"]:
        errors.append("scope is required: type(scope): subject")
    elif match["scope"] not in scopes:
        errors.append(
            f"unknown scope '{match['scope']}' (register modules in docs/modules.json; "
            f"allowed: {', '.join(sorted(scopes))})"
        )
    if GENERIC_SUBJECT.match(match["subject"].strip()):
        errors.append("subject must say WHAT changed in business terms (not 'changes', 'wip'...)")
    return errors


def messages_in_range(revision_range: str) -> list[tuple[str, str]]:
    output = subprocess.run(  # noqa: S603
        ["git", "log", "--format=%H%x00%B%x1e", revision_range],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    entries = [entry.strip("\n") for entry in output.split("\x1e") if entry.strip()]
    return [tuple(entry.split("\x00", 1)) for entry in entries]  # type: ignore[misc]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="commit message file (commit-msg hook)")
    source.add_argument("--range", dest="revision_range", help="git revision range, e.g. a..b")
    args = parser.parse_args(argv)

    scopes = load_scopes()
    if args.file:
        targets = [("commit message", args.file.read_text())]
    else:
        targets = [(sha[:10], body) for sha, body in messages_in_range(args.revision_range)]

    failed = False
    for label, message in targets:
        errors = validate(message, scopes)
        if errors:
            failed = True
            header = next(iter(strip_git_comments(message).splitlines()), "")
            print(f"✘ {label}: {header!r}", file=sys.stderr)
            for error in errors:
                print(f"    - {error}", file=sys.stderr)
    if failed:
        print("\nConvention: CONTRIBUTING.md → 'Commits'", file=sys.stderr)
        return 1
    if args.revision_range:
        print(f"✔ {len(targets)} commit message(s) follow the convention")
    return 0


if __name__ == "__main__":
    sys.exit(main())
