# 0001 — One monorepo for Python and TypeScript

- **Status**: Accepted
- **Date**: 2026-10-02

## Context

The API is Python and the web app is Angular ([ADR 0003](0003-fastapi-and-angular.md)). The
frontend client is generated from the API's OpenAPI document, so a change in the API and the
regenerated client should land together. One developer works on everything with AI agents,
who need the whole picture in one place. Decisions: [initiative README](../../plans/platform-infraestructura/README.md) #1, #2, #8.

## Decision

A single repository with `apps/api` (uv project), `apps/web` (pnpm project),
`packages/api-client` (generated), `infra/`, `docs/` and `plans/`. `just` is the single
entry point for tasks in both languages. Repository tooling (bootstrap, commit checks) is
Python, managed by the root `pyproject.toml`, so no Node is needed until phase 3.

## Alternatives considered

- **Two repositories**: API changes and client regeneration drift apart; agents lose context.
- **Turborepo/Nx as the orchestrator**: built around JavaScript; the Python side would be
  second-class. `just` treats both equally.
- **Makefile**: tab and `.PHONY` pitfalls, poor argument handling.

## Consequences

- One PR can change an endpoint, regenerate the client and update the UI.
- Two toolchains (uv and pnpm) to keep up to date; CI installs both once phase 3 starts.
