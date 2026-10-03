#!/usr/bin/env python3
"""PreToolUse(Bash): conservative guard over known shell syntax. Not a sandbox.

Reads the hook payload on stdin; exit 2 with `Command blocked: ...` on stderr blocks the
command, exit 0 allows it. Anything it cannot inspect is blocked (fail closed).
Standard library only, and syntax compatible with Python 3.10+ (the system `python3` runs it).
"""

from __future__ import annotations

import json
import os
import re
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from guard_paths import inspect_path
except Exception as import_error:  # noqa: BLE001 - fail closed when the guard cannot start
    print(f"Command blocked: the guard could not start ({import_error}).", file=sys.stderr)
    sys.exit(2)

SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "csh", "tcsh"}
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
WRAPPERS = {
    "env",
    "eval",
    "exec",
    "command",
    "xargs",
    "nohup",
    "nice",
    "timeout",
    "time",
    "stdbuf",
    "setsid",
    "ionice",
    "flock",
    "chrt",
    "taskset",
    "doas",
    "su",
    "runuser",
    "pkexec",
    "run0",
    "watch",
    "parallel",
    "script",
    "busybox",
    "unbuffer",
    "systemd-run",
    "nsenter",
    "unshare",
    "chroot",
}
# Reserved words start compound commands (`if`, `while`, `!`, ...): the real command follows
# them, so the line is rejected instead of modelled.
RESERVED_WORDS = {
    "!",
    "if",
    "then",
    "elif",
    "else",
    "fi",
    "do",
    "done",
    "while",
    "until",
    "for",
    "in",
    "case",
    "esac",
    "select",
    "coproc",
    "function",
    "[[",
    "]]",
}
# Builtins that run a string as code, change how later words resolve, or install callbacks.
HIDDEN_RUNNERS = {
    "builtin",
    "trap",
    "source",
    ".",
    "alias",
    "hash",
    "enable",
    "shopt",
    "fc",
    "mapfile",
    "readarray",
    "compgen",
    "complete",
    "bind",
}
# Builtins that put variables into the environment of the commands that follow.
ENV_SETTERS = {"export", "declare", "typeset", "readonly", "local"}
# PostgreSQL client programs that destroy data, on the host or inside `docker … exec`.
DB_DROPPERS = {"dropdb", "dropuser", "pg_resetwal"}
# `git config` keys an agent may set; anything else (alias.*, include.*, core.*,
# clean.requireForce, hooks, filters, credential helpers...) can change what git runs.
GIT_CONFIG_SAFE = (
    "user.",
    "color.",
    "advice.",
    "init.defaultbranch",
    "pull.rebase",
    "pull.ff",
    "push.default",
    "push.autosetupremote",
    "fetch.prune",
)
GIT_CONFIG_WRITES = (
    "--unset",
    "--unset-all",
    "--add",
    "--replace-all",
    "--rename-section",
    "--remove-section",
)
INTERPRETER = re.compile(r"^(node|python[\d.]*|perl|ruby|php)$")
# Short options that take the program text, per interpreter, and the options whose value
# ends a cluster of short flags (`python3 -Wc` passes "c" to -W; `python3 -Sc` runs code).
INLINE_LETTERS = {
    "python": ("c", "WXm"),
    "node": ("ep", "r"),
    "perl": ("eE", "IM"),
    "ruby": ("e", "Ir"),
    "php": ("rRBE", "cdf"),
}
INLINE_LONG = re.compile(r"^(--eval|--print|--execute|--run)(=|$)")
STDIN_SCRIPTS = {"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
PYTHON_VALUE_FLAGS = {"-W", "-X", "--check-hash-based-pycs"}
SHELL_C = re.compile(r"^-[a-z]*c[a-z]*$")
PUSH_TO_MAIN = re.compile(r":(refs/heads/)?(main|master)$")
DESTRUCTIVE_SQL = re.compile(r"\b(drop\s+(database|schema|table)|truncate)\b", re.IGNORECASE)
DELETE_FROM = re.compile(r"^\s*delete\s+from\b", re.IGNORECASE)
WHERE = re.compile(r"\bwhere\b", re.IGNORECASE)
DOCKER_DATA_LOSS = re.compile(r"\b(volume (rm|remove|prune)|system prune)\b")
GIT_CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--list", "-l", "get", "list"}

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
    "--from",
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
    __slots__ = (
        "cwd",
        "depth",
        "pipe",
        "previous_program",
        "project_dir",
        "stdin",
        "subshell",
    )

    def __init__(self, project_dir: str, cwd: str, depth: int = 0) -> None:
        self.project_dir = project_dir
        self.cwd = cwd
        self.depth = depth
        self.pipe = False  # fed by the previous command of a pipeline
        self.stdin = False  # stdin comes from a pipe or a `<` redirection
        self.subshell = False  # member of a pipeline: `cd` does not move the next commands
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
                # `>&2`, `2>&1`, `<&-` duplicate or close a descriptor and write no file. Any
                # other word (`>&file`, `>& file`) is a redirection to that file: inspect it.
                j = i + 2
                while j < n and (command[j].isdigit() or command[j] == "-"):
                    j += 1
                if j > i + 2 and (j == n or command[j].isspace() or command[j] in ";|&<>\n"):
                    i = j
                    continue
                i += 1
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


def _options(vals: list[str], takes_value: str = "") -> tuple[set[str], list[str]]:
    """Split git-style arguments into option spellings and positionals.

    Short clusters are expanded (`-uf` → `-u`, `-f`) up to a letter that takes a value
    (`takes_value`); its detached value is skipped. Everything after `--` is positional.
    """
    options: set[str] = set()
    positionals: list[str] = []
    skip = False
    rest = False
    for value in vals:
        if rest:
            positionals.append(value)
        elif skip:
            skip = False
        elif value == "--":
            rest = True
        elif value.startswith("--"):
            options.add(value.split("=", 1)[0])
        elif value.startswith("-") and len(value) > 1:
            for k, letter in enumerate(value[1:], start=1):
                options.add("-" + letter)
                if letter in takes_value:
                    skip = k == len(value) - 1
                    break
        else:
            positionals.append(value)
    return options, positionals


def _has(options: set[str], *spellings: str) -> bool:
    """True when an option matches a short spelling or a long one or its abbreviation."""
    for spelling in spellings:
        if spelling.startswith("--"):
            if any(o.startswith("--") and len(o) > 2 and spelling.startswith(o) for o in options):
                return True
        elif spelling in options:
            return True
    return False


def _check_sql(values: list[str], ctx: Context) -> None:
    if ctx.stdin:
        raise Blocked(
            "SQL piped or redirected into psql is not inspectable. "
            "Pass it with -c, or run a reviewed file with -f <file>"
        )
    for value in values:
        if DESTRUCTIVE_SQL.search(value):
            raise Blocked("destructive SQL. Write a migration or ask the user to run it")
        for statement in value.split(";"):
            if DELETE_FROM.search(statement) and not WHERE.search(statement):
                raise Blocked("DELETE without WHERE. Add a WHERE clause or ask the user")


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(root), path]) == os.path.abspath(root)
    except ValueError:
        return False


def _physical(target: Word, ctx: Context) -> str:
    """The file the kernel removes: the parent resolved through symlinks, the name kept."""
    joined = os.path.join(ctx.cwd, target.value.rstrip("/") or "/")
    return os.path.join(os.path.realpath(os.path.dirname(joined)), os.path.basename(joined))


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
        # The kernel resolves the parent physically (`link/../f` is under the link target).
        physical = _physical(target, ctx)
        reason = inspect_path(abs_path, ctx.project_dir, write=True) or inspect_path(
            physical, ctx.project_dir, write=True
        )
        if reason:
            raise Blocked(reason)
        if recursive:
            if not _inside(abs_path, ctx.project_dir):
                raise Blocked("recursive delete outside the project. Ask the user")
            if os.path.basename(abs_path) not in DISPOSABLE or ".." in target.value.split("/"):
                raise Blocked(
                    "recursive delete outside disposable directories "
                    f"({', '.join(sorted(DISPOSABLE))}). Ask the user"
                )
            if os.path.lexists(abs_path) and os.path.realpath(abs_path) != abs_path:
                raise Blocked(
                    "no recursive delete through symlinks. Remove the link target explicitly"
                )
        elif os.path.lexists(abs_path) or os.path.lexists(physical):
            import subprocess  # noqa: PLC0415 - lazy: only rm of an existing file needs git

            tracked = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", "ls-files", "--error-unmatch", "--", physical],  # noqa: S607
                cwd=ctx.project_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if tracked.returncode != 0:
                raise Blocked(
                    "untracked file without reliable provenance. Ask the user to delete it"
                )


def _broad_pathspec(path: str) -> bool:
    # Any magic pathspec (`:/`, `:!x`, `:^x`, `:(exclude)x`) can mean "everything but".
    stripped = path.rstrip("/")
    return stripped in ("", ".", "*") or stripped.endswith("/.") or path.startswith(":")


def _check_git(args: list[Word], ctx: Context) -> None:
    cwd = ctx.cwd
    while args and args[0].value.startswith("-"):
        if args[0].value == "-C" and len(args) > 1:
            cwd = os.path.abspath(os.path.join(cwd, args[1].value))
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
    if sub == "reset" and _has(_options(vals)[0], "--hard"):
        raise Blocked("git reset --hard discards work. Use git reset (mixed) or ask the user")
    if sub == "clean" and _has(_options(vals, "e")[0], "-f", "--force"):
        raise Blocked("git clean destroys untracked files. Ask the user")
    if sub == "restore":
        options = _options(vals, "s")[0]
        if not _has(options, "-S", "--staged") or _has(options, "-W", "--worktree"):
            raise Blocked("git restore discards work. Use git restore --staged <path>")
    if sub == "checkout":
        options, positionals = _options(vals, "bB")
        branch = "-b" in options
        if (
            "--" in vals
            or _has(
                options,
                "-B",
                "-f",
                "--force",
                "--ours",
                "--theirs",
                "--pathspec-from-file",
                "--pathspec-file-nul",
            )
            or (not branch and len(positionals) > 1)
            or (
                not branch
                and len(positionals) == 1
                and (
                    any(c in positionals[0] for c in "*?[")
                    or positionals[0].startswith(":")
                    or os.path.lexists(os.path.join(cwd, positionals[0]))
                )
            )
        ):
            raise Blocked(
                "git checkout can discard files. Use git switch <branch> or git switch -c"
            )
    if sub == "switch":
        options = _options(vals, "cC")[0]
        if _has(options, "--discard-changes", "-f", "--force"):
            raise Blocked("git switch --discard-changes/--force discards work. Commit first")
        if _has(options, "-C", "--force-create"):
            raise Blocked("git switch -C resets an existing branch. Use git switch -c <new>")
    if sub == "branch":
        options = _options(vals, "u")[0]
        if "-D" in options or _has(options, "-f", "--force"):
            raise Blocked("forced branch delete or reset. Use git branch -d or ask the user")
    if sub == "add":
        options, positionals = _options(vals)
        if _has(
            options,
            "-A",
            "--all",
            "-u",
            "--update",
            "--no-ignore-removal",
            "--pathspec-from-file",
            "--pathspec-file-nul",
        ) or any(_broad_pathspec(p) for p in positionals):
            raise Blocked("staging needs explicit paths. Use git add <path>...")
    if sub == "commit":
        options = _options(vals, "mFCctuS")[0]
        if _has(options, "-a", "--all"):
            raise Blocked("commit needs explicit staging. Use git add <path> then git commit")
        if "-n" in options:
            raise Blocked("never skip hooks. Fix what the hook reports")
    if sub in ("commit", "push", "merge") and _has(_options(vals)[0], "--no-verify"):
        raise Blocked("never skip hooks. Fix what the hook reports")
    if sub == "push":
        options, positionals = _options(vals, "o")
        if _has(options, "-f", "--force", "--mirror", "--prune") or any(
            v in ("main", "master") or v.startswith("+") or PUSH_TO_MAIN.search(v)
            for v in positionals
        ):
            raise Blocked(
                "force push or direct push to the main branch. "
                "Use --force-with-lease on a feature branch"
            )
    if sub == "config":
        _check_git_config(vals)
    _check_git_plumbing(sub, vals)


def _check_git_plumbing(sub: str, vals: list[str]) -> None:
    """Low-level commands that discard work like `checkout -- .` or `reset --hard` do."""
    options = _options(vals)[0]
    discards = (
        (sub == "checkout-index" and _has(options, "-f", "--force"))
        or (sub == "read-tree" and _has(options, "-u", "--reset"))
        or (sub == "rm" and _has(options, "-f", "--force"))
        or (sub == "worktree" and vals[:1] == ["remove"] and _has(options, "-f", "--force"))
        or (sub == "update-ref" and _has(options, "-d", "--delete"))
        or (sub == "reflog" and vals[:1] in (["expire"], ["delete"]))
    )
    if discards:
        raise Blocked(
            f"git {sub} with these options discards work or history. "
            "Use the porcelain command (git rm --cached, git branch -d…) or ask the user"
        )


def _check_git_config(vals: list[str]) -> None:
    """Allow reads and a few harmless keys; any other write can change what git runs."""
    options, positionals = _options(vals)
    if _has(options, "-e", "--edit", "-f", "--file", "--blob"):
        raise Blocked("git config on an editor or another file is not inspectable. Ask the user")
    if any(o in GIT_CONFIG_READS or o == "--get-urlmatch" for o in options):
        return
    if positionals[:1] in (["get"], ["list"]):
        return
    if positionals[:1] in (["set"], ["unset"], ["rename-section"], ["remove-section"], ["edit"]):
        if positionals[0] == "edit":
            raise Blocked("git config edit is not inspectable. Ask the user")
        key = positionals[1] if len(positionals) > 1 else ""
    elif len(positionals) == 1 and not _has(options, *GIT_CONFIG_WRITES):
        return  # `git config <key>` reads it
    else:
        key = positionals[0] if positionals else ""
    if not key.lower().startswith(GIT_CONFIG_SAFE):
        raise Blocked(
            "this git config key can change what git runs (aliases, includes, core.*, "
            "clean.requireForce, hooks). Ask the user"
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


def _check_just(args: list[Word], ctx: Context) -> None:
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
        _check_sql(rest, ctx)


def _check_interpreter(program: str, args: list[Word], ctx: Context) -> None:
    vals = _vals(args)
    family = "python" if program.startswith("python") else program
    letters, value_letters = INLINE_LETTERS[family]
    script: str | None = None
    k = 0
    while k < len(vals):
        value = vals[k]
        if value in STDIN_SCRIPTS:
            raise Blocked("code read from stdin is not inspectable. Run a reviewed script file")
        if INLINE_LONG.match(value):
            raise Blocked("inline code is not inspectable. Use a reviewed file")
        if value == "--":
            script = vals[k + 1] if k + 1 < len(vals) else None
            break
        if value.startswith("--"):
            if family == "python" and value in PYTHON_VALUE_FLAGS:
                k += 1
            k += 1
            continue
        if value.startswith("-"):
            for position, letter in enumerate(value[1:], start=2):
                if letter in letters:
                    raise Blocked("inline code is not inspectable. Use a reviewed file")
                if family == "python" and letter == "m":
                    # `-m mod args` or `-Bmmod args`: check `mod args` as a command.
                    attached = value[position:]
                    module = [Word(attached)] if attached else []
                    inner = module + args[k + 1 :]
                    if inner:
                        check_segment(inner, ctx)
                    return
                if letter in value_letters:
                    if position == len(value) and family == "python":
                        k += 1  # detached value of -W / -X
                    break
            k += 1
            continue
        script = value
        break
    if script in STDIN_SCRIPTS:
        raise Blocked("code read from stdin is not inspectable. Run a reviewed script file")
    if script is None and ctx.stdin:
        raise Blocked(
            "code piped or redirected into an interpreter is not inspectable. "
            "Run a reviewed script file"
        )


def _docker_removes_volumes(vals: list[str]) -> bool:
    if "down" not in vals:
        return False
    for value in vals:
        name, _, setting = value.partition("=")
        if name in ("-v", "--volumes") and setting.lower() not in ("false", "0"):
            return True
        if re.match(r"^-[a-zA-Z]*v[a-zA-Z]*$", value) and not value.startswith("--"):
            return True
    return False


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
    first = segment[0].value
    program = os.path.basename(first)
    args = segment[1:]
    vals = _vals(args)
    if first in RESERVED_WORDS:
        raise Blocked(
            "shell keywords and compound commands (if, while, for, !, coproc...) are not "
            "inspectable. Run each command explicitly"
        )
    # Any `NAME=value` before the program is an environment assignment, whatever the value
    # holds (`X=/a/echo git stash` runs git).
    if "=" in first or program in WRAPPERS:
        raise Blocked("dynamic wrapper is not supported. Invoke the command directly")
    if first in HIDDEN_RUNNERS:
        raise Blocked(f"the {first} builtin can run hidden commands. Invoke the command directly")
    if first in ("pushd", "popd"):
        raise Blocked("pushd/popd are not tracked by the guard. Use cd <literal path>")
    if program in ENV_SETTERS and any(not v.startswith("-") for v in vals):
        raise Blocked(
            "exporting or declaring variables changes how later commands run. "
            "Pass options explicitly"
        )
    if program == "set" and vals:
        # `set -k` turns later `NAME=value` arguments into environment variables (GIT_CONFIG_*
        # aliases, core.hooksPath…); other options change how every later command runs.
        raise Blocked(
            "set changes how the shell runs later commands (set -k injects environment). "
            "Pass options to each command explicitly"
        )
    if program in DB_DROPPERS or (
        program in ("docker", "docker-compose")
        and any(os.path.basename(v) in DB_DROPPERS for v in vals)
    ):
        raise Blocked("destructive database program. Ask the user")
    if program.startswith("git-"):
        raise Blocked("git helper binaries skip the git checks. Use git <subcommand>")
    if program == "sudo":
        raise Blocked("the agent never uses sudo. Ask the user")
    if ctx.pipe and program in SHELLS and ctx.previous_program in ("curl", "wget"):
        raise Blocked("never run downloads through a shell. Download, review, then run a file")
    if program in SHELLS:
        index = next((k for k, a in enumerate(args) if SHELL_C.match(a.value)), -1)
        if index < 0 or index + 2 != len(args):
            raise Blocked("shell is not inspectable. Use sh -c '<command>' or explicit commands")
        check_command(args[index + 1].value, ctx.project_dir, ctx.cwd, ctx.depth, ctx.stdin)
    if INTERPRETER.match(program):
        _check_interpreter(program, args, ctx)
    if program == "cd":
        if len(args) != 1 or args[0].glob or args[0].value.startswith("~"):
            raise Blocked("cd needs one explicit literal path")
        target = os.path.abspath(os.path.join(ctx.cwd, args[0].value))
        if not os.path.isdir(target):
            raise Blocked("the working directory cannot be verified. cd into an existing directory")
        if not ctx.subshell:  # a pipeline member runs in a subshell: the cwd does not change
            # Logical, like bash: `cd link; cd ..` returns to where it started. Paths are then
            # also inspected physically (guard_paths resolves the joined path).
            ctx.cwd = target
    if program == "rm":
        _check_rm(args, ctx)
    if program == "find" and any(
        v in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for v in vals
    ):
        raise Blocked("find cannot run or delete from the guard. List the paths and act explicitly")
    if program == "git":
        _check_git(args, ctx)
    if program == "uv":
        if vals[:1] == ["run"]:
            _check_uv_run(args[1:], ctx)
        if vals[:2] == ["tool", "run"]:
            _check_uv_run(args[2:], ctx)
        if vals[:2] == ["pip", "install"] and "--system" in vals:
            raise Blocked("never install into the system Python. Use uv add")
    if program == "uvx":
        _check_uv_run(args, ctx)
    if program == "just":
        _check_just(args, ctx)
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
    docker = program in ("docker", "docker-compose")
    if program in ("psql", "pgcli") or (docker and "psql" in vals):
        _check_sql(vals, ctx)
    if docker and (_docker_removes_volumes(vals) or DOCKER_DATA_LOSS.search(" ".join(vals))):
        raise Blocked("never delete volumes/data. Use uv run just down (keeps data)")
    if docker:
        # `docker … exec <svc> sh -c '<cmd>'`: the payload is checked like a top-level command.
        shell = next((k for k, a in enumerate(args) if os.path.basename(a.value) in SHELLS), -1)
        if shell >= 0:
            check_segment(args[shell:], ctx)
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
    command: object,
    project_dir: str,
    cwd: str | None = None,
    depth: int = 0,
    stdin: bool = False,
) -> None:
    """Raise Blocked when the command (a full shell line) is not allowed."""
    if depth > 8:
        raise Blocked("too many nested shells. Run the command directly")
    if not isinstance(command, str) or not command.strip() or "\0" in command:
        raise Blocked("empty or invalid command")
    ctx = Context(project_dir=project_dir, cwd=cwd or project_dir, depth=depth)
    tokens = lex(command)

    # Simple commands with their redirections, and the operators between them.
    segments: list[tuple[list[Word], list[tuple[str, Word]]]] = [([], [])]
    separators: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if isinstance(token, Word):
            segments[-1][0].append(token)
            i += 1
            continue
        if token.value in (">", ">>", "<"):
            target = tokens[i + 1] if i + 1 < len(tokens) else None
            if not isinstance(target, Word) or target.glob or target.value.startswith("~"):
                raise Blocked("redirection target is not verifiable. Use a literal path")
            segments[-1][1].append((token.value, target))
            i += 2
            continue
        separators.append(token.value)
        segments.append(([], []))
        i += 1

    for k, (words, redirects) in enumerate(segments):
        before = separators[k - 1] if k > 0 else None
        after = separators[k] if k < len(separators) else None
        for op, target in redirects:
            reason = inspect_path(target.value, project_dir, ctx.cwd, write=op != "<")
            if reason:
                raise Blocked(reason)
        if not words:
            continue
        ctx.pipe = before == "|"
        ctx.stdin = stdin or before == "|" or any(op == "<" for op, _ in redirects)
        ctx.subshell = "|" in (before, after)
        check_segment(words, ctx)
        ctx.previous_program = os.path.basename(words[0].value)


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
    except BaseException as error:  # noqa: BLE001 - fail closed on anything unexpected
        print(f"Command blocked: the guard could not inspect it ({error!r}).", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
