-- Runs ONLY when a new PostgreSQL volume is initialized. Isolated database for integration
-- tests (DATABASE_URL_TEST); the API refuses a test database whose name does not end in _test.
-- `just bootstrap` also ensures it exists for volumes created before this script.
CREATE DATABASE fragancia_test;
