#!/bin/sh
# Runs once, when the postgres volume is first initialized. Creates the database used by
# integration tests. `just bootstrap` also ensures it exists for volumes created earlier.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v test_db="$POSTGRES_TEST_DB" -v owner="$POSTGRES_USER" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER %I', :'test_db', :'owner')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'test_db')\gexec
SQL
