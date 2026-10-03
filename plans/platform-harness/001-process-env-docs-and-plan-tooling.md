---
status: testing
module: platform
min_implementer: mid
depends_on: []
---

# 001 — Process: one env file, harness docs, full plan format and plan tooling

## Context

### Env file and test database (README decision 1)

Phase 1 introduced two divergences from web-rh (decision 1 of the README):

- A root `.env.example` → `.env` holds compose variables (ports, credentials), while
  `apps/api/.env.example` repeats the same credentials in `DATABASE_URL`. web-rh has no root
  env file: `~/codes/web-rh/infra/docker/docker-compose.yml` uses inline defaults
  (`${POSTGRES_USER:-rrhh}`) and only apps have `.env.example` files.
- `infra/docker/postgres/01-create-test-db.sh` reads `POSTGRES_TEST_DB`; web-rh uses
  `01-create-test-db.sql` with `CREATE DATABASE rrhh_test;` and its bootstrap ensures the
  database through `docker compose exec postgres psql` (`~/codes/web-rh/scripts/bootstrap-database.mjs`).

Consumers of the root `.env` today: `justfile` (`set dotenv-load`, `env_file`), `scripts/bootstrap.py`
(`read_env` for ports, credentials, bucket), `.github/workflows/ci.yml` (`infra` job greps
`= .env already exists`, smoke uses `--env-file .env`), `README.md`, `AGENTS.md`.

Approach: compose keeps its defaults inline (already the case) and drops `--env-file`; ports
are overridden by exporting variables in the shell, as in web-rh. The test database is created
inside the container (no host credentials). S3 settings move to `apps/api/.env.example`, which
the API will need for product photos anyway; bootstrap reads the bucket settings from there.

### Process docs and plan format (README decisions 2, 4, 5)

Today the process is a few paragraphs in `AGENTS.md` ("The rules that govern everything") and
simplified templates (`plans/_TEMPLATE.md`, `plans/_INITIATIVE.md`, `plans/findings/README.md`).
Plans use only `draft → approved → implementing → done` and have no `min_implementer`,
`## Test coverage` or `## Review findings` sections.

web-rh's process lives in `~/codes/web-rh/docs/harness/`: `HARNESS.md` (vision, mechanical
enforcement table, module registry, destructive-action rules with their origin incidents,
scratchpad baseline, database rules), `workflow.md` (entry points, statuses and who moves each,
routing table, dispatch policy, fast lane, deviation protocol, refactors, model tiers, repair
handoff, closing an initiative), `security.md`, `roles/{architect,implementer,tester,reviewer,verifier}.md`
and `conventions/{plans,commits,backend,frontend,testing}.md`, plus `plans/_TEMPLATE.md`,
`_INITIATIVE.md` and `_FINDING.md`.

This plan ports that content, translated to this stack (uv, just, FastAPI, SQLAlchemy Core,
Alembic, import-linter, pytest) and to English (README decision 5). It keeps what already
exists here: `docs/modules.json` stays where it is (registry), `plans/findings/` stays the
findings directory, and `docs/recipes/` stay the recipes. Names in the routing table use the
English skills of decision 4.

### Plan tooling

The steps above define the plan format, statuses, tiers and findings. web-rh enforces it with
`~/codes/web-rh/scripts/plans/{lib,lint,status,scope}.mjs` (+ `plans.test.mjs`,
`scope.test.mjs`), wired into `pnpm check` as `plans:lint`, and used by the reviewer as
`plans:scope`. This repo's tooling is Python (`scripts/`, run with `uv run`, tested by pytest
through `testpaths = ["scripts", …]` in `pyproject.toml`).

Port, rule for rule (summary of web-rh's behavior):

- **lib**: parse frontmatter (`^---\n…\n---`, YAML) and `## ` sections (a section is empty if
  it only holds HTML comments/whitespace); list plans (`plans/*/NNN-*.md`, excluding
  `findings/`, `_*` templates and `README.md`); initiatives (first-level dirs except
  `findings`); owner module = longest registry name equal to the initiative or prefixing it as
  `name-`; resolve `depends_on` refs (`"002"` same initiative, `"x-y/002"` another; zero-pad);
  declared files = backticked tokens containing `/` or `.` and no space on `Files:` lines
  inside `## Steps`.
- **lint** (exit 1 with `where: message` lines): initiative name kebab-case, prefix is a module,
  has README; no loose plans, one level deep, `NNN-slug.md`, unique numbers; valid YAML and
  frontmatter present; `status` ∈ statuses, `module` ∈ registry and equal to the owner,
  `min_implementer` ∈ small|mid|high, `depends_on` list, `superseded_by` only when
  superseded; dependencies exist, no self-dependency, no cycles (DFS, even in draft); from
  implementing on every dependency is done; ten sections present and in order; evidence by
  status (Deviations from testing, Test coverage from review, Review findings from verify,
  Verification at done); Out of scope non-empty unless draft/superseded. Findings: kebab slug,
  YAML, status ∈ open|deferred|planned|resolved|discarded, module, `found` date, `plan`
  resolvable when planned/resolved.
- **status** (`[initiative] [--all]`): priority blocked 0, draft 1, verify 2, review 3,
  testing 4, implementing 5, approved 6, done 8, superseded 9; approved/implementing with
  unfinished dependencies bumped to 0; table (plan, status, tier, title ≤50, next action);
  done/superseded hidden unless `--all`; open findings listed when unfiltered.
- **scope** (`<plan> [--base main]`): changed set = committed diff from `merge-base base HEAD`
  (renames: both paths) ∪ staged ∪ worktree ∪ untracked; allowed = declared exactly, under a
  declared `dir/`, the plan file, its initiative README, `plans/findings/*`, hot files
  (`apps/api/src/fragancia_api/container.py`, `docs/modules.json`, `apps/api/.importlinter`),
  `apps/api/migrations/versions/*` when a `…/infrastructure/tables.py` is declared,
  `apps/api/openapi.json` when a `contracts.py` or `http/router.py` is declared, `uv.lock` when
  a `pyproject.toml` is declared. Output: counts, hot files to review as append-only, declared
  but unchanged, out-of-scope list (exit 1); git/usage errors exit 2, never an empty success.

## Out of scope

- Production configuration (platform-infraestructura/002); using S3 from the API (only the
  settings move); deleting the user's existing root `.env` (it becomes unused).
- Guard hooks and generated adapters (plan 002). Docs mention them as "added by plan 002"
  until it is done.
- Checking that hot-file edits are append-only (manual reviewer check, as in web-rh).
- Frontend conventions beyond a short placeholder (phase 3).

## Dependencies

None

## Steps

1. **Compose and test database**
   - Files: `infra/docker/compose.yaml` (modify), `infra/docker/postgres/01-create-test-db.sh` (delete),
     `infra/docker/postgres/01-create-test-db.sql` (create), `.env.example` (delete)
   - Do: remove `POSTGRES_TEST_DB` from compose; the header explains overrides via exported
     variables. The `.sql` runs `CREATE DATABASE fragancia_test;` on a new volume.
   - Observable result: `docker compose -f infra/docker/compose.yaml config` works with no env file.

2. **API env file carries the S3 settings**
   - Files: `apps/api/.env.example` (modify)
   - Do: add `S3_ENDPOINT_URL=http://127.0.0.1:9100`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`,
     `S3_BUCKET=fragancia-media` (development values matching the compose defaults).
   - Observable result: one file holds every app setting.

3. **Bootstrap and recipes without the root env**
   - Files: `scripts/bootstrap.py` (modify), `scripts/infra.py` (modify), `scripts/test_bootstrap.py` (modify),
     `justfile` (modify)
   - Do: bootstrap creates only `apps/api/.env`; ensures `fragancia_test` with
     `docker compose exec -T postgres psql` (same idea as web-rh `bootstrap-database.mjs`);
     reads S3 settings from `apps/api/.env`; prints ports from the compose defaults or the
     exported variables. `justfile` drops `set dotenv-load` / `env_file`; `psql` and `db-reset`
     use the container's own `POSTGRES_USER` / `POSTGRES_DB`. If a root `.env` exists,
     bootstrap prints that it is no longer used (never deletes it).
   - Observable result: `uv run just bootstrap` works on a clone without any root env file.

4. **CI and docs**
   - Files: `.github/workflows/ci.yml` (modify), `README.md` (modify), `AGENTS.md` (modify)
   - Do: the `infra` job no longer expects a root `.env`; smoke commands drop `--env-file`.
     Docs describe the single env file and port overrides.
   - Observable result: CI `infra` job green.

5. **Harness vision and workflow**
   - Files: `docs/harness/HARNESS.md` (create), `docs/harness/workflow.md` (create),
     `docs/harness/security.md` (create)
   - Do: port web-rh's content. Statuses: draft, approved, implementing, testing, review,
     verify, blocked, done, superseded; who moves each (user only for approved/done/superseded).
     Routing table with skills `plan`, `implement`, `write-tests`, `review`, `verify`, `fix`
     and subagents `implementer`, `tester`, `reviewer`, `verifier`; Codex profiles. Dispatch
     policy (ask before dispatching when the user is present). Fast lane criteria (≤3 files, no
     migration/contract/auth/money change). Deviation protocol, refactors with
     characterization tests, model tiers (small→haiku, mid→sonnet, high→opus; Codex profiles),
     repair handoff, closing an initiative. Mechanical enforcement table (import-linter,
     plans-lint/scope, hooks, harness-check, commit-msg). Destructive-action rules, scratchpad
     baseline, database rules (`just db-reset` dev only by the user; `SELECT COUNT(*)` before
     DELETE/UPDATE; never write to non-local environments). Hot files (append-only):
     `apps/api/src/fragancia_api/container.py`, `docs/modules.json`, `apps/api/.importlinter`.
   - Observable result: a reader knows what happens next for any plan status.

6. **Roles**
   - Files: `docs/harness/roles/architect.md` (create), `docs/harness/roles/implementer.md` (create),
     `docs/harness/roles/tester.md` (create), `docs/harness/roles/reviewer.md` (create),
     `docs/harness/roles/verifier.md` (create)
   - Do: inputs, process, outputs, exit criteria and forbidden actions per role, as in web-rh,
     with this repo's commands (`uv run just check`, `test-integration`, `plans-scope`) and
     checklist (layers via import-linter, Result + stable codes, `Money` in cents, UTC via
     `Clock`, new Alembic migration reviewed, declared access, `openapi.json` regenerated,
     composition root wiring). Tester markings in pytest: confirmed → test;
     `pytest.mark.skip(reason="NOT CONFIRMED: …")`; `pytest.mark.xfail(strict=True, reason="GAP: …")`.
   - Observable result: each role doc is self-sufficient for its phase.

7. **Conventions**
   - Files: `docs/harness/conventions/plans.md` (create), `docs/harness/conventions/commits.md` (create),
     `docs/harness/conventions/backend.md` (create), `docs/harness/conventions/testing.md` (create),
     `docs/harness/conventions/frontend.md` (create)
   - Do: plans (initiative naming, numbering, README sections, findings, required sections and
     the evidence each status requires); commits (English, one commit per phase with the plan
     file: `docs(x): plan 003 for … (draft)`, `feat(x): …` + body `Plan 003 to testing.`,
     `test(x): … (plan 003)`, `fix(x): review findings (plan 003)`,
     `docs(x): verification of plan 003 (PASS|FAIL)`, `docs(x): plan 003 done`; branches
     `feat/<initiative>`; PRs to main); backend (pointer to `apps/api/README.md` + checklist);
     testing (layers, nothing invented, coverage matrix, two full runs budget); frontend stub.
   - Observable result: CONTRIBUTING and AGENTS link here instead of repeating rules.

8. **Templates and the existing plans**
   - Files: `plans/_TEMPLATE.md` (modify), `plans/_INITIATIVE.md` (modify), `plans/_FINDING.md` (create),
     `plans/findings/README.md` (modify), `plans/platform-infraestructura/001-monorepo-entorno-local-y-ci.md` (modify),
     `plans/platform-api-foundation/001-api-platform.md` (modify),
     `plans/platform-api-foundation/002-catalog-brands-reference-module.md` (modify),
     `plans/platform-openapi/001-document-drift-check-and-scalar.md` (modify)
   - Do: template frontmatter `status`, `module`, `min_implementer`, `depends_on`,
     optional `superseded_by`; ten sections in order (Context, Out of scope, Dependencies,
     Steps, Acceptance criteria, Test layers required, Deviations, Test coverage, Review
     findings, Verification). Finding template with `status` (open, deferred, planned,
     resolved, discarded), `module`, `found`, `plan`. Existing done plans get
     `min_implementer` and the two new sections with "Not applicable: completed before the
     harness (2026-10-02); evidence is in Verification."
   - Observable result: every plan follows the new format.

9. **Entry points**
   - Files: `AGENTS.md` (modify), `CONTRIBUTING.md` (modify), `CLAUDE.md` (modify), `docs/modules.json` (modify),
     `scripts/commits.py` (modify), `scripts/test_commits.py` (modify)
   - Do: AGENTS.md becomes the web-rh shape: the three rules (plan-driven, architecture
     conventions, no destructive actions), subagent dispatch policy, "before writing code",
     repo map, commands, architecture rules, definition of done, forbidden actions (explain the
     risk and give the user the command instead). CLAUDE.md lists skills/subagents/hooks
     (marked as arriving with plan 002). `harness` becomes a cross-cutting commit scope.
   - Observable result: a new session knows the pipeline from AGENTS.md alone.

10. **Library**
   - Files: `scripts/plans/__init__.py` (create), `scripts/plans/lib.py` (create), `pyproject.toml` (modify), `uv.lock` (modify)
   - Do: the lib behavior above; `pyyaml` as a dev dependency.
   - Observable result: importable from the other scripts and tests.

11. **Lint and status**
   - Files: `scripts/plans/lint.py` (create), `scripts/plans/status.py` (create)
   - Do: the rules above; messages in English.
   - Observable result: `uv run just plans-lint` passes on the repo; `uv run just plans-status` prints the table.

12. **Scope**
   - Files: `scripts/plans/scope.py` (create)
   - Do: the algorithm above with NUL-delimited git output; reject bases starting with `-`.
   - Observable result: `uv run just plans-scope plans/platform-harness/001-process-env-docs-and-plan-tooling.md` reports this branch's changes.

13. **Tests**
   - Files: `scripts/plans/test_lint.py` (create), `scripts/plans/test_status.py` (create), `scripts/plans/test_scope.py` (create)
   - Do: fixtures in `tmp_path` for each lint rule (one failing case each), status ordering and
     bumping, scope in a temporary git repo (declared, dir prefix, hot files, migrations with
     tables, renames, untracked, out-of-scope exit 1, bad base exit 2).
   - Observable result: `uv run just test` green.

14. **Recipes and check**
   - Files: `justfile` (modify), `.github/workflows/ci.yml` (modify), `docs/harness/HARNESS.md` (modify), `AGENTS.md` (modify)
   - Do: recipes `plans-lint`, `plans-status *args`, `plans-scope plan *args`; `check` runs
     `plans-lint`.
   - Observable result: a malformed plan fails `just check` and CI.

15. **Pull request format**
   - Files: `.github/pull_request_template.md` (modify), `docs/harness/conventions/pull-requests.md` (create),
     `CONTRIBUTING.md` (modify)
   - Do: every PR answers, in this order: **What changed** (the outcome in 2–3 lines, readable
     without the code); **Why** (the problem or need, link to the plan and its decisions);
     **How** (approach, key design decisions, alternatives discarded, files worth reviewing
     first); **How it was verified** (commands with their real result, live checks, evidence or
     a link to the plan's Verification); **What's missing** (NOT VERIFIED items, known
     limitations, follow-ups, open findings — "Nothing" only if true); **Risks and rollout**
     (migrations, config/env changes, manual steps after merge, or "None"). Short checklist at
     the end (`just check`, `test-integration` when persistence changed, `plans-scope`,
     `openapi.json` regenerated, docs in English). The convention doc explains each section
     with a good and a bad example, and that PR bodies carry no AI attribution.
   - Observable result: a new PR opens with the template; the reviewer role's checklist
     includes "the PR body answers the six sections".

## Acceptance criteria

- [ ] With no root `.env`, `uv run just bootstrap` exits 0 twice; the second run reports
      `= apps/api/.env already exists` and `= fragancia_test already exists`.
- [ ] A fresh volume gets `fragancia_test` from the `.sql` init script.
- [ ] `POSTGRES_PORT=5544 uv run just up` publishes PostgreSQL on 5544 (then back to default).
- [ ] `uv run just psql -d fragancia_test -c 'select 1'` and `just db-reset --test` (aborted) work.
- [ ] `docs/harness/` contains HARNESS, workflow, security, 5 roles and 5 conventions, in
      English, with commands that exist in this repo.
- [ ] Every plan in `plans/` has the new frontmatter and the ten sections in order.
- [ ] `git commit -m "docs(harness): …"` is accepted by the commit hook.
- [ ] `uv run just plans-lint` passes on the repo and fails (exit 1, precise message) on a plan
      missing `## Out of scope` content in `approved`.
- [ ] `uv run just plans-status` lists drafts first and hides done plans unless `--all`.
- [ ] `uv run just plans-scope <plan>` flags a file outside the plan (exit 1) and passes when
      only declared/allowed files changed.
- [ ] `.github/pull_request_template.md` has the six sections (What changed, Why, How, How it
      was verified, What's missing, Risks and rollout); this initiative's PR uses it.
- [ ] `uv run just check` passes (including `plans-lint`); CI green.

## Test layers required

| Layer       | Applies | Focus                                                               |
| ----------- | ------- | ------------------------------------------------------------------- |
| tooling     | yes     | bootstrap helpers, `harness` commit scope, every lint rule, status, scope |
| integration | yes     | CI `infra` job: bootstrap twice on a clean runner + smoke            |
| domain      | no      |                                                                     |
| application | no      |                                                                     |
| http        | no      |                                                                     |

## Deviations

1. **`ensure_env_keys` added (step 3)** — said: bootstrap creates only `apps/api/.env`.
   Reality: an existing `apps/api/.env` lacked the new `S3_*` keys and bootstrap crashed
   (`KeyError: 'S3_ENDPOINT_URL'`). Done: `scripts/infra.py::ensure_env_keys` appends missing
   keys from `.env.example` without changing existing values; tested and idempotent.
2. **`declared_files` reads wrapped `Files:` lines and change-marked tokens (step 10)** — said:
   tokens on `Files:` lines containing `/` or `.` (web-rh's rule). Reality: our plans wrap
   long `Files:` lines and declare `justfile`; both were reported out of scope. Done: an entry
   continues until the next bullet/step/blank line, and a backticked token followed by
   `(create|modify|delete)` counts as a path.
3. **Plan recipes set `PYTHONPATH` inside `just`** — the guard hook (plan 002, already active)
   blocks `VAR=… cmd` in agent shells; recipes are the interface (`uv run just plans-lint`).
4. **Existing plan module fixed** — `platform-api-foundation/002` had `module: catalog`, but the
   initiative is owned by `platform` (lint rule 12); now `module: platform`.
5. **`platform-harness/002` moved back to `approved`** — it was set to `implementing` while 001
   was not done (lint rule 18). Its hooks part (independent of 001) was implemented in
   parallel by a subagent; see 002's Deviations.
6. **Docs written by a subagent from web-rh's sources** — judgment calls: web-rh's legal/payroll
   sensitive areas map to money math, payments/webhooks, stock reservation and order
   transitions; lowercase subjects/trailing periods are convention only (not checked by
   `scripts/commits.py`); the verifier gets `ADMIN_DEV_TOKEN` from the user, never by reading
   `apps/api/.env`. One example in `pull-requests.md` corrected (savepoint backs the pre-check).
7. **Also touched**: `pyproject.toml` ruff ignores now cover `scripts/**/test_*.py`; CI quality
   job runs `plans-lint`; `CLAUDE.md` lists skills/subagents/hooks (adapters arrive with 002).

## Test coverage

## Review findings

## Verification
