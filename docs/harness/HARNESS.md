# La Fragancia Ideal Harness — Vision & Map

> All non-trivial work flows through a pipeline of versioned artifacts
> (plan → implementation → tests → review → verification), executable by any AI agent or
> human. Each phase has defined inputs, outputs and exit criteria. The state lives in the
> plan file; the evidence lives in the plan file; the guardrails are enforced by tools, not
> by good intentions.

Read this first, then [workflow.md](workflow.md). Architecture and code conventions live in
[docs/architecture.md](../architecture.md), [apps/api/README.md](../../apps/api/README.md),
[docs/recipes/](../recipes/) and the ADRs in [docs/adr/](../adr/); this directory only governs
**process**.

| Doc                                | Purpose                                                         |
| ---------------------------------- | --------------------------------------------------------------- |
| `workflow.md`                      | Pipeline, statuses, routing, fast lane, deviations, model tiers |
| `security.md`                      | Trust boundary, what each control really proves                 |
| `roles/architect.md`               | Writes plans. Read-only on code.                                |
| `roles/implementer.md`             | Executes an approved plan literally.                            |
| `roles/tester.md`                  | Layered tests, "nothing invented".                              |
| `roles/reviewer.md`                | Conventions checklist + bug hunt against the plan.              |
| `roles/verifier.md`                | Drives the real running app.                                    |
| `conventions/plans.md`             | Initiatives, plan semantics/format, findings, lint rules.       |
| `conventions/commits.md`           | Commit format, one commit per phase, staging, no attribution.   |
| `conventions/pull-requests.md`     | The six questions every PR body answers.                        |
| `conventions/backend.md`           | API checklist (points to architecture, API README, recipes).    |
| `conventions/frontend.md`          | Placeholder until phase 3 (Angular).                            |
| `conventions/testing.md`           | Testing contract: layers, nothing invented, execution budget.   |
| [`docs/modules.json`](../modules.json) | Module registry (machine-readable; also the commit scopes). |

## Architecture of the harness: agnostic core + generated adapters

Everything that defines the process is plain markdown in this directory plus `plans/`.
Tool-specific layers are **generated** from it — never hand-edited:

```
docs/harness/roles/*.md  ──┐
scripts/harness/adapters.py (names, models, per-role glue)
                           ├──▶ uv run just harness-sync ──▶ .claude/agents/*.md        (Claude subagents)
                           │                              ─▶ .claude/skills/*/SKILL.md   (Claude skills)
                           │                              ─▶ .codex/agents/*.toml       (Codex profiles)
                           └──────────────────────────────▶ .agents/skills/*/SKILL.md   (Codex skills)
```

The generator is `scripts/harness/sync.py`. `just check` runs `just harness-check`
(`sync.py --check`), which fails if any adapter drifted from its source (missing `+`,
different `~`, obsolete `-`). To change an adapter, edit the role doc or
`scripts/harness/adapters.py` and run `uv run just harness-sync`. Hand-written recipe skills —
`new-module`, `new-use-case`, `db-change` — are not generated; they load
[docs/recipes/](../recipes/) and are conventions detail, not roles. `.codex/config.toml`
(subagent limits) is hand-written too. The generated paths are also write-protected by the
`guard_files` hook, so a Claude session cannot hand-edit them by accident.

There is no runtime orchestrator. **Orchestration is a protocol, not a process**: any
session, any tool, any model, or a human reads a plan's `status:` and the routing table in
`workflow.md` and knows what happens next. `uv run just plans-status` computes the overview.

## Mechanical enforcement (what is NOT left to good intentions)

| Guarantee                                                                   | Enforced by                                       |
| --------------------------------------------------------------------------- | ------------------------------------------------- |
| Layer and module boundaries                                                 | `just arch` (import-linter, `apps/api/.importlinter`) |
| Plans are well-formed, statuses coherent                                    | `just plans-lint` (in `just check`)               |
| Diff stays inside the plan's file list                                      | `just plans-scope <plan>` (reviewer runs it)      |
| Adapters match their role docs                                              | `just harness-check` (in `just check` and CI)     |
| Known destructive shell/git/db/docker forms (registered Claude hook only)   | `.claude/hooks/guard_bash.py` (+ its tests)       |
| Protected paths through registered edit hooks (not arbitrary programs)      | `.claude/hooks/guard_files.py`                    |
| Secrets not read through the registered Read hook                           | `.claude/hooks/guard_read.py`                     |
| Formatting of edited Python files                                           | `.claude/hooks/format_file.py` (ruff), pre-commit |
| Commit format, registry scopes, no AI attribution, no generic subjects      | `scripts/commits.py` (`commit-msg` hook + CI `commits` job) |
| Explicit staging only (no `git add -A` / `.` / `-u` / `commit -a`)          | `.claude/hooks/guard_bash.py`                     |
| Hook programs keep blocking what they must block                            | `just test-harness` (in `just check` and CI)      |

The hooks are registered in `.claude/settings.json` (with its allow/ask/deny permissions) and
run only when Claude Code loads the project settings. Codex does not run Claude hooks. That is
why its generated profiles carry the destructive rules as text. `just test-harness` tests guard behavior with inert payloads; it does not
register hooks in Codex or prove live enforcement. Host sandbox/permissions remain essential.
See [security.md](security.md).

## Module registry

[`docs/modules.json`](../modules.json) is the source of truth; a plan's `module:` must be one
of its names, and every name is also a commit scope.

- **Reference**: `catalog` — the canonical pattern. New modules imitate it by name
  (`apps/api/src/fragancia_api/modules/catalog/...`, recipe
  [new-module.md](../recipes/new-module.md)).
- **Active**: `platform` (cross-cutting work), `web`, `catalog`, `identity`.
- **Planned**: `inventory`, `orders`, `payments`, `notifications`, `shipping` — a
  plan may create them; nothing may assume they exist until that plan is `done`.

## Shared hot files (append-only)

Edited by many plans; merge-conflict magnets. Append inside your module's entry only; never
reorder, regroup or "clean up" surrounding code. `plans-scope` allows them even if the plan
does not list them, but the reviewer still checks the change is append-only.

- `apps/api/src/fragancia_api/container.py` (the `MODULES` list)
- `docs/modules.json` (a new module here also becomes a commit scope automatically)
- `apps/api/.importlinter` (the module's entry in the `module-layers` containers; narrowly
  scoped `ignore_imports` for an approved anti-corruption adapter)

## Security rules (all roles, no exceptions)

- Never read, copy or quote `.env*` contents (except `.env.example`) into code, plans, tests,
  commits or chat. Refer to configuration by variable name (`DATABASE_URL`, `S3_SECRET_KEY`).
- Never commit secrets, keys, dumps or real personal data (customer names, addresses, phone
  numbers, emails, payment data) — fixtures use synthetic data.
- Tests that touch a database use the **test database** (`DATABASE_URL_TEST`, name ends in
  `_test`, i.e. `fragancia_test`); `Settings` refuses a test URL whose database name does not
  end in `_test`.

## Destructive actions (all roles, no exceptions)

**Nothing outside your plan's file list gets deleted, overwritten, reverted or "cleaned up".
Ever.** If an action is hard to reverse and the user did not name the thing explicitly,
don't do it — ask (attended) or set `status: blocked` (subagent).

These rules come from real incidents in a previous project: subagents "tidying up" deleted
untracked user files that git could not restore, reverted a user's uncommitted edit to an
unrelated file, and `git stash`-ed a worktree shared by six parallel implementers. The hooks
block the commands; the rules below explain the intent so you do not look for a way around
them.

**Files**

- Never delete a file you did not create in this session. Untracked (`??`) does **not** mean
  disposable. `rm` of untracked files, globs, `find -delete` and `git clean` are blocked.
  Recursive `rm` is only for disposable directories (`.venv`, `__pycache__`, `.pytest_cache`,
  `.ruff_cache`, `.mypy_cache`, `node_modules`, `dist`, `build`, `.angular`, `coverage`,
  `htmlcov`).
- Ignore untracked noise instead: `git status --short | grep -v '^??'`.
- Your temp files go to the session scratchpad (or `/tmp`), never the repo.
- Read a file before overwriting it if you did not write it.
- **Never `git stash`, `git checkout -- <file>`, `git restore <file>`** — not even on your own
  files, not even "to get a baseline". A dirty tree is someone's uncommitted work, possibly
  another agent's in the same worktree. (All blocked.) Also blocked: `git reset --hard`,
  `git clean -f`, `git branch -D`, force push or push to `main`, `--no-verify`.
- **Safe baseline for "was this failure pre-existing?"**, touching only files you modified:
  1. copy each of your modified files to the scratchpad;
  2. `git show HEAD:<path> > <path>` for those files only;
  3. run the relevant tests (`uv run pytest <path>::<test>`);
  4. copy your versions back and confirm with `git diff --stat` that your changes are intact.

**Database**

- Schema changes only via **new Alembic migrations** (`uv run just db-revision "msg"`, then
  review the generated file by hand; recipe [db-change.md](../recipes/db-change.md)). Never
  edit a migration that has reached `main`; never `alembic downgrade` the development
  database (only `-x test=true`).
- `just db-reset` is the only sanctioned rebuild of a local database (drop and recreate; then
  `just db-migrate`). The development database is reset only by the user, in an interactive
  terminal, typing its name. Codex runs no hooks, so that typed confirmation is the real
  guard. Agents may run `just db-reset --test` (it rebuilds `fragancia_test`).
- No `DELETE`/`UPDATE` whose `WHERE` you have not first proven with `SELECT COUNT(*)`; if the
  count disagrees with the plan, stop (`status: blocked`) — never adjust the plan to match.
  No `DROP`/`TRUNCATE` through `just psql`.
- A migration that drops or rewrites data needs the user's explicit approval in the plan.
- Seeded verification data is removed afterwards — only the rows you seeded.
- Docker: never `docker compose down -v`, `docker volume rm|prune`, `docker system prune`
  (they delete every local database). `just down` keeps the data.

**Production**

- Never run migrations, seeds or any write path against a non-local environment.
