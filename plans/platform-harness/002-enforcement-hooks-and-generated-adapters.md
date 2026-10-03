---
status: implementing
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
   - Files: `docs/harness/HARNESS.md` (modify), `docs/harness/workflow.md` (modify), `AGENTS.md` (modify), `CLAUDE.md` (modify)
   - Do: adapter architecture diagram ("agnostic core + generated adapters"), how to change a
     role (edit the doc or `adapters.py`, run `just harness-sync`).

## Acceptance criteria

- [ ] In a live Claude Code session in this repo: `git stash`, `docker compose down -v`,
      runs.
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

Not verified here (manual, e2e layer): a fresh Claude Code session listing the skills and
subagents; Codex listing the profiles.

## Test coverage

Baseline `uv run just check`: green (348 harness tests). Tests live in
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

GAP summary (product code not touched, strict xfail, 8 xfail cases): combined python flags
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

## Verification
