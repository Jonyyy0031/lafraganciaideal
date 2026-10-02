# La Fragancia Ideal

Online store and back office for a perfume business in Mexico: a catalog of perfumes (in stock
and made to order), cart and checkout with Mercado Pago (cards, interest-free installments,
OXXO, SPEI), and an admin area to manage products, prices, stock and the lifecycle of every
order. WhatsApp and parcel carrier integrations come later.

> **Status:** phase 1 — infrastructure. The API (phase 2) and the web app (phase 3) do not
> exist yet. The target design is in [docs/architecture.md](docs/architecture.md).

| Part                  | Stack                                                             | Status      |
| --------------------- | ----------------------------------------------------------------- | ----------- |
| `apps/api`            | Python · FastAPI · Pydantic v2 · SQLAlchemy 2 · Alembic · arq     | Phase 2     |
| `apps/web`            | Angular (SSR) — storefront + `/admin`                             | Phase 3     |
| `packages/api-client` | TypeScript client generated from the API's OpenAPI document       | Phase 3     |
| `infra/docker`        | PostgreSQL 18 · Valkey · RustFS (S3) · Mailpit                    | ✔ Available |

## Getting started

Requirements: **Docker** (with Compose v2), **[uv](https://docs.astral.sh/uv/)** and **git**.
[just](https://just.systems) is a dev dependency (`rust-just` in `pyproject.toml`, locked in
`uv.lock`), so nothing else is installed system-wide:

```bash
uv run just bootstrap   # .env, services, test database, S3 bucket and git hooks
```

The commands below are written as `just …`. Run them as `uv run just …`, or activate the
environment once per shell (`source .venv/bin/activate`, `.venv/bin/activate.fish` for fish).

Bootstrap is idempotent: run it as often as you like. It never overwrites an existing `.env`
and never deletes data.

## Local services

All ports bind to `127.0.0.1` only. Defaults differ from other local projects so they can run
side by side; override them in `.env`.

| Service    | Address                                    | Notes                                            |
| ---------- | ------------------------------------------ | ------------------------------------------------ |
| PostgreSQL | `127.0.0.1:5433`                           | databases `fragancia` and `fragancia_test`       |
| Valkey     | `127.0.0.1:6380`                           | job queues                                       |
| S3 API     | `http://127.0.0.1:9100`                    | bucket `fragancia-media` (product photos)        |
| S3 console | [localhost:9101](http://localhost:9101)    | user/password from `.env` (`S3_ACCESS_KEY`/`…_SECRET_KEY`) |
| Mailpit    | [localhost:8026](http://localhost:8026)    | SMTP on `127.0.0.1:1026`; captures every email   |

## Commands

```bash
just                       # list recipes
just up / just down        # start / stop services (data is kept)
just ps                    # status and health
just logs [service…]       # follow logs
just psql [-d fragancia_test] [-c 'select 1']
just db-reset [--test]     # drop and recreate a database (asks you to type its name)
just lint / just test      # lint + every pre-commit hook / tooling tests
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
