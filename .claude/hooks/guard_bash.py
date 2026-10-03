#!/usr/bin/env python3
"""PreToolUse(Bash): conservative guard over known shell syntax. Not a sandbox.

Reads the hook payload on stdin; exit 2 with `Command blocked: ...` on stderr blocks the
command, exit 0 allows it. Anything it cannot inspect is blocked (fail closed).
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from guard_paths import inspect_path  # noqa: E402

SHELLS = {"sh", "bash", "zsh", "fish", "dash"}
DISPOSABLE = {
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    "dist",
    "build",
    ".angular",
    "coverage",
    "htmlcov",
}
WRAPPERS = {"env", "eval", "exec", "command", "xargs", "nohup", "nice", "timeout", "time", "stdbuf"}
INTERPRETER = re.compile(r"^(node|python[\d.]*|perl|ruby|php)$")
INLINE_FLAG = re.compile(r"^(-[ecp]|--eval(=|$)|--print(=|$)|--execute(=|$)|--run(=|$))")
SHELL_C = re.compile(r"^-[a-z]*c[a-z]*$")
PUSH_TO_MAIN = re.compile(r":(refs/heads/)?(main|master)$")
DESTRUCTIVE_SQL = re.compile(r"\b(drop\s+(database|schema|table)|truncate)\b", re.IGNORECASE)
DELETE_FROM = re.compile(r"^\s*delete\s+from\b", re.IGNORECASE)
WHERE = re.compile(r"\bwhere\b", re.IGNORECASE)

UV_RUN_VALUE_FLAGS = {
    "--directory",
    "--project",
    "--package",
    "--with",
    "--with-editable",
    "--with-requirements",
    "--python",
    "-p",
    "--group",
    "--only-group",
    "--no-group",
    "--extra",
    "--env-file",
    "--index",
    "--index-url",
    "--default-index",
    "--extra-index-url",
}
UV_RUN_BOOL_FLAGS = {
    "--frozen",
    "--locked",
    "--no-sync",
    "--isolated",
    "--all-packages",
    "--all-extras",
    "--all-groups",
    "--no-dev",
    "--dev",
    "--active",
    "--no-project",
    "--no-env-file",
    "--offline",
    "--exact",
    "--quiet",
    "-q",
    "--verbose",
    "-v",
}
JUST_VALUE_FLAGS = {
    "-f",
    "--justfile",
    "-d",
    "--working-directory",
    "--dotenv-path",
    "--dotenv-filename",
    "--color",
    "--command-color",
    "--chooser",
    "--list-heading",
    "--list-prefix",
}


class Blocked(Exception):  # noqa: N818 - reads as `raise Blocked(reason)`
    """The command is not allowed; the message is the reason plus the safe alternative."""


# Plain classes instead of dataclasses: importing dataclasses costs more than the whole check.
class Word:
    __slots__ = ("glob", "value")

    def __init__(self, value: str, glob: bool = False) -> None:
        self.value = value
        self.glob = glob


class Op:
    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


class Context:
    __slots__ = ("cwd", "depth", "pipe", "previous_program", "project_dir")

    def __init__(self, project_dir: str, cwd: str, depth: int = 0) -> None:
        self.project_dir = project_dir
        self.cwd = cwd
        self.depth = depth
        self.pipe = False
        self.previous_program: str | None = None


def lex(command: str) -> list[Word | Op]:
    """Keep literal arguments; never expand variables nor run substitutions."""
    tokens: list[Word | Op] = []
    value = ""
    active = False
    quoted = False
    glob = False
    quote: str | None = None

    def flush() -> None:
        nonlocal value, active, quoted, glob
        if active:
            tokens.append(Word(value, glob))
        value, active, quoted, glob = "", False, False, False

    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                value += ch
            i += 1
            continue
        if ch in "$`":
            raise Blocked("dynamic expansion is not supported. Use literal arguments")
        if ch == "\\":
            if i + 1 >= n:
                raise Blocked("incomplete escape. Remove the trailing backslash")
            i += 1
            if command[i] != "\n":
                value += command[i]
                active = True
                quoted = True
            i += 1
            continue
        if quote == '"':
            if ch == '"':
                quote = None
            else:
                value += ch
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            active = True
            quoted = True
            i += 1
            continue
        if ch == "#" and not active:
            while i + 1 < n and command[i + 1] != "\n":
                i += 1
            i += 1
            continue
        if ch in "(){}":
            raise Blocked("dynamic grouping is not supported. Use explicit commands")
        if ch.isspace() and ch != "\n":
            flush()
            i += 1
            continue
        if ch in ";|&<>\n":
            # `2>` / `2>&1`: a bare file-descriptor number before a redirection is not an argument.
            if ch in "<>" and active and not quoted and value.isdigit():
                value, active = "", False
            flush()
            op = ch
            if i + 1 < n and command[i + 1] == ch and ch in "|&<>":
                i += 1
                op += ch
            if op in (">", "<") and i + 1 < n and command[i + 1] == "&":
                # File-descriptor duplication (`>&2`, `2>&1`, `<&-`): writes no file.
                i += 2
                while i < n and (command[i].isdigit() or command[i] == "-"):
                    i += 1
                continue
            if op in ("<<", "&"):
                raise Blocked(
                    "heredoc/background is not supported. "
                    "Use a reviewed file and an explicit command"
                )
            tokens.append(Op(op))
            i += 1
            continue
        active = True
        if ch in "*?[":
            glob = True
        value += ch
        i += 1
    if quote:
        raise Blocked("unclosed quotes. Close every quote")
    flush()
    return tokens


def _vals(args: list[Word]) -> list[str]:
    return [a.value for a in args]


def _check_sql(values: list[str]) -> None:
    for value in values:
        if DESTRUCTIVE_SQL.search(value):
            raise Blocked("destructive SQL. Write a migration or ask the user to run it")
        for statement in value.split(";"):
            if DELETE_FROM.search(statement) and not WHERE.search(statement):
                raise Blocked("DELETE without WHERE. Add a WHERE clause or ask the user")


def _check_rm(args: list[Word], ctx: Context) -> None:
    flags = " ".join(a.value for a in args if a.value.startswith("-"))
    targets = [a for a in args if not a.value.startswith("-")]
    if not targets:
        raise Blocked("rm without verifiable paths. Pass literal paths")
    recursive = re.search(r"--recursive|-[a-zA-Z]*[rR]", flags) is not None
    for target in targets:
        if target.glob or target.value.startswith("~"):
            raise Blocked("rm needs literal paths without wildcards. List each path")
        abs_path = os.path.abspath(os.path.join(ctx.cwd, target.value))
        reason = inspect_path(abs_path, ctx.project_dir, write=True)
        if reason:
            raise Blocked(reason)
        if recursive:
            if os.path.basename(abs_path) not in DISPOSABLE or ".." in target.value.split("/"):
                raise Blocked(
                    "recursive delete outside disposable directories "
                    f"({', '.join(sorted(DISPOSABLE))}). Ask the user"
                )
            if os.path.lexists(abs_path) and os.path.realpath(abs_path) != abs_path:
                raise Blocked(
                    "no recursive delete through symlinks. Remove the link target explicitly"
                )
        elif os.path.lexists(abs_path):
            import subprocess  # noqa: PLC0415 - lazy: only rm of an existing file needs git

            tracked = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", "ls-files", "--error-unmatch", "--", abs_path],  # noqa: S607
                cwd=ctx.project_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if tracked.returncode != 0:
                raise Blocked(
                    "untracked file without reliable provenance. Ask the user to delete it"
                )


def _check_git(args: list[Word]) -> None:
    while args and args[0].value.startswith("-"):
        if args[0].value == "-C" and len(args) > 1:
            args = args[2:]
            continue
        if args[0].value in ("--no-pager", "--literal-pathspecs"):
            args = args[1:]
            continue
        raise Blocked("unsupported git global option. Run git from the right directory")
    if not args:
        return
    sub = args[0].value
    vals = _vals(args[1:])
    if sub == "stash" and (not vals or vals[0] not in ("list", "show")):
        raise Blocked("git stash changes shared work. Commit on a branch instead")
    if sub == "reset" and "--hard" in vals:
        raise Blocked("git reset --hard discards work. Use git reset (mixed) or ask the user")
    if sub == "clean" and any(re.match(r"^-[^-]*f", v) for v in vals):
        raise Blocked("git clean destroys untracked files. Ask the user")
    if sub == "restore" and ("--staged" not in vals or "--worktree" in vals or "-W" in vals):
        raise Blocked("git restore discards work. Use git restore --staged <path>")
    if sub == "checkout" and (
        any(v in ("--", "-f", "--force", ".") for v in vals) or ("-b" not in vals and len(vals) > 1)
    ):
        raise Blocked("git checkout can discard files. Use git switch <branch> or git switch -c")
    if sub == "switch" and "--discard-changes" in vals:
        raise Blocked("git switch --discard-changes discards work. Commit first")
    if sub == "branch" and "-D" in vals:
        raise Blocked("forced branch delete. Use git branch -d or ask the user")
    if sub == "add" and any(v in ("-A", "--all", "-u", "--update", ".", "*", ":/") for v in vals):
        raise Blocked("staging needs explicit paths. Use git add <path>...")
    if sub == "commit" and any(re.match(r"^-[^-]*a", v) or v == "--all" for v in vals):
        raise Blocked("commit needs explicit staging. Use git add <path> then git commit")
    if sub in ("commit", "push", "merge") and "--no-verify" in vals:
        raise Blocked("never skip hooks. Fix what the hook reports")
    if sub == "push" and any(
        v in ("--force", "-f", "main", "master") or v.startswith("+") or PUSH_TO_MAIN.search(v)
        for v in vals
    ):
        raise Blocked(
            "force push or direct push to the main branch. "
            "Use --force-with-lease on a feature branch"
        )


def _check_uv_run(args: list[Word], ctx: Context) -> None:
    i = 0
    cwd = ctx.cwd
    while i < len(args) and args[i].value.startswith("-"):
        flag = args[i].value
        if flag == "--":
            i += 1
            break
        name = flag.split("=", 1)[0]
        if name in ("-m", "--module") and "=" not in flag:
            check_segment([Word("python"), Word("-m"), *args[i + 1 :]], ctx)
            return
        if name in UV_RUN_VALUE_FLAGS:
            value = flag.split("=", 1)[1] if "=" in flag else None
            if value is None:
                if i + 1 >= len(args):
                    raise Blocked(f"uv run {flag} needs a value")
                value = args[i + 1].value
                i += 1
            if name in ("--directory", "--project"):
                cwd = os.path.abspath(os.path.join(cwd, value))
            i += 1
            continue
        if name in UV_RUN_BOOL_FLAGS:
            i += 1
            continue
        raise Blocked(f"uv run option {flag} is not supported by the guard. Use uv run <command>")
    inner = args[i:]
    if not inner:
        return
    saved = ctx.cwd
    ctx.cwd = cwd
    try:
        check_segment(inner, ctx)
    finally:
        ctx.cwd = saved


def _check_just(args: list[Word]) -> None:
    rest: list[str] = []
    i = 0
    while i < len(args):
        value = args[i].value
        if not rest and (value in ("-c", "--command") or value.startswith("--command=")):
            raise Blocked("just --command is not inspectable. Run the command directly")
        if not rest and value in JUST_VALUE_FLAGS:
            i += 2
            continue
        if not rest and value == "--set":
            i += 3
            continue
        if not rest and value.startswith("-"):
            i += 1
            continue
        rest.append(value)
        i += 1
    if "db-reset" in rest and rest != ["db-reset", "--test"]:
        raise Blocked(
            "only the user resets the development database. "
            "Agents may use uv run just db-reset --test"
        )
    if "psql" in rest:
        _check_sql(rest)


def check_segment(segment: list[Word], ctx: Context) -> None:
    """Inspect one simple command (no operators)."""
    ctx.depth += 1
    if ctx.depth > 8:
        raise Blocked("too many nested commands. Run the command directly")
    try:
        _check_segment(segment, ctx)
    finally:
        ctx.depth -= 1


def _check_segment(segment: list[Word], ctx: Context) -> None:
    if segment[0].glob:
        raise Blocked("dynamic executable is not supported. Name the program literally")
    program = os.path.basename(segment[0].value)
    args = segment[1:]
    vals = _vals(args)
    if "=" in program or program in WRAPPERS:
        raise Blocked("dynamic wrapper is not supported. Invoke the command directly")
    if program == "sudo":
        raise Blocked("the agent never uses sudo. Ask the user")
    if ctx.pipe and program in SHELLS and ctx.previous_program in ("curl", "wget"):
        raise Blocked("never run downloads through a shell. Download, review, then run a file")
    if program in SHELLS:
        index = next((k for k, a in enumerate(args) if SHELL_C.match(a.value)), -1)
        if index < 0 or index + 2 != len(args):
            raise Blocked("shell is not inspectable. Use sh -c '<command>' or explicit commands")
        check_command(args[index + 1].value, ctx.project_dir, ctx.cwd, ctx.depth)
    if INTERPRETER.match(program):
        if any(INLINE_FLAG.match(v) or v == "-" for v in vals):
            raise Blocked("inline code is not inspectable. Use a reviewed file")
        if "-m" in vals:
            module = vals.index("-m")
            if module + 1 < len(args):
                check_segment(args[module + 1 :], ctx)
    if program == "cd":
        if len(args) != 1 or args[0].glob or args[0].value.startswith("~"):
            raise Blocked("cd needs one explicit literal path")
        target = os.path.abspath(os.path.join(ctx.cwd, args[0].value))
        if not os.path.isdir(target):
            raise Blocked("the working directory cannot be verified. cd into an existing directory")
        ctx.cwd = os.path.realpath(target)
    if program == "rm":
        _check_rm(args, ctx)
    if program == "find" and any(
        v in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for v in vals
    ):
        raise Blocked("find cannot run or delete from the guard. List the paths and act explicitly")
    if program == "git":
        _check_git(args)
    if program == "uv":
        if vals[:1] == ["run"]:
            _check_uv_run(args[1:], ctx)
        if vals[:2] == ["pip", "install"] and "--system" in vals:
            raise Blocked("never install into the system Python. Use uv add")
    if program == "just":
        _check_just(args)
    if re.match(r"^pip[\d.]*$", program) and "install" in vals:
        raise Blocked("this repo uses uv. Use uv add <package> (or uv add --dev)")
    if program == "alembic" and "downgrade" in vals:
        test = any(
            v == "-xtest=true" or (v == "-x" and k + 1 < len(vals) and vals[k + 1] == "test=true")
            for k, v in enumerate(vals)
        )
        if not test:
            raise Blocked(
                "never downgrade the development database. "
                "Use uv run alembic -x test=true downgrade"
            )
    if program in ("psql", "pgcli") or (program == "docker" and "psql" in vals):
        _check_sql(vals)
    text = " ".join([program, *vals])
    if program == "docker" and (
        ("down" in vals and any(v in ("-v", "--volumes") for v in vals))
        or re.search(r"\b(volume (rm|prune)|system prune)\b", text)
    ):
        raise Blocked("never delete volumes/data. Use uv run just down (keeps data)")
    if program in ("npm", "yarn", "npx", "bun", "bunx"):
        raise Blocked("this repo uses uv (Python) and pnpm (web, phase 3)")
    if program == "chmod" and any(re.match(r"^0?777$", v) for v in vals):
        raise Blocked("never use 777 permissions. Grant the minimum needed")
    if program in ("cat", "head", "tail", "tee"):
        for arg in args:
            if arg.value.startswith("-"):
                continue
            if arg.glob or arg.value.startswith("~"):
                raise Blocked("reading/writing needs literal, verifiable paths")
            reason = inspect_path(arg.value, ctx.project_dir, ctx.cwd, write=program == "tee")
            if reason:
                raise Blocked(reason)


def check_command(
    command: object, project_dir: str, cwd: str | None = None, depth: int = 0
) -> None:
    """Raise Blocked when the command (a full shell line) is not allowed."""
    if depth > 8:
        raise Blocked("too many nested shells. Run the command directly")
    if not isinstance(command, str) or not command.strip() or "\0" in command:
        raise Blocked("empty or invalid command")
    ctx = Context(project_dir=project_dir, cwd=cwd or project_dir, depth=depth)
    tokens = lex(command)
    segment: list[Word] = []

    def run() -> None:
        nonlocal segment
        if not segment:
            return
        check_segment(segment, ctx)
        ctx.previous_program = os.path.basename(segment[0].value)
        segment = []

    i = 0
    while i < len(tokens):
        token = tokens[i]
        if isinstance(token, Word):
            segment.append(token)
            i += 1
            continue
        if token.value in (">", ">>", "<"):
            target = tokens[i + 1] if i + 1 < len(tokens) else None
            if not isinstance(target, Word) or target.glob or target.value.startswith("~"):
                raise Blocked("redirection target is not verifiable. Use a literal path")
            reason = inspect_path(target.value, project_dir, ctx.cwd, write=token.value != "<")
            if reason:
                raise Blocked(reason)
            i += 2
            continue
        run()
        ctx.pipe = token.value == "|"
        i += 1
    run()


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict) or not isinstance(payload.get("tool_input"), dict):
            raise Blocked("invalid hook payload")
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        cwd = payload.get("cwd", project_dir)
        if not isinstance(cwd, str) or not cwd:
            raise Blocked("invalid working directory in the payload")
        check_command(payload["tool_input"].get("command"), project_dir, cwd)
    except Blocked as error:
        print(f"Command blocked: {error}.", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - fail closed on anything unexpected
        print(f"Command blocked: the guard could not inspect it ({error}).", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
