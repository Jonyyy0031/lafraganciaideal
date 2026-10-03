---
status: done
module: platform
min_implementer: mid
depends_on: []
---

# 001 — Monorepo foundation, local environment and development CI

## Context

The `lafraganciaideal/` directory is empty and has no git repo. The product is a platform to
sell and manage perfumes (catalog with in-stock and made-to-order items, cart, checkout with
Mercado Pago, admin panel, order states). Decisions in [README.md](README.md).

This plan only builds the **infrastructure**: monorepo layout, local services with Docker, an
idempotent bootstrap driven by `just`, a verified commit convention, initial documentation
(target architecture + ADRs) and development CI. No API or web code.

All repository documentation is written in **English** (decision 9).

It imitates `~/codes/web-rh` (reference repo by the same author):

- Compose with digest-pinned images, ports bound to `127.0.0.1` only, healthchecks and a
  `_test` database created by an init script: `~/codes/web-rh/infra/docker/docker-compose.yml:1-90`.
- Idempotent bootstrap that never overwrites `.env` files or deletes data:
  `~/codes/web-rh/scripts/bootstrap.mjs:1-80`.
- Commit rules (closed scope list, non-generic subject, no AI attribution):
  `~/codes/web-rh/commitlint.config.mjs:1-50`.
- CI with SHA-pinned actions, `permissions: contents: read`, `concurrency` and
  `persist-credentials: false`: `~/codes/web-rh/.github/workflows/ci.yml:1-40`.
- Plan format: `~/codes/web-rh/plans/_TEMPLATE.md` and `_INITIATIVE.md`.

Deliberate differences from web-rh:

- **Tooling scripts are Python** (run with `uv run`), not Node: the backend will be Python, so
  the repo needs no Node until Angular arrives.
- **Different default ports** from web-rh so both environments can run side by side on the
  same machine (see step 3).
- **No role harness or generated adapters**: just plans, ADRs and AGENTS.md.
- **Commit validation is an in-repo script** (`scripts/commits.py`) used by the `commit-msg`
  hook and by CI over the PR range, so commitlint (Node) is not needed.

Machine prerequisites: Docker with Compose v2, `uv`, `git` and **`just`** (not installed yet:
`sudo pacman -S just`). Bootstrap checks for them and fails with clear instructions.

## Out of scope

- Any `apps/api` code (FastAPI, mypy, import-linter, Alembic): phase 2.
- Any `apps/web` code (Angular, pnpm, generated client): phase 3.
- Production: prod compose, Caddy/HTTPS, backups, secrets, deployment (plan 002).
- Claude Code hooks guarding destructive actions (evaluated in a separate plan if needed).
- Seeds and migrations (no schema yet).

## Dependencies

None

## Steps

1. **Repository foundation**
   - Files: `.gitignore` (create), `.gitattributes` (create), `.editorconfig` (create),
     `.python-version` (create), `apps/.gitkeep` (create), `packages/.gitkeep` (create)
   - Do: `git init -b main`. `.gitignore` covers `.env`, `.env.*` except `.env.example`,
     `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `node_modules/`, `dist/`,
     `.angular/`, `CLAUDE.local.md`, `.claude/settings.local.json`. `.gitattributes` with
     `* text=auto eol=lf`. `.editorconfig`: UTF-8, LF, 2 spaces; 4 for `*.py`.
     `.python-version` = `3.13`.
   - Observable result: `git status` lists only expected files; no `.env` can be tracked.

2. **Python project for repo tooling**
   - Files: `pyproject.toml` (create), `uv.lock` (create)
   - Do: root project `fragancia-tooling` (not the API) with dev dependencies: `pytest`, `ruff`,
     `pre-commit`, `boto3`, `psycopg[binary]`. `ruff` config (line-length 100, rules
     `E,F,I,B,UP,S`, `S101` ignored in tests) and `pytest` config (`testpaths = ["scripts"]`).
     Versions locked in `uv.lock`.
   - Observable result: `uv sync` creates `.venv` cleanly; `uv run ruff check` passes.

3. **Development services with Docker Compose**
   - Files: `infra/docker/compose.yaml` (create),
     `infra/docker/postgres/01-create-test-db.sh` (create), `.env.example` (create)
   - Do: compose project `fragancia` with:
     - `postgres` (18-alpine), database `fragancia`; the init script creates `fragancia_test`
       with the same owner. Port `127.0.0.1:${POSTGRES_PORT:-5433}`.
     - `valkey` (latest stable alpine) with `--appendonly yes`. `127.0.0.1:${VALKEY_PORT:-6380}`.
     - `storage` (RustFS), S3 API on `127.0.0.1:${S3_PORT:-9100}`, console on
       `${S3_CONSOLE_PORT:-9101}`, with a healthcheck.
     - `mailpit`, SMTP on `127.0.0.1:${SMTP_PORT:-1026}`, UI on `127.0.0.1:${MAILPIT_UI_PORT:-8026}`,
       with a healthcheck.
     - Every image **pinned by digest** with a `# Source verified on <date>: <image:tag>`
       comment (resolve with `docker buildx imagetools inspect`; never invent digests). Named
       volumes, `restart: unless-stopped`, `TZ: UTC` on Postgres.
     - Header warning: `docker compose down -v` deletes the data.
     - Root `.env.example` with every compose variable and `S3_BUCKET=fragancia-media`, using
       obvious development credentials (`fragancia` / `fragancia-dev`).
   - Observable result: `docker compose -f infra/docker/compose.yaml config` validates; with
     web-rh running, both environments coexist without port clashes.

4. **Idempotent bootstrap**
   - Files: `scripts/bootstrap.py` (create), `scripts/infra.py` (create),
     `scripts/test_bootstrap.py` (create)
   - Do: `scripts/infra.py` holds small testable functions: `ensure_env_file(example, target)
     -> bool` (copies only if missing), `ensure_database(conn, name) -> bool`,
     `ensure_bucket(s3, name) -> bool`, `missing_tools() -> list[str]`. `bootstrap.py`
     orchestrates them: 1) check `docker info`, `uv`, `just`; 2) `.env` from `.env.example`
     (never overwritten); 3) `docker compose up -d --wait`; 4) ensure `fragancia_test` (in case
     the volume predates the init script); 5) ensure the S3 bucket; 6) print the URLs. Output
     uses `▸` / `+` / `=` markers like web-rh. Never deletes data or volumes.
   - Observable result: running it twice in a row exits 0 both times; the second run only
     reports `= already exists`.

5. **`just` recipes**
   - Files: `justfile` (create)
   - Do: recipes `bootstrap`, `up`, `down` (never `-v`), `ps`, `logs *svc`, `psql *args`
     (via `docker compose exec postgres psql`), `db-reset` (asks for confirmation by typing the
     database name; recreates only that database, `--test` for `fragancia_test`; never removes
     volumes), `lint` (`ruff check` + `ruff format --check` + `pre-commit run --all-files`),
     `test` (`pytest`), `check` (lint + test + `compose config`). Running `just` with no
     arguments lists recipes (`default: @just --list`). Every recipe has a doc comment.
   - Observable result: `just --list` shows the recipes with descriptions; `just check` passes.

6. **Verified commit convention**
   - Files: `scripts/commits.py` (create), `scripts/test_commits.py` (create),
     `docs/modules.json` (create), `.pre-commit-config.yaml` (create)
   - Do: `docs/modules.json` registers modules `catalog`, `inventory`, `orders`, `payments`,
     `identity`, `notifications`, `shipping` (all `planned`) and `platform`, `web` (`active`).
     `commits.py` validates Conventional Commits: type from the conventional list, required
     scope ∈ modules + cross-cutting scopes (`api`, `web`, `infra`, `ci`, `deps`, `docs`,
     `repo`), header ≤ 100 chars, non-generic subject (same regex as web-rh), no AI attribution
     trailers. Two modes: `commits.py --file <msg>` (hook) and `commits.py --range <a>..<b>`
     (CI). `.pre-commit-config.yaml`: hooks trailing-whitespace, end-of-file-fixer, check-yaml,
     check-merge-conflict, check-added-large-files, detect-private-key, ruff, ruff-format,
     actionlint, plus a local `commit-msg` hook calling `commits.py --file`. `just bootstrap`
     installs the hooks (`pre-commit install --hook-type pre-commit --hook-type commit-msg`).
   - Observable result: `git commit -m "changes"` is rejected with a clear message;
     `git commit -m "chore(infra): add development services with docker compose"` passes.

7. **Development CI**
   - Files: `.github/workflows/ci.yml` (create), `.github/workflows/README.md` (create),
     `.github/pull_request_template.md` (create)
   - Do: workflow `CI` on `pull_request`, `push` to `main` and `workflow_dispatch`;
     `permissions: contents: read`; `concurrency` with cancel-in-progress; every action
     **pinned by SHA** with a version comment (resolve them, never invent them). Jobs:
     - `quality`: `astral-sh/setup-uv` → `uv sync --frozen` → `uv run ruff check` →
       `uv run ruff format --check` → `uv run pre-commit run --all-files` → `uv run pytest`.
     - `commits` (PRs only): checkout with `fetch-depth: 0` and
       `uv run scripts/commits.py --range origin/${{ github.base_ref }}..HEAD`.
     - `infra`: install `just`, run `just bootstrap` **twice** (idempotency) and a smoke test:
       `pg_isready` on both databases, bucket exists, `curl` to the Mailpit API; finish with
       `just down`.
     The README explains each job and how to reproduce it locally. PR template with plan,
     changes, and how it was verified.
   - Observable result: the workflow passes `actionlint`; on a test PR all three jobs pass.

8. **Initial documentation (English)**
   - Files: `README.md` (create), `AGENTS.md` (create), `CLAUDE.md` (create),
     `CONTRIBUTING.md` (create), `docs/architecture.md` (create),
     `docs/adr/0000-template.md` (create), `docs/adr/0001-bilingual-monorepo.md` (create),
     `docs/adr/0002-modular-monolith.md` (create), `docs/adr/0003-fastapi-and-angular.md` (create),
     `docs/adr/0004-mercado-pago.md` (create), `docs/adr/0005-vps-with-docker.md` (create),
     `docs/adr/0006-outbox-from-day-one.md` (create), `docs/adr/README.md` (create),
     `plans/_TEMPLATE.md` (create), `plans/_INITIATIVE.md` (create), `plans/findings/README.md` (create)
   - Do: `README.md`: what it is, requirements, `just bootstrap`, services and URLs, commands.
     `AGENTS.md`: source of truth for agents — brainstorm → plan → implement workflow,
     documentation in English, target architecture rules (marked "from phase 2 on"), ban on
     destructive actions (`down -v`, deleting volumes, `git reset --hard`, `push --force`),
     "read the docs of the installed version", out-of-scope findings go to `plans/findings/`.
     `CLAUDE.md` = `@AGENTS.md`. `docs/architecture.md`: target view (modules, hexagonal layers,
     lightweight CQRS, events via outbox, order state machine) with everything marked
     **to be built**. ADRs in context/decision/consequences format, each citing the decisions
     in this initiative's README. Plan templates simplified from web-rh (Context, Out of scope,
     Dependencies, Steps, Acceptance criteria, Test layers, Deviations, Verification).
   - Observable result: a new agent reading `AGENTS.md` knows how to bring up the environment,
     how to propose work and what it must not do.

## Acceptance criteria

- [ ] On a machine with Docker, uv and just, and no `.env`: `just bootstrap` exits 0 and all
      four services show `healthy` in `just ps`.
- [ ] A second `just bootstrap` exits 0, leaves `.env` unchanged (same hash) and reports
      `= already exists` for env, test database and bucket.
- [ ] `just psql -d fragancia -c 'select 1'` and `just psql -d fragancia_test -c 'select 1'`
      return `1`.
- [ ] The `fragancia-media` bucket exists in RustFS (console at `http://localhost:9101`).
- [ ] Mailpit answers at `http://localhost:8026` and accepts SMTP on `127.0.0.1:1026`.
- [ ] No published port listens outside `127.0.0.1` (`ss -tlnp`).
- [ ] With web-rh running in parallel there are no port conflicts.
- [ ] `just down` stops the services and, after `just up`, the data is still there.
- [ ] `just db-reset` without typing the database name aborts without touching anything.
- [ ] `git commit -m "changes"` is rejected; a conventional message with a valid scope passes;
      a message with `Co-Authored-By:` is rejected.
- [ ] `just check` passes.
- [ ] Every document created by this plan is in English.
- [ ] CI: the `quality`, `commits` and `infra` jobs pass on a PR.

## Test layers required

| Layer       | Applies | Focus                                                                       |
| ----------- | ------- | --------------------------------------------------------------------------- |
| unit        | yes     | `scripts/infra.py` (idempotent env/db/bucket with test doubles), `commits.py` |
| integration | yes     | CI `infra` job: real bootstrap twice + service smoke test                    |
| domain      | no      | no product code                                                             |
| http        | no      | no API                                                                      |
| e2e         | no      | no web app                                                                  |

## Deviations

1. **Python 3.14 instead of 3.13** — the plan said `.python-version = 3.13`; 3.14 is the
   current stable release and the one installed locally. `.python-version`, `requires-python`
   and ruff's `target-version` use 3.14.
2. **`just` in CI** — CI installs it with `uv tool install rust-just` (same 1.58.0 binary)
   instead of adding a third-party action. Locally it was already installed with `pacman`.
3. **Image tags** — Valkey `9.0-alpine`, RustFS `1.0.1-preview.16` (a fixed tag rather than
   `latest`), Mailpit `v1.31.3`. `docker buildx` is not installed locally, so digests were
   read with `docker pull` + `docker image inspect` (multi-arch index digests); the README
   documents both methods.
4. **CI `commits` job** — uses `pull_request.base.sha..head.sha` instead of
   `origin/<base_ref>..HEAD`: in a PR checkout `HEAD` is GitHub's synthetic merge commit.
5. **pre-commit details** — ruff runs as local hooks from the uv environment (one version,
   locked in `uv.lock`) instead of the `ruff-pre-commit` repo; actionlint comes from
   `actionlint-py`; `check-json` and `check-toml` were added; `default_stages: [pre-commit]`
   was needed so file hooks do not run again at the `commit-msg` stage. External hook repos
   are frozen to SHAs.
6. **Commit convention reference** — `commits.py` points to `CONTRIBUTING.md → Commits`
   (there is no `docs/conventions.md` in this plan).
7. **Git identity** — set per repository (`git config --local`) to the user's personal
   identity, as instructed.
8. **Extra commit** — `fix(repo): show the rejected commit header as plain text` (the error
   printed a Python list) found during verification.

## Test coverage

Not applicable: completed before the harness existed (2026-10-02); tests and review evidence are recorded in Verification.

## Review findings

Not applicable: completed before the harness existed (2026-10-02); tests and review evidence are recorded in Verification.

## Verification

Run on 2026-10-02 with web-rh's environment running in parallel (`rrhh-*` containers on
5432/6379/9000-9001/1025/8025).

| Criterion | Result | Evidence |
| --- | --- | --- |
| `just bootstrap` from no `.env` exits 0, 4 services healthy | ✔ | `+ .env created`, `+ bucket fragancia-media created`; `ps`: mailpit/postgres/storage/valkey `healthy` |
| Second run exits 0, `.env` unchanged, reports `= already exists` | ✔ | `= .env`, `= fragancia_test`, `= bucket fragancia-media already exists`; sha256 of `.env` identical |
| Test database ensured for a volume that lacks it | ✔ | after dropping the empty `fragancia_test`: `+ fragancia_test created` |
| `just psql` on both databases | ✔ | `fragancia_test`, `1` |
| Bucket exists | ✔ | `head_bucket` OK |
| Mailpit UI and SMTP | ✔ | `/api/v1/info` → `v1.31.3`; SMTP bound on `127.0.0.1:1026` |
| No port outside `127.0.0.1` | ✔ | `ss -tln`: 5433, 6380, 9100, 9101, 1026, 8026 all on `127.0.0.1` |
| Coexists with web-rh | ✔ | both stacks up at the same time, no conflicts |
| Data survives `just down` + `just up` | ✔ | test DB and bucket still present; Valkey `PONG` |
| `just db-reset` without the name aborts | ✔ | `Aborted: nothing was changed.`, exit 1 |
| Commit messages | ✔ | real `git commit`: `changes` rejected; `Co-Authored-By:` rejected; 7 conventional commits accepted; `commits.py --range` → `✔ 3 commit message(s)` |
| `just check` | ✔ | ruff, every pre-commit hook (incl. actionlint) Passed; `28 passed` |
| Documentation in English | ✔ | README, AGENTS, CLAUDE, CONTRIBUTING, architecture, 7 ADR files, templates, CI docs |
| CI jobs pass | ✔ | push to `main`, run [37055491475](https://github.com/Jonyyy0031/lafraganciaideal/actions/runs/37055491475): `Quality` ✓ 19s, `Local environment (bootstrap twice + smoke)` ✓ 26s. `Commit messages` is PR-only, so it was skipped on this push; its command (`commits.py --range`) was verified locally |
