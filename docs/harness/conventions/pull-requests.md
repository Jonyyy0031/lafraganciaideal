# Pull request conventions

Work reaches `main` only through a pull request. The **title** follows the commit convention
([commits.md](commits.md)): `feat(catalog): admin can create and list brands`. The **body**
follows `.github/pull_request_template.md` and answers six questions, in this order, so a
reviewer who has not read the code or the chat can decide whether to merge. **No AI
attribution** in titles or bodies (no "Generated with …", no co-author lines).

Write for a reader with five minutes. Facts, not adjectives; real command output, not
promises. When the plan already holds the evidence, link to it instead of copying it all.

## 1. What changed

The outcome in 2–3 lines, readable without opening the code: what someone can now do, or what
no longer breaks.

- **Good**: "The admin can create brands (`POST /api/v1/admin/brands`) and list them with
  their status; the storefront lists active brands (`GET /api/v1/brands`). Brand names are
  unique by slug. `catalog` is now the reference module for every new module."
- **Bad**: "Added router, use case, tables, migration and tests for brands." (a file list,
  not an outcome)

## 2. Why

The problem or need, and a link to the plan and the README decisions it applies.

- **Good**: "The catalog needs brands before perfumes can reference them, and phase 2 needs a
  complete module to copy. Plan:
  [platform-api-foundation/002](../../../plans/platform-api-foundation/002-catalog-brands-reference-module.md);
  decisions 2 (a real business slice as the reference module) and 3 (declared access) of
  the initiative README."
- **Bad**: "Needed for the catalog." (no plan, no decision, nothing a reviewer can check)

## 3. How

The approach, key design decisions, alternatives discarded, and the files worth reviewing
first.

- **Good**: "`Brand` aggregate validates the name and derives the slug; `CreateBrand` runs in
  `TransactionRunner.run` and publishes `catalog.brand.created` through the outbox. Unique
  violations map to `CATALOG_BRAND_ALREADY_EXISTS` (409) inside a savepoint, so the
  existence pre-check is backed by the database under concurrent creates. Discarded: an ORM model (ADR 0007 keeps Core tables + mappers).
  Review first: `modules/catalog/domain/brand.py`,
  `infrastructure/sql_brand_repository.py`, `migrations/versions/0002_catalog_brands.py`."
- **Bad**: "Standard hexagonal implementation, see code." (no decisions, no reading order)

## 4. How it was verified

Commands with their **real** results, live checks, and evidence (or a link to the plan's
`## Verification`). Only what was actually run.

- **Good**: "`uv run just check` → 0 failures (148 passed). `uv run just test-integration` →
  12 passed. Live: `uv run just api`; `curl -X POST …/admin/brands` → 201 `{id}`; same name
  again → 409 `CATALOG_BRAND_ALREADY_EXISTS`; without token → 401
  `AUTHENTICATION_REQUIRED`; `just psql -c "select count(*) from platform.outbox where name =
  'catalog.brand.created'"` → 1. Full log in the plan's Verification (2026-10-01)."
- **Bad**: "Tested locally, everything works." (no commands, no results, unfalsifiable)

## 5. What's missing

NOT VERIFIED items, known limitations, follow-ups and open findings. Write "Nothing" only if it
is true.

- **Good**: "NOT VERIFIED: behaviour under concurrent creates beyond the unique-constraint
  test. Limitation: brands cannot be renamed or deactivated yet (plan 003). Open finding:
  `plans/findings/platform-request-id-on-422.md`."
- **Bad**: "Nothing." (while the plan's Verification lists a NOT VERIFIED item) — or omitting
  the section.

## 6. Risks and rollout

Migrations, configuration/env changes, manual steps after merge, or "None".

- **Good**: "Migration `0002_catalog_brands` creates schema `catalog` and table `brands`;
  reversible (CI ran downgrade/upgrade). New setting: none. After merge, run
  `uv run just db-migrate` locally."
- **Bad**: "Low risk." (says nothing about the migration a deployer must run)

(Counts, event names and the finding file in these examples are illustrative.)

## Checklist (end of the body)

- [ ] `uv run just check` passes
- [ ] `uv run just test-integration` passes (when persistence changed)
- [ ] `uv run just plans-scope <plan>` passes (when there is a plan)
- [ ] `apps/api/openapi.json` regenerated with `uv run just openapi` (when a contract changed)
- [ ] Docs updated, in English

The reviewer role checks that the body answers the six sections with real evidence
([roles/reviewer.md](../roles/reviewer.md)).
