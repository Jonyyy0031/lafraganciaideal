# La Fragancia Ideal

Online store and back office for a perfume business in Mexico: a catalog of perfumes (in stock
and made to order), cart and checkout with Mercado Pago (cards, interest-free installments,
OXXO, SPEI), and an admin area to manage products, prices, stock and the lifecycle of every
order. WhatsApp and parcel carrier integrations come later.

> **Status:** phase 2 — the API platform exists (health, errors, declared access, outbox,
> worker); business modules are being added. The web app (phase 3) does not exist yet.
> Design: [docs/architecture.md](docs/architecture.md) · API guide: [apps/api/README.md](apps/api/README.md).

| Part                  | Stack                                                             | Status      |
| --------------------- | ----------------------------------------------------------------- | ----------- |
| `apps/api`            | Python 3.14 · FastAPI · Pydantic v2 · SQLAlchemy 2 · Alembic · arq | ✔ Platform  |
| `apps/web`            | Angular (SSR) — storefront + `/admin`                             | Phase 3     |
| `packages/api-client` | TypeScript client generated from the API's OpenAPI document       | Phase 3     |
| `infra/docker`        | PostgreSQL 18 · Valkey · RustFS (S3) · Mailpit                    | ✔ Available |

## Getting started

Requirements: **Docker** (with Compose v2), **[uv](https://docs.astral.sh/uv/)** and **git**.
[just](https://just.systems) is a dev dependency (`rust-just` in `pyproject.toml`, locked in
`uv.lock`), so nothing else is installed system-wide:

```bash
uv run just bootstrap   # apps/api/.env, services, migrations, S3 bucket and git hooks
uv run just api         # API on http://127.0.0.1:8100/api/v1/docs
uv run just worker      # background worker (outbox relay)
```

The commands below are written as `just …`. Run them as `uv run just …`, or activate the
environment once per shell (`source .venv/bin/activate`, `.venv/bin/activate.fish` for fish).

Bootstrap is idempotent: run it as often as you like. It never overwrites `apps/api/.env` (it
only appends settings added to `.env.example` later) and never deletes data. There is a single
env file, `apps/api/.env`; compose needs none (its defaults are inline, as in web-rh).

## Local services

All ports bind to `127.0.0.1` only. Defaults differ from other local projects so they can run
side by side; override one by exporting it, e.g. `POSTGRES_PORT=5544 uv run just up`. Compose
reads only the exported variables: when you move PostgreSQL, Valkey or S3 to another port,
update the matching URLs in `apps/api/.env` too (`DATABASE_URL`, `DATABASE_URL_TEST`,
`VALKEY_URL`, `S3_ENDPOINT_URL`) — the API, Alembic and bootstrap read them from there.

| Service    | Address                                    | Notes                                            |
| ---------- | ------------------------------------------ | ------------------------------------------------ |
| PostgreSQL | `127.0.0.1:5433`                           | databases `fragancia` and `fragancia_test`       |
| Valkey     | `127.0.0.1:6380`                           | job queues                                       |
| S3 API     | `http://127.0.0.1:9100`                    | bucket `fragancia-media` (product photos)        |
| S3 console | [localhost:9101](http://localhost:9101)    | user `fragancia`, password `fragancia-dev` |
| Mailpit    | [localhost:8026](http://localhost:8026)    | SMTP on `127.0.0.1:1026`; captures every email   |

## Commands

```bash
just                       # list recipes
just up / just down        # start / stop services (data is kept)
just ps                    # status and health
just logs [service…]       # follow logs
just psql [-d fragancia_test] [-c 'select 1']
just db-reset [--test]     # drop and recreate a database (asks you to type its name)
just api / just worker     # run the API (port 8100) / the worker
just db-migrate [--test]   # apply migrations
just lint / just test      # lint + every pre-commit hook / unit tests
just typecheck / just arch # mypy strict / import-linter boundaries
just test-integration      # API tests against PostgreSQL and Valkey
just check                 # everything CI's quality job checks
```

> ⚠️ `docker compose down -v` deletes every volume (all data). It is intentionally not a recipe.

## Updating infrastructure images

Images in `infra/docker/compose.yaml` are pinned by digest. To update one: pull the new tag,
read its digest with `docker image inspect <image:tag> --format '{{index .RepoDigests 0}}'`
(or `docker buildx imagetools inspect <image:tag>`), check data compatibility (major PostgreSQL
upgrades need a dump/restore), replace the digest and the `Source verified on` comment, and
run `just bootstrap`. Never delete volumes to work around an incompatibility.

## How work happens

Changes in behavior go through a plan: brainstorm → plan in `plans/` → approval → implement →
verify. See [AGENTS.md](AGENTS.md) (the entry point for humans and AI agents) and
[CONTRIBUTING.md](CONTRIBUTING.md) (commits, branches, pull requests). Architecture decisions
live in [docs/adr/](docs/adr/).
