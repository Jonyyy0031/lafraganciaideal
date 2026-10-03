---
status: done
module: platform
min_implementer: high
depends_on: ["001"]
---

# 002 — Enforcement: guard hooks, permissions and generated agents and skills

## Context

### Guard hooks and permissions

The destructive-action rules exist only as text in `AGENTS.md`. web-rh enforces them in Claude
Code with `~/codes/web-rh/.claude/hooks/{guard-bash,guard-files,guard-read,guard-paths,format-file}.mjs`
(+ `hooks.test.mjs`) registered in `.claude/settings.json`, along with allow/ask/deny
permissions. Protocol: JSON on stdin (`tool_input.command`, `file_path`, `notebook_path`,
`edits[]`, `cwd`); block = message on stderr + exit 2; allow = exit 0; invalid/empty payload
fails closed (exit 2). Project root from `CLAUDE_PROJECT_DIR`.

Port to Python stdlib (no venv, fast start: `python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/<x>.py`),
adapted to this stack:

- **guard_bash**: literal-argument lexer (rejects `$`/backticks, `( ) { }`, heredoc and
  background `&`, unclosed quotes, trailing escape; splits on `; | || & && < > >> newline`;
  globs flagged; redirect targets literal and checked as writes). Per segment: nesting ≤ 8; no
  env/eval/exec/command/xargs wrappers or `VAR=` prefixes; no `sudo`; no shells fed by
  curl/wget; shells only as `sh -c '<cmd>'` (checked recursively); no inline code for
  python/node/perl/ruby/php (`-c`, `-e`, `-`); `cd` literal and existing (tracked); `rm` needs
  literal targets, recursive only for disposable dirs (`.venv`, `__pycache__`, `.pytest_cache`,
  `.ruff_cache`, `.mypy_cache`, `node_modules`, `dist`, `build`, `.angular`, `coverage`,
  `htmlcov`) and not through symlinks, non-recursive only for git-tracked files (untracked is
  not disposable); `find` without `-delete`/`-exec*`/`-ok*`; git: no stash (except list/show),
  `reset --hard`, `clean -f`, `restore` of the worktree, discarding `checkout`, `switch
  --discard-changes`, `branch -D`, `add -A/./-u`, `commit -a`, `--no-verify`, force push or push
  to main/master (`--force-with-lease` allowed); `just db-reset` and
  `scripts/…db reset` without `--test`; `alembic downgrade` without `-x test=true`; psql with
  `drop database|schema|table`, `truncate` or `delete from` without WHERE; `docker … down -v`,
  `volume rm|prune`, `system prune`; `pip install`, `npm`, `yarn`, `npx`, `bun` (uv and, from
  phase 3, pnpm); `chmod 777`; cat/head/tail/tee with literal, inspected paths.
- **guard_paths** (shared): never reads content; resolves symlinks through the deepest existing
  parent; always blocks `.env`/`.env.*` except `.env.example`, `*.pem`, `*.key`; on write also
  blocks `.git/`, `uv.lock` (maintained by uv), `apps/api/openapi.json` (use `just openapi`),
  generated adapters (`.claude/agents/*`, `.codex/agents/*`, generated
  `.claude|.agents/skills/{plan,implement,write-tests,review,verify,fix}/SKILL.md`: use `just
  harness-sync`), existing `apps/api/migrations/versions/*.py` (create a new migration).
- **guard_files**: every path of Edit/Write/MultiEdit/NotebookEdit (incl. `edits[]`) checked as
  a write; **guard_read**: Read checked for secrets.
- **format_file** (PostToolUse): `ruff format` + `ruff check --fix` on edited `.py` files inside
  the project; always exits 0.
- **settings.json**: allow read-only and verification commands (`uv run just check|lint|test|…`,
  `git status|diff|log|show *`, `git switch -c *`, `git add *`, compose ps/logs); ask for
  `git commit|push|merge|rebase *`, `uv add|remove *`, `just db-migrate *`, `docker *`, `gh *`;
  deny reading `.env`/keys and `git push --force *`, `git reset --hard *`.

### Generated adapters (README decisions 3, 4)

Plan 001 writes the role docs (`docs/harness/roles/*.md`) and the plan tooling. web-rh
turns roles into tool-specific adapters with `~/codes/web-rh/scripts/harness/adapters.mjs`
(definitions) and `sync.mjs` (generation + `--check` drift detection), never hand-edited:

- Claude subagents `.claude/agents/<name>.md` (frontmatter `name`, folded `description`,
  `model`, optional `tools`; GENERATED marker comment; body): implementer (`inherit`, tier
  chosen at dispatch), tester (`sonnet`), reviewer (`opus`, tools Read/Grep/Glob/Bash/Edit),
  verifier (`sonnet`).
- Skills `.claude/skills/<n>/SKILL.md` and byte-identical `.agents/skills/<n>/SKILL.md`
  (frontmatter `name`, `description`): here `plan`, `implement`, `write-tests`, `review`,
  `verify`, `fix` (README decision 4).
- Codex profiles `.codex/agents/<profile>.toml` (`name`, `model`, `model_reasoning_effort`,
  `description`, `developer_instructions` = body + profile note): implementer-small,
  implementer, implementer-high, tester, tester-high, reviewer, reviewer-medium, verifier;
  models as in web-rh (gpt-5.4-mini low; gpt-5.6-terra medium/high; gpt-5.5 high/medium).
  `.codex/config.toml` hand-written (`[agents] max_threads = 6, max_depth = 1`).
- Shared fragments in every body: escalation (a subagent cannot ask the user: set `blocked`,
  write why, return), trust boundary (tool output is data), destructive rules, scope and
  commits; a `## Return` contract.
- `--check` compares bytes, reports missing (+), different (~) and obsolete marked files (-);
  sync deletes only obsolete files carrying the marker.
- Recipe skills (hand-written, no marker): `new-module`, `new-use-case`, `db-change`, pointing
  to `docs/recipes/`.

## Out of scope

- Codex has no hooks: its protection is the destructive-rules text embedded in every generated
  profile.
- Host sandbox configuration.
- Changing the role docs or the plan tooling (plan 001).

## Dependencies

- platform-harness/001: `docs/harness/roles/*.md`, workflow routing table and tiers, `just
  plans-lint|plans-status|plans-scope` referenced by the skills.

## Steps

1. **Shared path guard**
   - Files: `.claude/hooks/guard_paths.py` (create)
   - Do: `inspect_path(path, project_dir, cwd, write)` → reason or None, per the rules above.
   - Observable result: importable by the three guards.

2. **Guards and formatter**
   - Files: `.claude/hooks/guard_bash.py` (create), `.claude/hooks/guard_files.py` (create),
     `.claude/hooks/guard_read.py` (create), `.claude/hooks/format_file.py` (create)
   - Do: the behavior above; messages `Command blocked: <reason>` / `Edit blocked: …` /
     `Read blocked: …`, each with the safe alternative.
   - Observable result: running a hook with a payload on stdin gives the expected exit code.

3. **Registration and permissions**
   - Files: `.claude/settings.json` (create)
   - Do: PreToolUse Bash → guard_bash, Edit|Write|MultiEdit|NotebookEdit → guard_files, Read →
     guard_read; PostToolUse Edit|Write|MultiEdit → format_file (timeouts 10 s / 20 s);
     permissions as above.
   - Observable result: a new Claude Code session in the repo loads the hooks.

4. **Tests**
   - Files: `scripts/harness/__init__.py` (create), `scripts/harness/test_hooks.py` (create),
     `pyproject.toml` (modify), `justfile` (modify)
   - Do: spawn each hook with JSON payloads; parametrized blocks and allows (including web-rh's
     must-pass cases adapted: `rm -rf .venv`, `git add -p f`, `git restore --staged f`,
     `git show HEAD:f > f`, `echo "DROP TABLE x" > /tmp/n`, `sh -c 'uv run just check'`,
     `cat apps/api/.env.example`), adversarial inputs (invalid JSON, empty payload, NUL, symlinks
     into `.env`, untracked rm). Recipe `test-harness`, part of `check`.
   - Observable result: `uv run just test-harness` green.

5. **Docs**
   - Files: `docs/harness/HARNESS.md` (modify), `docs/harness/security.md` (modify), `CLAUDE.md` (modify)
   - Do: enforcement table and controls matrix point to the hooks; note that hooks run only when
     settings are loaded and Codex runs none.

6. **Definitions and generator**
   - Files: `scripts/harness/adapters.py` (create), `scripts/harness/sync.py` (create)
   - Do: the definitions and output formats above (Python; TOML strings with `'''`, error if a
     body contains `'''`); `sync.py [--check]`.
   - Observable result: `uv run just harness-sync` writes every adapter.

7. **Generated adapters**
   - Files: `.claude/agents/` (create), `.claude/skills/plan/` (create), `.claude/skills/implement/` (create),
     `.claude/skills/write-tests/` (create), `.claude/skills/review/` (create), `.claude/skills/verify/` (create),
     `.claude/skills/fix/` (create), `.agents/skills/` (create), `.codex/agents/` (create), `.codex/config.toml` (create)
   - Do: run the generator; commit the outputs.
   - Observable result: `/plan`, `/implement`, … appear in Claude Code; Codex lists the profiles.

8. **Recipe skills**
   - Files: `.claude/skills/new-module/SKILL.md` (create), `.claude/skills/new-use-case/SKILL.md` (create),
     `.claude/skills/db-change/SKILL.md` (create)
   - Do: short skills that load `docs/recipes/*.md` and the reference module.

9. **Tests and check**
   - Files: `scripts/harness/test_sync.py` (create), `justfile` (modify), `.github/workflows/ci.yml` (modify)
   - Do: generation is deterministic, `--check` detects missing/changed/obsolete files in a temp
     dir, `'''` guard; recipes `harness-sync`, `harness-check` (in `check`).
   - Observable result: editing a generated file by hand fails `just check`.

10. **Docs**
   - Files: `docs/harness/HARNESS.md` (modify), `docs/harness/workflow.md` (modify), `AGENTS.md` (modify), `CLAUDE.md` (modify),
     `docs/harness/conventions/testing.md` (modify), `docs/harness/roles/reviewer.md` (modify)
   - Do: adapter architecture diagram ("agnostic core + generated adapters"), how to change a
     role (edit the doc or `adapters.py`, run `just harness-sync`).

## Acceptance criteria

- [ ] In a live Claude Code session in this repo: `git stash`, `docker compose down -v`,
      reading `apps/api/.env` and editing `uv.lock` are blocked with the reason and the safe
      alternative; `uv run just check` runs.
- [ ] Editing a `.py` file reformats it with ruff.
- [ ] `uv run just harness-check` passes; a manual edit to `.claude/agents/tester.md` makes it
      fail with `~ .claude/agents/tester.md`.
- [ ] A new Claude Code session lists the skills `plan`, `implement`, `write-tests`, `review`,
      `verify`, `fix`, `new-module`, `new-use-case`, `db-change` and the four subagents.
- [ ] `.codex/agents/*.toml` parse as TOML (checked by a test).
- [ ] `uv run just check` runs `test-harness` and `harness-check`; CI green.

## Test layers required

| Layer       | Applies | Focus                                                          |
| ----------- | ------- | -------------------------------------------------------------- |
| tooling     | yes     | every hook block/allow rule, adversarial payloads, sync determinism, drift, TOML |
| e2e         | yes     | live Claude Code session: hooks block, skills/subagents listed (manual) |
| domain      | no      |                                                                |
| http        | no      |                                                                |
| integration | no      |                                                                |

## Deviations

Format: said / reality / done.

1. **Steps 1–4 written before this phase.** Said: implement steps in order once 002 is
   `implementing`. Reality: the hooks (`.claude/hooks/*.py`), `.claude/settings.json`,
   `scripts/harness/__init__.py` and `scripts/harness/test_hooks.py` (335 tests) were written by
   a subagent while plan 001 was still open (untracked, hooks already live). Done: reviewed them
   against web-rh's guards; no real defect found, kept as is. Its intentional differences from
   web-rh, recorded here:
   - `2>&1`, `>&2`, `2>/dev/null` allowed (fd duplication writes no file; `/dev/null` is a
     checked literal target).
   - Extra wrappers blocked: `nohup`, `nice`, `timeout`, `time`, `stdbuf`.
   - Forced refspecs (`git push origin +branch`) blocked like `--force`.
   - NUL in a command blocked (fail closed).
   - `uv run` treated as transparent: its flags are skipped, `-m` is checked as `python -m`,
     `--directory`/`--project` move the checked cwd.
   - `just db-reset` allowed only as exactly `db-reset --test`.
   - psql SQL checks also apply to `docker … psql`; DELETE-without-WHERE is checked per
     statement.
   - `format_file` runs `ruff check --fix` before `ruff format` (fixes can leave blank lines),
     both with `--force-exclude`.
   - The `scripts/…db reset` rule was not ported: this repo has no such script.
2. **`pyproject.toml` unchanged.** Said: step 4 modifies it. Reality: `S101`/`S603`/`S607` were
   already ignored for `scripts/**/test_*.py` and `testpaths` already covers `scripts`. Done:
   nothing to change.
3. **`check` does not run the harness tests twice.** Said: `test-harness` part of `check`.
   Reality: `uv run pytest` already collects `scripts/harness`. Done: `check` runs
   `harness-check`, `(test "--ignore=scripts/harness")` and `test-harness`; CI quality does the
   same (`pytest --ignore=scripts/harness`, `just harness-check`, `just test-harness`).
4. **Plan-002 markers outside the listed docs.** Said: steps 5/10 touch `HARNESS.md`,
   `security.md`, `workflow.md`, `AGENTS.md`, `CLAUDE.md`. Reality: plan 001 also left
   "plan 002" markers in `docs/harness/roles/reviewer.md` (checklist line) and
   `docs/harness/conventions/testing.md` (two lines). Done: removed those markers only (wording,
   no rule change); `plans-scope --base HEAD` reports exactly these two files as extra.
5. **Generator details.** `sync.py` runs as `PYTHONPATH=scripts uv run python -m harness.sync`
   (like the plans recipes) and also rejects `'''` in a profile description, not only in the
   body; unknown arguments exit 2. Codex model names and fragments are web-rh's, with the
   destructive fragment adapted to Alembic/`db-reset --test`/docker volumes and the findings
   path `plans/findings/`. The verbatim web-rh trust-boundary lines were reflowed to the
   100-column ruff limit.
6. **Recipe skills are short pointers** (`docs/recipes/*.md`, `apps/api/README.md`, the
   catalog files) rather than web-rh's long inline recipes, so the recipe docs stay the single
   source.
7. Step 10's adapter diagram and "how to change a role" already existed in `HARNESS.md` and
   `CLAUDE.md` from plan 001; only the "arrives with 002" caveats were removed and the hook
   registration/Codex note added.

8. **Repairs after review** (2026-10-03, reviewer findings 1–11 and the tester's 4 GAPs).
   Said: the guards enforce the listed rules. Reality: the review showed bypasses. Done:
   - High 1: `>&word` / `>& word` with a non-numeric word is a write redirect and is inspected;
     only `>&N`, `N>&M`, `>&-` are descriptor duplication.
   - High 2: `check_command` now splits the line into simple commands with their redirections;
     a `cd` that is a pipeline member is validated but does not move the tracked cwd.
   - High 3: git arguments are parsed like git does (`_options`/`_has`): abbreviated long
     options (`--har`, `--forc`, `--al`, `--work`, `--no-verif`), clustered short flags
     (`-uf`, `-df`, `-Av`, `-fd`), `switch -f/--force`, `commit -n`, `checkout -B`,
     `checkout <path>` when the single argument exists as a path or is a glob/`:` pathspec
     (web-rh's `len > 1` rule kept), `add ./`, `add dir/.`, `clean --force`.
   - High 4: a command fed by a pipe or a `<` redirection may not be an interpreter without a
     script file (`python*`, `node`, `perl`, `ruby`, `php`; `/dev/stdin` scripts too), nor
     `psql` / `just psql` / `docker[-compose] … psql` (message: use `-c` or `-f <file>`);
     stdin propagates into `sh -c`.
   - Medium 5: hooks parse on Python 3.10+ (parenthesized `except`; ruff
     `per-file-target-version` py310 for `.claude/hooks/*.py` in `pyproject.toml` so the
     formatter keeps it); the `guard_paths` import is inside a `try` and every unexpected
     exception (`BaseException`) exits 2.
   - Medium 6/7: wrappers `setsid`, `ionice`, `flock`, `chrt`, `taskset`; shells `ksh`, `mksh`,
     `csh`, `tcsh`; `uvx` and `uv tool run` checked like `uv run`; inline code in clusters
     (`-Sc`, `-mpip`, `perl -pe`, `php -r`); docker `volume remove`, `--volumes[=true]`,
     `-v=…`, clustered `-tv`, and `docker-compose` treated like `docker compose`.
   - Low 8: recursive `rm` outside the project is blocked. Low 9: `git config` setting
     `alias.*` or `core.hooksPath` is blocked (reads allowed). Low 10: `sync.py` deletes only
     files whose notice comment heads one of the first 15 lines (`is_generated`) and refuses to
     write through a symlink. Low 11: the first acceptance criterion was rewritten.
   - Checklist: `docs/harness/conventions/testing.md` and `docs/harness/roles/reviewer.md` are
     now declared in step 10 (see Deviation 4). `pyproject.toml` is now modified (Deviation 2
     superseded).
   - Tests: the 8 strict-`xfail` GAP cases are regular tests now; regressions for every finding
     were added at the end of `scripts/harness/test_hooks.py` (`REVIEW_BLOCKS`, allows,
     pipeline `cd`, checkout of a path, 3.10 syntax, missing `guard_paths`) and
     `scripts/harness/test_sync.py` (header marker, symlink). The implementer wrote these
     because each one reproduces a bypass the reviewer reported. `docs/harness/security.md`
     "Shell subset" describes the new checks.

9. **Repairs after review, round 2** (2026-10-03, re-review findings High 1–3, Medium 4–8,
   Low 9–11). Said: the guards enforce the listed rules. Reality: shell constructs the guard
   did not model still bypassed them. Done, preferring to reject over modelling
   (`.claude/hooks/guard_bash.py` unless noted):
   - High 1: shell reserved words (`if then elif else fi do done while until for in case esac
     select ! coproc function [[ ]]`) as the first word are rejected; builtins that run a
     string or change word resolution (`builtin trap source . alias hash enable shopt fc
     mapfile readarray compgen complete bind`) are rejected. More wrappers on the same grounds:
     `doas su runuser pkexec run0 watch parallel script busybox unbuffer systemd-run nsenter
     unshare chroot`.
   - High 2: any `=` in the first word (not its basename) is an assignment and is rejected.
   - High 3: `pushd`/`popd` are rejected (use `cd`).
   - Medium 4: `cd` keeps the logical path (`os.path.abspath`, no `realpath`), like bash.
     Because the kernel resolves symlinks before `..` for every other path,
     `.claude/hooks/guard_paths.py` `inspect_path` now also checks `os.path.realpath` of the
     joined (un-normalized) path, and `rm`'s tracked check uses the physically resolved parent.
     This closes a bypass found while repairing (not in the review): `echo x > hooks/../config`
     with `hooks -> .git/hooks` wrote `.git/config` while being inspected as `<root>/config`.
   - Medium 5: `export declare typeset readonly local` with any non-option argument are
     rejected (all variables, not only `GIT_*`); `export -p`/`declare -p` still allowed.
   - Medium 6: programs whose basename starts with `git-` are rejected.
   - Medium 7: `--pathspec-from-file`/`--pathspec-file-nul` (and abbreviations) are blocked in
     `git checkout` and `git add`.
   - Medium 8: in a `docker`/`docker-compose` command, the first argument named like a shell is
     checked as a top-level segment: only `sh -c '<cmd>'` is accepted and `<cmd>` is checked
     recursively (SQL, pipes into `psql`, …).
   - Low 9: `git config` writes are allowed only for `user.*`, `color.*`, `advice.*`,
     `init.defaultBranch`, `pull.rebase|ff`, `push.default|autoSetupRemote`, `fetch.prune`;
     reads (`--get*`, `--list`, `get`, `list`, a single key) stay allowed; `-e/--edit`,
     `-f/--file`, `--blob` are blocked.
   - Low 10: `switch -C/--force-create`, `branch -f/--force` (any use), `push --mirror` and,
     on the same grounds, `push --prune` are blocked.
   - Low 11: any `git add` pathspec starting with `:` (magic: `:!x`, `:^x`, `:(exclude)x`, `:/`)
     is treated as broad.
   - Tests: `ROUND2_BLOCKS` (78 cases, each finding has at least one case that the pre-repair
     guard allowed, checked against a scratchpad copy of it; 8 cases were already blocked and
     stay as complementary coverage), round-2 allows (every must-pass allow of the plan still
     passes, plus config reads, `export -p`, docker `sh -c 'psql … select'`), message test,
     and fixture tests with symlinks (logical `cd`, `hooks/../config`, physical `rm` parent,
     `pushd`). `docs/harness/security.md` now states that the guard is a best-effort denylist
     that catches accidents, not a sandbox, and lists the rejected constructs.
   - Not handled (residual, documented): `CDPATH` from the user's environment can make `cd`
     land elsewhere; `git -C` and `uv run --directory` paths are still joined lexically.

Not verified here (manual, e2e layer): a fresh Claude Code session listing the skills and
subagents; Codex listing the profiles; the hooks on an interpreter older than 3.14 (only parsed
with `ast` at `feature_version=(3, 10)` and ruff's py310 target).

## Test coverage

Round 1 (superseded by the repair): baseline `uv run just check` green (348 harness tests); 8 strict-xfail GAPs.

Round 3 (after the round-2 review repair, Deviation 9; tester, 2026-10-03). Baseline
`uv run just check` green: API 238 passed, 3 skipped; harness 655 passed, 0 xfail;
`harness-check` 24 adapters up to date. The tester added no tests: every round-2 finding
(High 1-3, Medium 4-8, Low 9-11) already has a regression in `scripts/harness/test_hooks.py`
(`ROUND2_BLOCKS` -> `test_guard_bash_blocks_round2_bypasses`, round-2 allows
`test_guard_bash_allows_after_round2_repairs`, `test_guard_bash_round2_messages_name_the_construct`,
and the symlink fixture tests `test_guard_bash_cd_is_logical_like_bash`,
`test_guard_paths_resolve_symlinks_before_dot_dot`,
`test_guard_bash_rm_resolves_the_parent_physically`,
`test_guard_bash_pushd_cannot_desynchronise_the_cwd`), written by the implementer because each
reproduces the reported bypass. Tester probe (scratchpad script, 52 commands against the live
`guard_bash.py`) confirmed independently: reserved words (`if`, `!`, `case`, `{`), `trap`,
`eval`, `exec`, `time`, `X=/a/echo`, `Y=1`, `pushd`/`popd`, `export GIT_CONFIG_COUNT`,
`/usr/lib/git-core/git-stash`, `--pathspec-f=`, `git add ':!x'`, `switch -C`, `branch -f`,
`push --mirror|--prune`, `git config include.path|clean.requireForce|--file|-e|--system`, and
docker `exec … sh -c 'psql … drop'` all exit 2; reads and must-pass cases (`git config
user.name x`, `git config --get alias.x`, `export -p`, `git stash list`, `git status`,
`uv run just check`) exit 0. No new bypass found, no GAP. Closing run = baseline (no file
touched): same result. Layer: tooling for every row.

| Behavior (round-2 repair) | Source | Test | State |
| --- | --- | --- | --- |
| Shell reserved words and string-running builtins rejected | `guard_bash.py` | H `test_guard_bash_blocks_round2_bypasses` | CONFIRMED (High 1) |
| Any `=` in the first word is an assignment, rejected | `guard_bash.py` | same | CONFIRMED (High 2) |
| `pushd`/`popd` rejected | `guard_bash.py` | same, `test_guard_bash_pushd_cannot_desynchronise_the_cwd` | CONFIRMED (High 3) |
| `cd` logical; paths realpath-checked incl. `hooks/../config` | `guard_bash.py`, `guard_paths.py` | `test_guard_bash_cd_is_logical_like_bash`, `test_guard_paths_resolve_symlinks_before_dot_dot`, `test_guard_bash_rm_resolves_the_parent_physically` | CONFIRMED (Medium 4) |
| `export/declare/typeset/readonly/local` with arguments rejected; `-p` allowed | `guard_bash.py` | blocks + allows tests | CONFIRMED (Medium 5) |
| `git-*` programs rejected | `guard_bash.py` | blocks | CONFIRMED (Medium 6) |
| `--pathspec-from-file` in checkout/add | `guard_bash.py` | blocks | CONFIRMED (Medium 7) |
| docker/compose `sh -c` inner command checked recursively | `guard_bash.py` | blocks, allows | CONFIRMED (Medium 8) |
| `git config` write allowlist, reads allowed | `guard_bash.py` | blocks, allows | CONFIRMED (Low 9) |
| `switch -C`, `branch -f`, `push --mirror|--prune` | `guard_bash.py` | blocks | CONFIRMED (Low 10) |
| `git add` magic pathspec `:` is broad | `guard_bash.py` | blocks | CONFIRMED (Low 11) |
| Residual: `CDPATH`, `git -C` / `uv run --directory` joined lexically | n/a | none | NOT CONFIRMED (documented residual, not testable without the user's environment) |
| Hooks on an interpreter older than 3.14 | n/a | `test_hooks_parse_as_python_3_10` (ast only) | NOT CONFIRMED on a real interpreter |
| Live Claude Code session, Codex profiles | n/a | none | NOT CONFIRMED (manual e2e) |

Round 2 (after the review repair, 2026-10-03; superseded by the round-2 repair): baseline `uv run just check` green (API 238 passed, 3 skipped; harness 545 passed, 0 xfail, `harness-check` 24 adapters up to date). The regression tests the implementer added for each reviewer finding are listed below; the tester added none (each already reproduces the reported bypass).

The Round 1 table follows; its GAP states are superseded by the Round 2 table. Tests live in
`scripts/harness/test_hooks.py` (H) and `scripts/harness/test_sync.py` (S). Tester additions are
at the end of each file; GAPs are strict `xfail`. Layer is `tooling` for every row (e2e below).

| Behavior | Source | Test | State |
| --- | --- | --- | --- |
| Lexer: rejects `$`/backtick, `( ) { }`, heredoc, `&`, unclosed quote, trailing `\`, NUL, empty | `guard_bash.py:127-220,483` | H `test_guard_bash_blocks` (lexer group), `test_hooks_fail_closed` | CONFIRMED |
| Splits on `; \| \|\| && < > >> \n`; `>\|`, `&>` blocked; `2>&1`, `>&2` allowed | `guard_bash.py:189-211,504-515` | H `blocks`/`allows`, `test_guard_bash_blocks_adversarial_variants` | CONFIRMED |
| Redirect targets literal and checked as writes | `guard_bash.py:504-512` | H `blocks` (uv.lock, .git, adapters, migration), `allows` (`/tmp`, 9999 migration, `.env.example`) | CONFIRMED |
| Nesting <= 8, recursive `sh -c` | `guard_bash.py:388,412,481` | H `test_guard_bash_nesting_beyond_eight_levels_is_blocked`, `test_guard_bash_nested_shell_checks_inner_command` | CONFIRMED |
| Wrappers, `VAR=`, sudo, curl\|sh, bare shells | `guard_bash.py:402-411` | H `blocks` (shells group) | CONFIRMED |
| No inline code python/node/perl/ruby/php (`-c`, `-e`, `-`) | `guard_bash.py:34-35,413-419` | H `blocks` (`-c`, `-e`, `-`); `test_guard_bash_blocks_inline_python_in_combined_flags` | CONFIRMED for plain flags; GAP: `python3 -Sc/-ic/-Bc` run inline code |
| `cd` literal, existing, tracked | `guard_bash.py:420-426` | H `blocks` (cd), `test_guard_bash_tracks_cd` | CONFIRMED |
| `rm`: literal, recursive only disposable, no symlinks, non-recursive only tracked | `guard_bash.py:236-272` | H `test_guard_bash_rm_in_fixture`, `blocks`, `allows` | CONFIRMED |
| `find` without delete/exec/ok | `guard_bash.py:429-432` | H `blocks` (find) | CONFIRMED |
| git: stash, reset --hard, clean -f, restore, checkout, switch, branch -D, commit -a, --no-verify, force/main push | `guard_bash.py:275-317` | H `blocks`/`allows` (git groups), adversarial variants | CONFIRMED |
| git clean `--force` | `guard_bash.py:292` | H `test_guard_bash_blocks_git_clean_long_force` | GAP (regex misses the long option) |
| git add `-A/./-u` | `guard_bash.py:304` | H `test_guard_bash_blocks_broad_git_add_variants` | CONFIRMED for exact tokens; GAP: `-Av`, `./`, `apps/.` |
| `db-reset` only `--test`; alembic downgrade needs `-x test=true`; psql DROP/TRUNCATE/DELETE w/o WHERE | `guard_bash.py:358-383,444-455` | H `blocks`/`allows` (database group) | CONFIRMED |
| docker `down -v`, `volume rm/prune`, `system prune` | `guard_bash.py:457-461` | H `blocks` (docker group) | CONFIRMED for `docker`; GAP: `docker-compose down -v` (`test_guard_bash_blocks_legacy_docker_compose_down_volumes`) |
| pip install, npm/yarn/npx/bun, chmod 777 | `guard_bash.py:442,462-465` | H `blocks` (package managers, chmod), `allows` (`pnpm`, `chmod 755`) | CONFIRMED |
| cat/head/tail/tee literal, inspected paths | `guard_bash.py:466-474` | H `blocks`/`allows` (`.env.example`), symlink test | CONFIRMED |
| guard_paths: `.env*` except `.env.example`, `*.pem`, `*.key` (read and write, via symlinks) | `guard_paths.py:30-36,57-58` | H `test_guard_files_blocks`, `test_guard_read`, `test_guard_read_blocks_every_secret_shape_but_not_other_paths`, `test_symlinks_into_secrets_and_git`, `test_symlink_loop_fails_closed` | CONFIRMED |
| guard_paths write-only: `.git/`, `uv.lock`, `apps/api/openapi.json` | `guard_paths.py:61-67` | H `test_guard_files_blocks`/`allows`, `test_guard_files_path_edge_cases`, `test_guard_read` (reads allowed) | CONFIRMED |
| guard_paths write-only: generated adapters; recipe skills and `.codex/config.toml` allowed | `guard_paths.py:11-13,68-72` | H `test_guard_files_blocks`/`allows`, `test_guard_files_path_edge_cases` | CONFIRMED |
| guard_paths write-only: existing migrations blocked, new allowed, symlinked new blocked | `guard_paths.py:73-74` | H `test_guard_files_blocks`/`allows`, `test_symlinks_into_secrets_and_git`, `test_guard_files_blocks_a_symlinked_new_migration` | CONFIRMED |
| guard_files: Edit/Write/NotebookEdit path and every `edits[]` member, malformed shapes fail closed | `guard_files.py:22-39,51-54` | H `test_guard_files_notebook_and_multiedit`, `..._checks_every_edit_of_a_multiedit...`, `..._secret_in_edits...`, `test_hooks_fail_closed` | CONFIRMED |
| Messages `Command/Edit/Read blocked: <reason + alternative>`; block = stderr + exit 2 | `guard_bash.py:530`, `guard_files.py:56`, `guard_read.py` | H `test_guard_*_message_format` | CONFIRMED |
| guard_read: secrets only, fails closed without `file_path` | `guard_read.py` | H `test_guard_read`, `test_guard_read_without_file_path_fails_closed` | CONFIRMED |
| format_file: `ruff check --fix` + `format` inside project only, always exit 0, no ruff = no-op | `format_file.py:16-47` | H `test_format_file_*` (5 tests incl. escaping path, non-string path) | CONFIRMED |
| settings.json: events, matchers, commands, timeouts 10/20 | `.claude/settings.json` | H `test_settings_register_each_hook_on_its_event_matcher_and_timeout`, `test_settings_register_every_hook` | CONFIRMED |
| settings.json permissions allow/ask/deny | `.claude/settings.json` | H `test_settings_permissions_allow_ask_deny_shape` | CONFIRMED (static shape; Claude Code applying them is e2e) |
| Sync deterministic; outputs cover all agents, profiles, skills; marker everywhere | `sync.py:73-82` | S `test_generation_is_deterministic`, `test_outputs_cover_...`, `test_every_output_carries_the_marker` | CONFIRMED |
| Skills byte-identical in `.claude` and `.agents` | `sync.py:80-81` | S `test_skills_are_identical_in_both_trees`, drift in `.agents` tree | CONFIRMED |
| Codex TOML valid (generated and committed), `.codex/config.toml` limits | `sync.py:51-63` | S `test_every_codex_profile_parses_as_toml`, `test_committed_codex_files_parse_as_toml`, `test_committed_codex_config_declares_the_agent_limits` | CONFIRMED |
| `'''` rejected in body and in description | `sync.py:54-55` | S `test_triple_quote_in_a_body_is_rejected`, `..._profile_description_...` | CONFIRMED |
| `--check` reports `+`/`~`/`-`, never writes or deletes; exit 1 on drift, 2 on unknown args | `sync.py:105-148` | S `test_check_reports_...`, `test_check_never_writes_or_deletes`, `test_main_*` | CONFIRMED |
| Sync deletes only marked obsolete files (all four roots; not unmarked, symlinks, out-of-root); idempotent | `sync.py:85-124` | S `test_sync_writes_and_deletes_...`, `..._in_every_generated_root`, `test_sync_never_deletes_...`, `test_second_sync_is_a_no_op` | CONFIRMED |
| Committed repo in sync, byte for byte; recipe skills hand-written; shared fragments in every body | `sync.py`, `adapters.py` | S `test_the_repository_is_in_sync`, `test_committed_adapters_match_...`, `test_recipe_skills_...`, `test_every_generated_body_carries_the_shared_fragments` | CONFIRMED |
| Recipes `test-harness`, `harness-sync`, `harness-check` in `check` | `justfile:104-130` | closing `uv run just check` runs both | CONFIRMED (execution) |
| Live Claude Code session blocks/lists skills and subagents; Codex lists profiles | n/a | none | NOT CONFIRMED (manual e2e; not runnable here) |

### Round 2 (repair) matrix

| Behavior | Source | Test | State |
| --- | --- | --- | --- |
| `>&word` write redirect inspected; `>&N`, `N>&M`, `>&-` allowed | `guard_bash.py` | H `test_guard_bash_blocks_review_bypasses` (`REVIEW_BLOCKS`), `test_guard_bash_allows_after_review_repairs` | CONFIRMED (finding 1) |
| `cd` in a pipeline does not move the tracked cwd | `guard_bash.py` | H `test_guard_bash_cd_inside_a_pipeline_does_not_move_the_cwd` | CONFIRMED (finding 2) |
| git abbreviated/clustered options, `switch -f`, `commit -n`, `checkout <path>`, `add ./`, `clean --force` | `guard_bash.py` | H `test_guard_bash_blocks_review_bypasses`; former GAP tests now regular (`clean --force`, `add -Av`, `./`, `dir/.`) | CONFIRMED (finding 3, 2 former GAPs) |
| Interpreter / psql fed by pipe or `<` blocked; propagates into `sh -c` | `guard_bash.py` | H `test_guard_bash_blocks_review_bypasses` | CONFIRMED (finding 4) |
| Hooks parse on Python 3.10; missing `guard_paths` fails closed (exit 2) | `.claude/hooks/*.py` | H `test_hooks_parse_as_python_3_10`, `test_hooks_fail_closed` | CONFIRMED for syntax (`ast`); NOT CONFIRMED on a real interpreter older than 3.14 |
| Extra wrappers/shells, `uvx`, inline-code clusters (`-Sc`), docker `volume remove`, `--volumes`, `docker-compose` | `guard_bash.py` | H `test_guard_bash_blocks_review_bypasses`; former GAPs now regular (`-Sc`, `docker-compose down -v`) | CONFIRMED (findings 6, 7, 2 former GAPs) |
| Recursive `rm` outside the project; `git config alias.*`/`core.hooksPath` blocked (reads allowed) | `guard_bash.py` | H `test_guard_bash_blocks_review_bypasses`, `test_guard_bash_allows_after_review_repairs` | CONFIRMED (findings 8, 9) |
| Sync deletes only files with the notice in the first 15 lines; refuses to write through a symlink | `sync.py` | S `test_every_output_is_recognized_as_generated_by_its_header`, `test_a_hand_written_file_quoting_the_marker_is_never_deleted`, `test_sync_refuses_to_write_through_a_symlink` | CONFIRMED (finding 10) |

GAP summary, Round 1 (superseded: all 8 xfail cases are regular passing tests after the repair; no GAP remains; originally strict xfail, 8 xfail cases): combined python flags
(`-Sc`), `git clean --force`, `git add -Av|./|dir/.`, `docker-compose down -v`.

## Review findings

Reviewer, 2026-10-03, diff `ee162d3..3b4f9df`. Hooks were exercised with crafted payloads from
a scratchpad script (`uv run python probe.py`); git/bash behavior was confirmed in a scratch repo.

### Checklist — FAILED (1 item)

- [ ] `uv run just plans-scope … --base ee162d3`: **fails**: `docs/harness/conventions/testing.md`
      and `docs/harness/roles/reviewer.md` are out of scope. Deviation 4 records them, but no step's
      Files list names them. `pyproject.toml` is declared but unchanged (Deviation 2, fine).
- [x] `uv run just check`: green (429 harness passed, 8 strict xfail; API 238 passed).
- [x] Integration / domain / CQRS / contracts / Money / migrations / routes / wiring: N/A (no
      `apps/api` change).
- [x] No secrets in the diff.
- [x] Deviations honest. Spot-checked #1 (`2>&1`/`>&2` allowed: `guard_bash.py:198-203`), #3
      (`justfile:130`) and #4 (markers removed: there are no "plan 002" leftovers in docs/AGENTS/CLAUDE).
- [x] Adapters match web-rh `adapters.mjs` in substance: load order, gates, all four
      fragments adapted (Alembic, `db-reset --test`, docker volumes, `plans/findings/`), the six
      skill names, Codex profiles/models/efforts, and reviewer tools. Every referenced path
      exists.
- [x] Docs updated (HARNESS, security, workflow, AGENTS, CLAUDE).
- [x] PR body: N/A (no PR yet).

### Findings

**High: these bypass rules the plan lists as enforced. Each one is exit 0 from the live guard.**

1. `guard_bash.py:198-203`: bash's `>&word` (with a non-numeric `word`) is treated as fd
   duplication, so `word` becomes a plain argument and is never inspected as a write. Example:
   `echo x >&uv.lock` or `… >&apps/api/openapi.json` overwrites a protected file. Confirmed: bash
   created `out.txt` from `>&out.txt`.
2. `guard_bash.py:420-426,513-515`: a `cd` inside a pipeline (`|`) runs in a subshell, but the guard
   still moves its tracked cwd. Example: `cd /tmp | echo x > uv.lock` is inspected as `/tmp/uv.lock`,
   but bash writes the repo's `uv.lock`. The same trick defeats the untracked-`rm` check:
   `cd apps | rm notes.txt`.
3. `guard_bash.py:288-317`: git rules only match exact tokens. Git accepts abbreviated long
   options and clustered short flags, so all of these pass:
   - `git reset --har`: confirmed, it discarded work in a scratch repo.
   - `git switch -f` / `--force`: confirmed, it discarded work. It is equivalent to
     `--discard-changes`.
   - `git push -uf`, `git push --forc`.
   - `git branch -d -f x`, `git branch --delete --force x`.
   - `git commit --al`, `git add --al`.
   - `git restore --staged --work f`.
   - `git checkout <path>` with a single path argument discards that file's changes.
4. `guard_bash.py:413-419,454-455`: interpreters that read code from stdin are not checked.
   `echo '<code>' | python3` runs inline code, and `echo 'drop database x' | uv run just psql`
   runs destructive SQL.

**Medium**

5. `guard_paths.py:49,53` + `guard_bash.py:17` (and the other guards): hooks run with the system
   `python3`. `except A, B:` is only valid syntax from Python 3.14 on, and the import sits outside
   the `try`. With `python3` older than 3.14, every guard exits 1, which Claude Code treats as a
   non-blocking error, so the hooks fail open. Not run on an older interpreter; this follows
   from the language rules.
6. `guard_bash.py:19,33`: the shell and wrapper lists are incomplete. `setsid git stash`,
   `ionice …`, `flock /tmp/l …`, `ksh -c '…'` and `uvx python -c …` run the inner command
   unchecked.
7. `guard_bash.py:457-461`: Docker variants pass: `docker volume remove x`,
   `docker compose down --volumes=true`, and `docker-compose down -v` (the tester's GAP).

**Low**

8. `guard_bash.py:250`: `rm -rf /abs/path/build` outside the project is allowed, because only the
   basename is checked (web-rh has the same behavior).
9. `git config alias.x stash` and `git config core.hooksPath /dev/null` are allowed. That makes
   a two-step bypass of the git rules and `--no-verify`.
10. `sync.py:93,116`: obsolete-file deletion keys on the marker *string* anywhere in the file, so
    a hand-written skill that quotes it would be deleted. Writes also follow an existing symlink
    out of the root. Uncertain impact.
11. The plan's first Acceptance criterion is truncated or garbled ("`git stash`,
    `docker compose down -v`, runs.").

Documented residuals (security.md says arbitrary programs are not analyzed), so not counted:
`cp/sed/grep/awk` reading `.env` or writing protected paths, and `dropdb`.

**Tester GAPs: all four confirmed** (`python3 -Sc`, `git clean --force`,
`git add -Av|./`, `docker-compose down -v`).

### Round 2 — re-review after the repair (2026-10-03, diff `ee162d3..6609ede`)

Round 1 above is historical. Hooks were exercised with crafted payloads from a scratchpad
script (`uv run python probe.py`, payloads on stdin, `CLAUDE_PROJECT_DIR` set to the repo or
to a disposable fixture repo in the scratchpad). Bash semantics were confirmed in that fixture
with harmless `touch`/`echo` payloads only.

#### Checklist — PASSED (applicable items)

- [x] `uv run just plans-scope … --base ee162d3`: "Every change is inside the plan" (34
      declared, 50 changed). No hot files touched.
- [x] `uv run just check`: exit 0 (API 238 passed, 3 skipped; harness 545 passed, 0 xfail;
      `harness-check` 24 adapters up to date; plans-lint OK; import-linter 7 kept).
- [x] Integration / domain / CQRS / contracts / Money / migrations / routes / wiring: N/A (no
      `apps/api` change).
- [x] No secrets in the diff.
- [x] Deviations honest: every claim of Deviation 8 reproduced (see below). Medium 5 also
      confirmed on a real older interpreter: the five hooks compile on CPython 3.12.13 and
      `guard_bash.py` blocks `git stash` with exit 2 there (3.10/3.11 not available here).
- [x] Docs updated (`security.md` "Shell subset" describes the new checks).
- [x] PR body: N/A (no PR yet).

#### Round 1 findings — all 11 resolved

Each original example now exits 2 from the live hook: `>&apps/api/openapi.json` (1);
`cd /tmp | echo x > uv.lock`, `cd apps | rm notes.txt` (2); `git reset --har`, `switch -f`,
`switch --force`, `push -uf`, `push --forc`, `branch -d -f`, `branch --delete --force`,
`commit --al`, `add --al`, `restore --staged --work`, `checkout justfile`, `checkout -B`,
`commit -n`, `commit --no-verif` (3); `echo … | uv run just psql`, `… | python3`,
`python3 < f`, `… | sh -c 'python3'` (4); `setsid`/`ionice`/`flock` wrappers, `ksh -c`,
`uvx python -c` (6); `docker volume remove`, `down --volumes=true`, `docker-compose down -v`
(7); `rm -rf /tmp/abs/build` (8); `git config alias.x stash`, `core.hooksPath` (9, reads
still allowed). Finding 10 is covered by the new sync tests; 11 is fixed in the plan text.

#### New findings (bypasses still open; all exit 0 from the live `guard_bash.py`)

None of these was opened by the repair: they exist at `3b4f9df` too and round 1 missed them.
They still defeat rules the plan lists as enforced, so the status stays `review`.

**High**

1. `guard_bash.py:600-606` (`_check_segment`): shell reserved words and some builtins are taken
   as the program, so the real command after them is never inspected.
   `if git stash; then true; fi`, `! git stash`, `while git stash; do break; done`,
   `for x in a; do git reset --hard; done`, `true; then git stash` (parsed as program `then`),
   `coproc git stash`, `builtin eval 'git stash'`, `trap 'git stash' EXIT` all exit 0.
   Confirmed in bash: `if touch m; then true; fi` and `! touch m` both ran `touch`. Failure
   scenario: any destructive rule (stash, reset --hard, db-reset, docker down -v) is bypassed
   by prefixing `!` or wrapping it in `if …; then …; fi`. Reserved words (`if then elif else
   fi do done while until for case esac select ! coproc function time`) and string-running
   builtins (`builtin`, `trap`, `.`/`source` aside) need to be treated like wrappers or
   unwrapped and checked.
2. `guard_bash.py:603,606`: the `VAR=` prefix check runs on `os.path.basename(segment[0])`, so
   an assignment whose value contains `/` hides itself. `X=/a/echo git stash` is seen as
   program `echo` with harmless arguments; bash treats `X=/a/echo` as an environment
   assignment and runs `git stash`. Confirmed in bash: `X=/a/echo touch m` created `m`.
   Failure scenario: every rule is bypassed with any `NAME=…/<harmless-program>` prefix.
3. `guard_bash.py:619-626`: only `cd` moves the tracked cwd; `pushd` (and `popd`) are
   ignored, so the guard resolves later relative paths against the wrong directory — the same
   class as round-1 finding 2. `pushd apps; echo x > api/openapi.json` is inspected as
   `<root>/api/openapi.json`; `pushd .git; echo x > config` as `<root>/config`. Confirmed in
   the fixture: bash wrote `apps/api/openapi.json`. Failure scenario: writing `.git/config`
   (aliases, `core.hooksPath`) or any protected file, and the untracked-`rm` check is
   desynchronised the same way.

**Medium**

4. `guard_bash.py:626`: `cd` stores `os.path.realpath(target)`, but bash's `cd` is logical
   (follows `..` lexically from `$PWD`). After `cd link; cd ..` (or `&&`), where `link` points
   to a directory elsewhere, the guard checks paths under the link target's parent while bash
   is back in the original directory. `cd link; cd ..; echo x > uv.lock` exits 0; confirmed in
   the fixture (bash wrote the fixture's `uv.lock`). Needs an existing symlink to a directory
   (`ln -s` is not checked).
5. `guard_bash.py:606`: `export`/`declare -x`/`typeset -x` set the environment for the rest of
   the line, which the `VAR=` prefix rule exists to prevent.
   `export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.x GIT_CONFIG_VALUE_0=stash; git x` exits 0
   and makes `git x` run `git stash` (equally `core.hooksPath` before `git commit`), bypassing
   the round-1 finding 9 repair. Not executed (git documents `GIT_CONFIG_COUNT/KEY_n/VALUE_n`).
6. `guard_bash.py:603,633`: git's dashed helpers bypass `_check_git` because only basename
   `git` is checked. `/usr/lib/git-core/git-stash` and `/usr/lib/git-core/git-reset --hard`
   exit 0; both binaries exist on this machine. Not executed.
7. `guard_bash.py:408-427` (checkout): `git checkout --pathspec-from-file=<f>` (and the
   abbreviation `--pathspec-f=`) has no positional, so none of the path conditions fire; it
   discards the worktree changes of every path listed in `<f>`. Incomplete repair of round-1
   finding 3. Not executed.
8. `guard_bash.py:658-660`: SQL is only inspected when `psql` is a separate argument. The
   repo's own `just psql` recipe shows the pattern that passes:
   `docker compose -f infra/docker/compose.yaml exec postgres sh -c 'psql -U u -d d -c "drop database x"'`
   exits 0. `docker … exec <svc> sh -c '<cmd>'` runs any command in the container unchecked.
   Not executed. (`dropdb` remains the documented residual from round 1.)

**Low**

9. `guard_bash.py:458-460` (git config): the denylist covers `alias.*` and `core.hooksPath`
   only. `git config include.path /tmp/x.cfg` (the included file can define aliases and
   `hooksPath`) and `git config clean.requireForce false` (then `git clean -d` deletes
   untracked files without `-f`) exit 0. Not executed.
10. `guard_bash.py:428-433`: `git switch -C <existing> <rev>` and `git branch -f <existing>
    <rev>` reset an existing branch (equivalent to the now-blocked `checkout -B`);
    `git push --mirror` force-updates every remote ref. All exit 0. Uncertain whether the plan
    intends these (they are not in its git list, but `checkout -B` was added on the same
    grounds).
11. `guard_bash.py:434-439`: `git add ':!x'` (exclude-only pathspec, meaning "everything but
    x") is not treated as broad. Uncertain impact (staging only).

Documented residuals unchanged (arbitrary programs: `cp`, `sed -i`, `unlink`, `ln`, `dropdb`,
scripts written to a file then run).

### Round 3 — re-review after the second repair (2026-10-03, diff `9aeef1e..b94975e`, base `ee162d3`)

Rounds 1 and 2 above are historical. Scope of this round (main session, under the user's batch
mandate): confirm every round-2 finding is resolved and nothing regressed; this is the last
repair round for the guard denylist (`docs/harness/security.md` declares it best-effort), so new
bypasses go to `plans/findings/` unless they are regressions or break a must-pass allow. Probed
with a scratchpad script (payload on stdin, `CLAUDE_PROJECT_DIR` = repo or a scratchpad fixture
repo); bash semantics confirmed in that fixture with harmless commands only.

#### Checklist — PASSED (applicable items)

- [x] `uv run just plans-scope … --base ee162d3`: "Every change is inside the plan" (34
      declared, 50 changed). No hot files touched.
- [x] `uv run just check`: exit 0 (ruff clean; import-linter 7 kept; plans OK; `harness-check`
      24 adapters up to date; API 238 passed, 3 skipped; harness 655 passed).
- [x] Integration / domain / CQRS / contracts / Money / migrations / routes / wiring: N/A (no
      `apps/api` change).
- [x] No secrets in the diff.
- [x] Deviations honest: Deviation 9 spot-checked item by item against
      `.claude/hooks/guard_bash.py` (`RESERVED_WORDS`, `HIDDEN_RUNNERS`, `ENV_SETTERS`,
      `_check_git_config`, `_physical`, docker `sh -c` recursion at `guard_bash.py:815-819`) and
      `guard_paths.py` (physical `realpath` candidate). Residuals (`CDPATH`, `git -C`) documented.
- [x] Docs updated: `docs/harness/security.md` states the best-effort stance and lists the
      rejected constructs.
- [x] PR body: N/A (no PR yet).

#### Round 2 findings — all 11 resolved

Each original example now exits 2 from the live hook: `if …; then …; fi`, `! git stash`,
`while`, `for`, `true; then git stash`, `coproc`, `builtin eval`, `trap` (High 1);
`X=/a/echo git stash` (High 2); `pushd apps; …`, `pushd .git; …`, `popd` (High 3);
`export GIT_CONFIG_COUNT=…`, `declare -x A=1` (Medium 5); `/usr/lib/git-core/git-stash`,
`…/git-reset --hard` (Medium 6); `checkout --pathspec-from-file=f`, `--pathspec-f=f`
(Medium 7); `docker compose … exec postgres sh -c 'psql … "drop database x"'`, also with
`bash -c` and `env sh -c` (Medium 8); `git config include.path …`,
`clean.requireForce false`, `--file=`/`--fil=`, `set alias.x` (Low 9); `switch -C`,
`branch -f`, `push --mirror`, `push --prune` (Low 10); `git add ':!x'`, `':(exclude)x'`
(Low 11). Medium 4 in the fixture: `cd link; cd ..; echo x > uv.lock` exits 2, and
`cd link; cd ..; echo x > notes2.txt` exits 0 (logical cd, no false block).

#### Regressions — none

36 must-pass/everyday commands exit 0, including `git status|diff|log`, `git stash list`,
`git add <path>`, `git commit -m`, `git push origin feat/x`, `--force-with-lease`, `push -u`,
`git switch -c|main`, `git branch -d`, `git config user.name x`, `--get alias.x`, `--list`,
`export -p`, `uv run just check|test|up|db-migrate --test|db-reset --test|plans-scope`,
`uv run just psql -c 'select 1'`, `cd apps/api && uv run pytest`, `echo hi > /tmp/x.txt`,
`docker compose … ps`, docker `exec postgres psql … 'select 1'` and `sh -c 'psql … select 1'`,
`rm -rf .pytest_cache`, `python3 scripts/bootstrap.py`, `just check 2>&1 | tail -5`.
Behavior change noted, not a regression: `docker compose … exec <svc> bash` (bare interactive
shell) is now blocked; no plan allow depends on it.

#### New findings — recorded outside the plan, not blocking

New bypasses (none a regression, none breaks an allow) are in
`plans/findings/platform-guard-bash-round3-bypasses.md` (status open): `set -k` turns later
`NAME=value` arguments into environment (confirmed in bash: `git x GIT_CONFIG_*` ran an alias),
and git plumbing that discards work (`checkout-index -f -a`, `read-tree -u --reset`, confirmed;
`rm -f`, `worktree remove --force`, `update-ref -d`, `reflog expire`, not executed).

Result: all round-2 items resolved, no regression → `status: verify`.

## Verification

Verifier, 2026-10-03, branch `feat/platform-harness`. Live session with the
`.claude/settings.json` hooks loaded. No `apps/api` change, so no migration or integration run
applies.

- `uv run just check`: exit 0. `Contracts: 7 kept, 0 broken.`, `plans OK (6 plans, 1 findings)`,
  `harness-check - 24 adapters up to date`, API `238 passed, 3 skipped`, harness `655 passed`.
- Live guard probes (each refused before running):
  - `git stash` -> `Command blocked: git stash changes shared work. Commit on a branch instead.`
  - `docker compose -f infra/docker/compose.yaml down -v` -> `Command blocked: never delete
    volumes/data. Use uv run just down (keeps data).`
  - `rm -rf apps` -> `Command blocked: recursive delete outside disposable directories (...).
    Ask the user.`
  - Read and Edit of `apps/api/.env` -> refused by the permission deny rules ("denied by your
    permission settings" / "covered by a Read deny rule"). The deny rule fired first, so the
    `guard_read` hook was not observed as a separate layer.
  - Edit `uv.lock` -> `Edit blocked: uv.lock is maintained by uv. Use uv add, uv remove or uv lock.`
  - Edit `.claude/agents/tester.md` -> `Edit blocked: Generated adapter. Edit docs/harness/roles
    or scripts/harness/adapters.py and run uv run just harness-sync.`
  - Also refused: `$VAR` expansion (`dynamic expansion is not supported`), `python -c`
    (`inline code is not inspectable`), heredoc (`heredoc/background is not supported`).
- Formatter: Write of `scripts/harness/zz_fmt_probe.py` (`import os,sys`, a badly spaced dict) was
  rewritten by the live PostToolUse hook to `x = {"a": 1, "b": 2}` (unused import removed). The
  probe file (mine) was then removed; `git status --short` was empty.
- Drift: on a scratchpad copy of the generated trees, `harness.sync --check` returned 0; after
  appending a line to the copy of `.claude/agents/tester.md` it returned 1 with
  `~ .claude/agents/tester.md`. (The real file cannot be hand-edited: the guard blocks it.)
- TOML: `pytest scripts/harness -k toml` passes (9 tests). Files present: 8 `.codex/agents/*.toml`,
  9 `.claude/skills/*`, 4 `.claude/agents/*`.
- `justfile` has `harness-sync`, `harness-check`, `test-harness`; `ci.yml` runs `harness-check`,
  `pytest --ignore=scripts/harness` and `test-harness`.

Acceptance criteria: 1, 2, 3, 5 pass; 4 and 6 only partly (below).

NOT VERIFIED: a fresh session's skill listing as shown by Claude Code (files exist; this run is
itself a registered subagent, but the slash-command list was not enumerated); Codex listing its
profiles; CI green (nothing pushed); hooks on an interpreter older than 3.14 beyond the recorded
`ast` and CPython 3.12 checks; `guard_read` as a layer separate from the permission deny rule.
