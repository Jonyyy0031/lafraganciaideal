# Commit conventions

Enforced by `scripts/commits.py` (the `commit-msg` hook installed by `just bootstrap`, and the
CI `commits` job over every commit of a PR) where marked ⚙. Check a range by hand with
`uv run scripts/commits.py --range a..b`.

## Format

```
<type>(<scope>): <subject>

<body — optional, wrapped at ~90 columns>
```

- ⚙ **type**: `feat` · `fix` · `refactor` · `perf` · `test` · `docs` · `style` · `build` ·
  `ci` · `chore` · `revert`. Append `!` for breaking changes (`feat(orders)!: …`).
- ⚙ **scope**: required. A module from [docs/modules.json](../../modules.json) (`catalog`,
  `orders`, `payments`, …) or a cross-cutting scope: `api`, `web`, `infra`, `ci`, `deps`,
  `docs`, `repo`, `harness`. The module list is read from the registry — a new module gets its
  scope automatically.
- **subject**: English, lowercase start, imperative or descriptive, no trailing period, ≤ 72
  chars recommended (⚙ 100 hard limit for the header). Says WHAT changed in business terms:
  `feat(catalog): admin can create brands with a unique slug`, not `feat(catalog): changes`.
- ⚙ **No AI attribution**: no `Co-Authored-By:` trailers, no "Generated with …" lines. The
  author of record is the human who owns the repo.
- ⚙ Generic subjects are rejected: `changes`, `wip`, `fix`, `update`, `misc`, `stuff`, `tests`…

## One commit per pipeline phase

Each phase closes with one atomic commit that includes the plan file (its status and evidence
section moved in the same edit). The plan number goes in the subject, so `git log` tells the
story:

| Phase closes              | Commit                                                                         |
| ------------------------- | ------------------------------------------------------------------------------ |
| Plan written              | `docs(<scope>): plan 003 for <topic> (draft)`                                  |
| Plan approved by the user | (no commit needed; the status change rides with the next one)                  |
| Implementation            | `feat(<scope>): <what was built>` — body: `Plan 003 to testing.`               |
| Tests                     | `test(<scope>): <what they cover> (plan 003)` — body: counts per layer, GAPs   |
| Review findings fixed     | `fix(<scope>): review findings (plan 003)` — body: findings by severity        |
| Verification              | `docs(<scope>): verification of plan 003 (PASS)` or `(FAIL)`                   |
| User accepts              | `docs(<scope>): plan 003 done`                                                 |
| Fast-lane fix             | `fix(<scope>): <symptom fixed>` — body: root cause + regression test           |
| Series closed             | `docs(<scope>): close the <initiative> series` (README "Delivered")            |

A series of plans implemented together may share phase commits
(`test(catalog): … (plans 002-004)`), never mix phases in one commit.

### Body

Explain the **why** and anything a reviewer can't see in the diff. For review fixes, list
findings by severity as bullets (`- High: …`, `- Medium: …`, `- Low: …`). End with the status
transition when the commit moves a plan (`Plan 003 to verify.`).

## Who commits, and how

- **Subagents never commit.** The main session commits at the end of each phase, and only if
  the user asked for commits or authorized them for this plan/batch. Never push without being
  asked. Repairing a plan grants no new commit/push permission.
- **Stage explicit paths only**: `git add <path> <path>` with the files of THIS phase. Never
  `git add -A`, `git add .`, `git add -u` or `git commit -a` (blocked): the worktree may
  contain the user's or another agent's uncommitted work, and bundling it is a violation.
- Before committing: `git status --short` and `git diff --cached --stat` — the staged set must
  equal the phase's files. Pre-existing user changes stay unstaged, untouched.
- ⚙ Hooks run on every commit (pre-commit: ruff, whitespace, YAML/JSON/TOML checks,
  `detect-private-key`; `commit-msg`: `scripts/commits.py`). Never `--no-verify` (blocked):
  fix what the hook reports.
- One cohesive change per commit. If you notice something unrelated, it becomes a finding
  (`plans/findings/`), not an extra hunk.

## Branches and pull requests

`feat/<initiative>` for a plan series (`feat/catalog-perfumes`), `fix/<module>-<slug>` for
fast-lane fixes, `chore/<slug>` for tooling. Never commit directly to `main`; work reaches
`main` through a pull request whose title follows this convention and whose body follows
[pull-requests.md](pull-requests.md) (template `.github/pull_request_template.md`).

Repair iterations preserve dated phase evidence; only the main session commits, with existing
user authorization.
