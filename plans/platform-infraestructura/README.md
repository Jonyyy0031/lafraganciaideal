# platform-infraestructura — Repository foundation, local environment and development CI

## Goal

Leave a repository ready to start building the API (FastAPI) and later the web app (Angular):
one command brings up the whole local infrastructure safely and idempotently, commits follow
a verified convention, and CI checks on every PR that the repo and the environment still
work. No product code yet.

## Plans

| Plan | Title                                          | Depends on | Purpose                                              |
| ---- | ---------------------------------------------- | ---------- | ---------------------------------------------------- |
| 001  | Monorepo foundation, local environment and CI  | —          | Skeleton, compose, `just` bootstrap, docs and CI     |
| 002  | Production on a VPS (to be planned)            | 001 + API  | Prod compose, Caddy, backups, secrets and deployment |

## Dependency notes

002 is planned once at least the API skeleton exists: there is nothing to deploy before that.

## Decisions with the user

1. (2026-10-02) Architecture inspired by `~/codes/web-rh` (modular monolith, hexagonal,
   lightweight CQRS, contracts first, enforced boundaries), in a lighter version: a single
   developer working with AI agents, no mobile app, no multi-company.
2. (2026-10-02) Stack: **FastAPI** (Python, uv) + **Angular** (SSR). The frontend's TS client is
   generated from the API's OpenAPI document.
3. (2026-10-02) Business in Mexico; payments with **Mercado Pago** (card, MSI, OXXO, SPEI).
4. (2026-10-02) Hosting on a **VPS with Docker**.
5. (2026-10-02) Work order: 1) infrastructure, 2) API only, 3) web starting from a design.
6. (2026-10-02) No specs: brainstorm → plan → implementation.
7. (2026-10-02) This first sub-project includes **development CI**; production comes later (002).
8. (2026-10-02) Task runner: **`just`** (the repo is bilingual Python/Node).
9. (2026-10-02) **Repository documentation is written in English** (README, AGENTS, docs, ADRs,
   plans, CI docs, code comments). Customer-facing UI copy is Spanish (decided in phase 3).
10. (2026-10-02) **`just` is a dev dependency** (`rust-just` in `pyproject.toml`), run with
    `uv run just …`: the only system requirements are Docker, uv and git, and local and CI use
    the exact version locked in `uv.lock`.

## Delivered

- **001** (2026-10-02): repo foundation, local services (`infra/docker/compose.yaml`),
  idempotent `just bootstrap`, commit convention (`scripts/commits.py` + hooks), CI
  (`.github/workflows/ci.yml`), README/AGENTS/CONTRIBUTING, `docs/architecture.md` and
  ADRs 0001–0006. Pushed to `github.com/Jonyyy0031/lafraganciaideal`; CI green.

## Considered and discarded

- **Express + Next.js (same as web-rh)**: the user wants a different stack; the architecture
  carries over, the language does not.
- **Off-the-shelf platform (Medusa/Saleor)**: fighting its model for orders, made-to-order
  items, Mercado Pago MSI and WhatsApp cancels the head start.
- **Next.js only, no API**: blurry boundaries and no shared contracts.
- **Makefile**: `just` is more readable and has no tab/`.PHONY` pitfalls.
- **Production in this plan**: there are no processes to deploy yet.
