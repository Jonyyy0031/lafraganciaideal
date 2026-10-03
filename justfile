# Task runner for La Fragancia Ideal. Run `uv run just` to list recipes
# (just is a dev dependency: `rust-just` in pyproject.toml).

set positional-arguments

# Compose has no env file: defaults live in compose.yaml; export a variable to override one.
compose := "docker compose -f infra/docker/compose.yaml"
api_port := env("API_PORT", "8100")

# List available recipes
default:
    @just --list

# One-command local setup (idempotent: never overwrites apps/api/.env nor deletes data)
bootstrap:
    uv run scripts/bootstrap.py

# Start the infrastructure services and wait until they are healthy
up:
    {{ compose }} up -d --wait

# Stop the services (data is kept in the volumes)
down:
    {{ compose }} down

# Show service status and health
ps:
    {{ compose }} ps

# Follow service logs (optionally only some services: `just logs postgres`)
logs *services:
    {{ compose }} logs -f "$@"

# Open psql in the postgres container (e.g. `just psql -d fragancia_test -c 'select 1'`)
psql *args:
    {{ compose }} exec postgres sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' psql "$@"

# Drop and recreate a database, asking to type its name first (`--test` for the test database)
db-reset *flags:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--test" ) ]]; then
        echo "Usage: just db-reset [--test]"; exit 2
    fi
    user=$({{ compose }} exec -T postgres printenv POSTGRES_USER)
    db=$({{ compose }} exec -T postgres printenv POSTGRES_DB)
    if [[ "${1:-}" == "--test" ]]; then db="fragancia_test"; fi
    read -r -p "This DELETES all data in '$db'. Type the database name to confirm: " answer || answer=""
    if [[ "$answer" != "$db" ]]; then echo "Aborted: nothing was changed."; exit 1; fi
    {{ compose }} exec -T postgres psql -U "$user" -d postgres -v ON_ERROR_STOP=1 \
        -c "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE)" \
        -c "CREATE DATABASE \"$db\" OWNER \"$user\""
    echo "✔ $db recreated (empty)."

# Run the API with auto-reload on http://127.0.0.1:8100 (docs at /api/v1/docs)
api:
    cd apps/api && uv run uvicorn fragancia_api.main.http:create_app --factory --reload \
        --reload-dir src --host 127.0.0.1 --port {{ api_port }}

# Run the background worker (outbox relay and jobs)
worker:
    cd apps/api && uv run arq fragancia_api.main.worker.WorkerSettings

# Apply pending migrations (`--test` for the test database)
db-migrate *flags:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/api
    if [[ "${1:-}" == "--test" ]]; then uv run alembic -x test=true upgrade head; else uv run alembic upgrade head; fi

# Create a migration from the table definitions (e.g. `just db-revision "catalog brands"`)
db-revision message:
    #!/usr/bin/env bash
    set -euo pipefail
    cd apps/api
    next=$(printf "%04d" $(( $(ls migrations/versions/[0-9]*.py | wc -l) + 1 )))
    uv run alembic revision --autogenerate --rev-id "$next" -m "$1"

# Regenerate the committed OpenAPI document (apps/api/openapi.json) after changing a contract
openapi:
    uv run python -m fragancia_api.main.openapi

# Static type checks (mypy strict)
typecheck:
    uv run mypy

# Architecture rules: layers and module boundaries (import-linter)
arch:
    cd apps/api && uv run lint-imports

# Validate every initiative, plan and finding (docs/harness/conventions/plans.md)
plans-lint:
    PYTHONPATH=scripts uv run python -m plans.lint

# What needs attention, by plan status (`just plans-status catalog-perfumes`, `--all`)
plans-status *args:
    PYTHONPATH=scripts uv run python -m plans.status "$@"

# Does the diff stay inside the plan's Files: lines? (`just plans-scope plans/x/001-y.md`)
plans-scope plan *args:
    PYTHONPATH=scripts uv run python -m plans.scope "$@"

# Regenerate the agent/skill adapters from docs/harness/roles + scripts/harness/adapters.py
harness-sync:
    PYTHONPATH=scripts uv run python -m harness.sync

# Fail if a generated adapter drifted from its source (+ missing, ~ different, - obsolete)
harness-check:
    PYTHONPATH=scripts uv run python -m harness.sync --check

# Lint and format checks, plus every pre-commit hook on all files
lint:
    uv run ruff check
    uv run ruff format --check
    uv run pre-commit run --all-files

# Unit tests (tooling + API); no services needed
test *args:
    uv run pytest "$@"

# API integration tests against PostgreSQL/Valkey (needs `just up`; migrates the test db)
test-integration *args: (db-migrate "--test")
    uv run pytest apps/api/tests -m integration "$@"

# Claude Code guard hooks and adapter generator tests (inert payloads; no services needed)
test-harness *args:
    uv run pytest scripts/harness "$@"

# Everything CI checks in the quality job, plus the compose file validation
check: lint typecheck arch plans-lint harness-check (test "--ignore=scripts/harness") test-harness
    {{ compose }} config --quiet
