---
status: done
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
8. **Tester files not declared in step 13** — the tester added `scripts/plans/conftest.py`
   (shared fixtures, written with the first tests) and `scripts/plans/test_lib.py`; both are
   tests of this plan's tooling (review finding 3).
9. **Repairs after review (2026-10-02)** — review → implementing: finding 1, `Files:` entries
   only start at a line that begins with `Files:` (`lib._FILES_ENTRY`); the strict-xfail GAP
   test became a regular regression test. Finding 2, docs mark what arrives with plan 002.
   Finding 4, README explains that moving a port also needs the URLs in `apps/api/.env`.
   Finding 5, lint compares scalar fields as text (`_scalar`), no `TypeError`. Finding 6,
   `plans-scope` allows only migrations that did not exist at the merge-base. Nits: wrapped
   `Files:` lines stop only at `N. ` steps; `## ` inside code fences no longer splits
   sections; `just db-reset` rejects anything but no flag or `--test` (exit 2).

## Test coverage

Baseline (`uv run just check`): green, 487 passed. Layer: tooling (`scripts/**/test_*.py`),
the only unit-level layer this plan requires; the CI `infra` job is the integration layer.
`ts` = `scripts/test_*.py`, `tp` = `scripts/plans/test_*.py`.

| Behavior | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| Env file copied only if missing, never overwritten | `infra.py:17` | tooling | `ts/test_bootstrap.py::test_ensure_env_file_*` | CONFIRMED |
| Missing keys appended, existing values kept, idempotent (Deviation 1) | `infra.py:25` | tooling | `test_ensure_env_keys_*` (4 tests) | CONFIRMED |
| `read_env` ignores comments/blank lines | `infra.py:48` | tooling | `test_read_env_ignores_comments_and_blank_lines` | CONFIRMED |
| Test DB ensured in container, idempotent, safe names, uses container credentials, errors propagate | `infra.py:71` | tooling | `test_ensure_database_in_container_*` (5 tests) | CONFIRMED |
| Bucket ensured; 404/NoSuchBucket = missing; other errors propagate | `infra.py:86` | tooling | `test_ensure_bucket_*` (4 tests) | CONFIRMED |
| `apps/api/.env.example` carries the S3 settings | step 2 | tooling | `test_the_api_env_example_carries_the_s3_settings` | CONFIRMED |
| Bootstrap works with no root `.env`, second run reports "already exists" | `bootstrap.py:62-93` | tooling (faked effects) | `test_bootstrap_works_without_a_root_env_and_is_idempotent` | CONFIRMED |
| Leftover root `.env` is mentioned, never deleted | `bootstrap.py:68` | tooling | `test_bootstrap_mentions_but_never_deletes_a_leftover_root_env` | CONFIRMED |
| Existing `apps/api/.env` gains S3 keys | `bootstrap.py:65` | tooling | `test_bootstrap_adds_missing_s3_keys_to_an_existing_api_env` | CONFIRMED |
| Ports printed from compose defaults or exported variables | `bootstrap.py:28,73` | tooling | `test_bootstrap_prints_exported_ports_over_the_compose_defaults` | CONFIRMED |
| Missing tool / docker daemon down stops with exit 1 | `bootstrap.py:51-59` | tooling | `test_bootstrap_stops_when_*` (2) | CONFIRMED |
| Dev and test databases migrated | `bootstrap.py:82-83` | tooling | `test_bootstrap_migrates_the_development_and_the_test_database` | CONFIRMED |
| Compose config valid with no env file | `justfile` `check` | check recipe | `docker compose config --quiet` in `just check` | CONFIRMED |
| Fresh volume gets `fragancia_test` from the `.sql` | step 1 | integration | `test_a_fresh_postgres_volume_creates_the_test_database` | NOT CONFIRMED (skip; CI `infra` job / verifier) |
| `POSTGRES_PORT=5544 just up` publishes on 5544 | step 1 | integration | `test_exporting_postgres_port_publishes_postgres_on_that_port` | NOT CONFIRMED (skip) |
| `just psql` / aborted `just db-reset --test` | `justfile:35,39` | integration | `test_psql_and_db_reset_use_the_containers_own_credentials` | NOT CONFIRMED (skip; touches the dev stack) |
| Commit scope `harness`; type/scope/length/generic/attribution rules | `commits.py:35-95` | tooling | `ts/test_commits.py` (existing + boundary 100, empty scope/message, generic subjects, attribution in merge) | CONFIRMED |
| `--file` and `--range` CLI exit codes and output | `commits.py:110-137` | tooling | `test_main_validates_a_message_file`, `test_main_requires_exactly_one_source`, `test_main_checks_every_commit_in_a_range` | CONFIRMED |
| Frontmatter/sections parsing, YAML errors, date normalization, `is_empty` | `lib.py:96,139` | tooling | `tp/test_lib.py` | CONFIRMED |
| Plans/initiatives/findings listing excludes templates, README, findings dir | `lib.py:155-195` | tooling | `test_plans_exclude_templates_readmes_findings_and_non_markdown` | CONFIRMED |
| Owner module (longest prefix), `resolve_ref` zero-pad/cross-initiative | `lib.py:149,198` | tooling | `tp/test_lint.py::test_owner_module_*`, `test_resolve_ref` | CONFIRMED |
| `declared_files`: wrapped lines, change marker, only inside Steps | `lib.py:210` | tooling | `test_declared_files_*` (3) | CONFIRMED |
| `declared_files` reads only `Files:` lines | `lib.py:221` (any line containing `files:` counts) | tooling | `tp/test_lib.py::test_a_do_line_mentioning_files_does_not_declare_paths` | GAP (strict xfail) — SUPERSEDED by repair (commit 1f0cce5): now a regular test, CONFIRMED |
| `- Do:` lines never declare paths, even containing `files:` | `lib._FILES_ENTRY` | tooling | `test_do_lines_never_declare_paths_even_with_a_files_colon` | CONFIRMED (repair) |
| Wrapped `Files:` continuation starting with a digit keeps its paths | `lib.declared_files` | tooling | `test_a_wrapped_files_continuation_starting_with_a_digit_keeps_its_paths` | CONFIRMED (repair) |
| A `## ` line inside a code fence does not split sections | `lib.parse_document` | tooling | `test_a_heading_inside_a_code_fence_does_not_split_sections` | CONFIRMED (repair) |
| List-valued `module`/`status`/`min_implementer` (plans) and `status`/`module` (findings) are reported, not crashes (review finding 5) | `lint.py` | tooling | `test_list_valued_plan_fields_are_reported_not_crashed` (3), `test_list_valued_finding_fields_are_reported_not_crashed` | CONFIRMED (repair) |
| Scope rejects editing a migration existing at the merge-base even with `tables.py` declared; allows a new one (review finding 6) | `scope.py:76-98` | tooling (temp git repo) | `test_editing_a_migration_from_the_base_fails_but_a_new_one_passes` | CONFIRMED (repair) |
| `reached` follows the pipeline | `lib.py:240` | tooling | `test_reached_follows_the_pipeline_order` | CONFIRMED |
| Lint structural rules (names, loose/nested plans, numbers, README, frontmatter, YAML) | `lint.py:43-106` | tooling | `test_each_rule_reports_a_precise_problem`, `test_missing_readme_and_frontmatter`, `test_invalid_yaml_is_reported`, `test_a_frontmatter_that_is_not_a_mapping_is_reported` | CONFIRMED |
| Lint field rules (status, module/owner, tier, depends_on list, superseded_by, deps exist/self/cycle/done) incl. positive cases | `lint.py:108-131,145` | tooling | parametrized rules + `test_dependencies_*`, `test_cycles_*`, `test_superseded_by_is_allowed_*` | CONFIRMED |
| Sections present/ordered; evidence by status (fail and pass); Out of scope rule | `lint.py:132-142` | tooling | `test_evidence_is_required_by_status`, `test_evidence_present_passes_at_each_status`, `test_out_of_scope_may_be_empty_*` | CONFIRMED |
| Findings rules (name, YAML, status, module, date, plan link) incl. positive cases | `lint.py:75-96` | tooling | `test_findings_*`, `test_planned_and_resolved_*`, `test_a_finding_*` | CONFIRMED |
| Lint output `path: message`, exit 1 / 0 | `lint.py:175` | tooling | `test_errors_are_prefixed_*`, `test_main_exits_one_*` | CONFIRMED |
| Status priority order, bump for unfinished deps, title ≤50, tier/next columns | `status.py:13,38-63` | tooling | `tp/test_status.py` (8 tests) | CONFIRMED |
| Status hides done/superseded unless `--all`; filter; open findings only when unfiltered | `status.py:75-107` | tooling | `test_done_plans_are_hidden_unless_all`, `test_filtering_by_initiative_*`, `test_only_open_findings_are_listed`, `test_unknown_initiative_fails` | CONFIRMED |
| Scope: declared/dir/plan/README/findings/hot/migrations/openapi/uv.lock allowed | `scope.py:76-90` | tooling | `test_declared_and_companion_files_are_in_scope`, `test_is_allowed_*` (4) | CONFIRMED |
| Scope changed set: committed, staged, unstaged, deleted, renames (both paths), untracked, merge-base | `scope.py:44-73` | tooling (temp git repo) | `test_committed_*`, `test_an_unstaged_edit_*`, `test_a_deleted_*`, `test_changes_made_on_the_base_*` | CONFIRMED |
| Scope output: counts, hot files, declared-unchanged, JSON-quoted NUL-safe paths, exit 0/1 | `scope.py:111-128` | tooling | `test_the_summary_line_*`, `test_paths_with_spaces_*`, `test_declared_but_unchanged_is_reported` | CONFIRMED |
| Scope git/usage errors exit 2 (bad/missing base, no plan, not a repo, no commits) | `scope.py:61-66,100-109` | tooling | `test_bad_base_*`, `test_missing_plan_*`, `test_a_directory_that_is_not_a_git_repository_*`, `test_a_repository_without_commits_*` | CONFIRMED |
| `plans-lint` / `plans-status` / `plans-scope` recipes; `check` runs `plans-lint` | `justfile:89-115` | check recipe | `plans-lint` step of `just check` (green on the repo); status/scope exercised via module `main` | CONFIRMED (recipe wiring only via `just check`) |

Added: `scripts/plans/test_lib.py` (new, 1 GAP), additions to `test_lint.py`, `test_status.py`,
`test_scope.py`, `test_bootstrap.py` (bootstrap `main` with faked effects, 3 NOT CONFIRMED
skips), `test_commits.py` (CLI + edge cases). Finding in the GAP above: a `- Do:` line
containing the text `files:` declares its backticked paths, widening scope silently; fix in
product code belongs to the implementer. Not tested: pull-request template and docs content
(prose, no behavior). Closing run: see the line below.

Closing `uv run just check`: green, `564 passed, 3 skipped, 15 deselected, 1 xfailed`
(baseline 487 passed). That run is superseded by the repair closing run: `573 passed, 3 skipped, 15 deselected`
(0 xfailed; the former GAP is now a regular test).

## Review findings

Reviewer pass, 2026-10-02, diff `origin/main...HEAD` (7 commits, `186f152..794d18c`). Untracked
plan-002 files (`.claude/`, `scripts/harness/`, plan 002) excluded from the review.

**Checklist: FAILED** (2 items).

- [ ] `plans-scope --base origin/main`: exit 1. Besides the expected plan-002 files it flags
      `scripts/plans/conftest.py` and `scripts/plans/test_lib.py`, committed in `794d18c` but
      declared on no `Files:` line (step 13 lists only `test_lint/status/scope.py`) and not
      recorded as a deviation. Hot file `docs/modules.json`: declared in step 9; the only edit
      extends the `$comment` string (no registry entry touched) — acceptable.
- [x] `uv run just check`: green (`564 passed, 3 skipped, 15 deselected, 1 xfailed`; plans OK).
- [x] `test-integration`: not applicable (no `apps/api` infrastructure/tables/migrations).
- [x] API checklist items (domain, CQRS, contracts, Result codes, Money/Clock, migrations,
      routes, wiring): not applicable — no `apps/api` source changed.
- [x] No secrets: only development defaults already in compose/`.env.example`.
- [x] Deviations honest: spot-checked 1 (`infra.py:25 ensure_env_keys`), 4
      (`platform-api-foundation/002` has `module: platform`) and 5 (002 is `approved`).
- [ ] Docs accurate vs. the repo: commands from plan 002 are presented as existing (major 2).
- [x] PR template has the six sections in order + checklist; no PR opened yet, so the body
      check is deferred to the PR.
- [x] Commits: convention followed, no AI attribution in the 7 messages.

### Major

1. **`declared_files` widens scope from any line containing `files:`** —
   `scripts/plans/lib.py:221` (`"files:" in line.lower()`). Failure: a step with
   `- Do: update the files: \`evil/x.py\` and \`README.md\`` declares both paths (reproduced:
   `['a/b.py', 'evil/x.py', 'README.md']`), so `plans-scope` passes changes the plan never
   listed — fail-open on the reviewer's scope gate. Should match only an entry that starts
   with `Files:` (optionally after `- `). Strict xfail
   `tp/test_lib.py::test_a_do_line_mentioning_files_does_not_declare_paths` already pins it.
2. **Docs present plan-002 commands/hooks as existing** — the plan's Out of scope requires
   them marked "added by plan 002", and the acceptance criterion says "commands that exist in
   this repo". `harness-sync`, `harness-check`, `test-harness` are not justfile recipes and
   `just check` runs neither; `.claude/` is untracked on this branch. Unmarked at:
   `AGENTS.md:89,95`; `CLAUDE.md:17` ("Active hooks"); `docs/harness/workflow.md:223,236,239`;
   `docs/harness/roles/reviewer.md:18` ("check passes (… hooks, harness)");
   `docs/harness/HARNESS.md:64,71` ("in `just check`"; the only disclaimer is at :49-50);
   `docs/harness/security.md:17`; `docs/harness/conventions/testing.md:30,78`. Failure: if
   001 merges before 002, a reader runs `uv run just harness-sync` → "Justfile does not
   contain recipe", and believes `just check` covers hooks/adapters when it does not.

### Minor

3. **Scope out-of-scope: tester files undeclared** — see checklist; add
   `scripts/plans/conftest.py` (create) and `scripts/plans/test_lib.py` (create) to step 13 or
   record a deviation, so `plans-scope` reports only plan-002 files.
4. **Exported port overrides break bootstrap** — `scripts/bootstrap.py:73,82-93` +
   `README.md:41`, `AGENTS.md:99-100`. Failure: `POSTGRES_PORT=5544 uv run just bootstrap`
   binds 5544 and prints it, but alembic uses `DATABASE_URL` (5433) from `apps/api/.env` →
   connection refused (or migrates whatever else listens on 5433). Same for
   `VALKEY_PORT`/`S3_PORT`. Docs should say the override must also be mirrored in
   `apps/api/.env` (or bootstrap should warn when they differ).
5. **Lint crashes on a non-scalar `module`** — `scripts/plans/lint.py:112` (and `:89` for
   findings): `module: [platform]` → `TypeError: unhashable type: 'list'` traceback instead
   of a `where: message` line (reproduced). Fail-closed, but not the precise message the
   plan promises.
6. **Migration allowance broader than documented** — `scripts/plans/scope.py:84`: any path
   under `apps/api/migrations/versions/` is allowed when a `tables.py` is declared, including
   edits/deletions of existing migrations; `conventions/plans.md` says "new". Failure: editing
   `0001_*.py` passes scope. Either check the git status is `A` or fix the doc.

### Nit

7. `scripts/plans/lib.py:223` — a wrapped `Files:` continuation line starting with a digit
   ends the entry, dropping its paths (fail-closed: shows as out of scope).
8. `scripts/plans/lib.py:122` — `## ` lines inside fenced code blocks start a new section;
   a fenced example inside `## Steps` would truncate the Files parsed after it.
9. `justfile:44` — `db-reset` treats any flag other than `--test` (typo `--tset`) as the dev
   database; the typed-name confirmation still protects it.

Status left at `review`: findings 1–6 need product/plan changes (repair handoff).

### Re-review (2026-10-02, after `1f0cce5` + `dafabfc`)

The first review above is superseded by this block for the current code.

**Checklist: PASSED.**

- `uv run just check`: green, `573 passed, 3 skipped, 15 deselected` (no xfail left).
- `plans-scope --base origin/main`: exit 1. Only these are outside: the plan-002 files
  (`.claude/*`, `scripts/harness/*`, `plans/platform-harness/002-…md`) and
  `scripts/plans/conftest.py` + `scripts/plans/test_lib.py`. The latter two are recorded as
  Deviation 8, the remedy finding 3 allowed. 50 declared paths, same as before the fix, so
  narrowing `Files:` dropped no real declarations.

Findings:

1. RESOLVED — `scripts/plans/lib.py:58,230` `_FILES_ENTRY` anchors at the line start.
   Re-probed: a `- Do: … files: \`evil/x.py\`` line now declares nothing. Former xfail is a
   regular test (`test_lib.py`).
2. RESOLVED — plan-002 items marked: `AGENTS.md:89,95,97`, `CLAUDE.md:5`,
   `HARNESS.md:64,71` (+ disclaimer :49-50), `workflow.md:236,239`, `reviewer.md:18`,
   `security.md:17`, `testing.md:30,78`.
3. RESOLVED (by deviation) — Deviation 8. Optional: declare both files in step 13 so the tool
   output only lists plan-002 files.
4. RESOLVED — `README.md:41-44` says that moving a port also needs the URLs in
   `apps/api/.env`. Residual nit: `AGENTS.md:101-102` still says only "export a variable to
   override one".
5. RESOLVED — `lint.py:36` `_scalar`. Re-probed: `module: [platform]` →
   `module ['platform'] is not in docs/modules.json`, no traceback.
6. RESOLVED — `scope.py:76-81,93-97` allows only migrations absent at the merge-base (plus a
   new `test_scope.py` regression).
7. Nits — wrapped line starting with a digit now kept (`lib.py:59` `_STEP`; re-probed);
   `## ` inside fences ignored (`lib.py:122-128`); `justfile:42-44` `db-reset` rejects other
   arguments with exit 2.

Regressions looked for: an unclosed fence swallows the remaining sections, and lint then
reports them missing (fail-closed). `migrations_at_base` git errors exit 2. `_scalar`
keeps non-string `status` invalid. None found.

All passed → `status: verify`.

## Verification

Verifier pass, 2026-10-02, branch `feat/platform-harness`, real Docker stack (project `fragancia`).

- `uv run just check`: `plans OK (6 plans, 0 findings)`, import-linter `7 kept, 0 broken`,
  `573 passed, 3 skipped, 15 deselected`, `docker compose config --quiet` clean.
- `uv run just test-integration`: `15 passed, 75 deselected`.
- Bootstrap run twice, both exit 0. Second run: `= apps/api/.env already exists`,
  `= fragancia_test already exists`, `= bucket fragancia-media already exists`. A root `.env`
  exists locally (not read, not deleted), so the run printed `! the root .env is no longer used`;
  the "no root `.env`" precondition was therefore not reproduced (tooling test covers it).
- Fresh volume: throwaway container (postgres image digest from compose, the `.sql` mounted
  read-only into `/docker-entrypoint-initdb.d/`); `psql \l` listed `fragancia` and
  `fragancia_test`; container stopped (`--rm`). Project volumes untouched.
- `uv run just psql -d fragancia_test -c 'select 1'` returned `1`. `echo nope | just db-reset --test`:
  `Aborted: nothing was changed.`, exit 1.
- `docs/harness/`: HARNESS, workflow, security, 5 roles, 5 conventions (+ `frontend.md`) present.
  `plans-lint`: OK. `plans-status`: 002 (approved) before 001 (verify), 4 done hidden; `--all`
  lists them.
- `plans-scope <this plan> --base origin/main`: exit 1; outside: `.claude/*`, `scripts/harness/*`,
  plan 002, and `scripts/plans/conftest.py` + `test_lib.py` (Deviation 8); 50 declared, 63 changed.
- Commit hook: `commits.py --file` accepted `docs(harness): ...` and rejected `wip stuff`.
- PR template has the six sections in order. Lint failure on an `approved` plan without Out of
  scope content: covered by `test_lint.py -k "out_of_scope or evidence"` (6 passed), not re-run on
  a real broken plan.
- Default stack still on `127.0.0.1:5433`.

NOT VERIFIED:

- `POSTGRES_PORT=5544 just up` publishing on 5544: the guard hook blocks `VAR=` prefixes in agent
  shells; not worked around. Default stack unchanged.
- `just db-reset --bogus` exits 2 with usage: the guard hook blocks any `db-reset` except
  `--test` for agents; covered only by the tooling regression test.
- "This initiative's PR uses the template": no PR exists yet.
- CI green: not exercised.

Status left at `verify`.
