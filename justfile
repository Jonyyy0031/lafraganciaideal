# Task runner for La Fragancia Ideal. Run `uv run just` to list recipes
# (just is a dev dependency: `rust-just` in pyproject.toml).

set dotenv-load
set positional-arguments

env_file := if path_exists(".env") == "true" { ".env" } else { ".env.example" }
compose := "docker compose -f infra/docker/compose.yaml --env-file " + env_file
api_port := env("API_PORT", "8100")

# List available recipes
default:
    @just --list

# One-command local setup (idempotent: never overwrites .env nor deletes data)
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
    {{ compose }} exec postgres psql -U "${POSTGRES_USER:-fragancia}" -d "${POSTGRES_DB:-fragancia}" "$@"

# Drop and recreate a database, asking to type its name first (`--test` for the test database)
db-reset *flags:
    #!/usr/bin/env bash
    set -euo pipefail
    user="${POSTGRES_USER:-fragancia}"
    db="${POSTGRES_DB:-fragancia}"
    if [[ "${1:-}" == "--test" ]]; then db="${POSTGRES_TEST_DB:-fragancia_test}"; fi
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

# Static type checks (mypy strict)
typecheck:
    uv run mypy

# Architecture rules: layers and module boundaries (import-linter)
arch:
    cd apps/api && uv run lint-imports

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

# Everything CI checks in the quality job, plus the compose file validation
check: lint typecheck arch test
    {{ compose }} config --quiet
