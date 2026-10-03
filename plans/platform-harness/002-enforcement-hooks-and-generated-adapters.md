---
status: draft
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

## Test coverage

## Review findings

## Verification
