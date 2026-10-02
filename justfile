# Task runner for La Fragancia Ideal. Run `just` to list recipes.

set dotenv-load
set positional-arguments

env_file := if path_exists(".env") == "true" { ".env" } else { ".env.example" }
compose := "docker compose -f infra/docker/compose.yaml --env-file " + env_file

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

# Lint and format checks, plus every pre-commit hook on all files
lint:
    uv run ruff check
    uv run ruff format --check
    uv run pre-commit run --all-files

# Run the tooling tests
test:
    uv run pytest

# Everything CI checks in the quality job, plus the compose file validation
check: lint test
    {{ compose }} config --quiet
