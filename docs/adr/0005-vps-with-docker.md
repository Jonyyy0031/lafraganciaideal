# 0005 — Host on a single VPS with Docker

- **Status**: Accepted
- **Date**: 2026-10-02

## Context

A business that is just starting: low traffic, tight budget, one person operating it. The
architecture needs PostgreSQL, Valkey, object storage and two API processes (HTTP and worker)
plus the web app. Decision: [initiative README](../../plans/platform-infraestructura/README.md) #4.

## Decision

Production runs on one VPS with Docker Compose: API, worker, web (SSR), PostgreSQL and Valkey,
behind a reverse proxy with automatic HTTPS. Object storage may be the VPS or an S3-compatible
provider. Details (proxy, backups, secrets, deployment) are planned in
`plans/platform-infraestructura/002`.

## Alternatives considered

- **Serverless (Vercel + managed Postgres)**: less to operate, but long-running workers and
  queues need different tools; diverges from the local environment.
- **Mixed (web on Vercel, API on a VPS)**: two platforms to operate for no gain at this size.

## Consequences

- Local development and production share the same images and topology.
- Backups and updates are our responsibility; plan 002 must cover them before going live.
- Vertical scaling first; the modular monolith allows splitting later if needed.
