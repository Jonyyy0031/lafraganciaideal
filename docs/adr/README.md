# Architecture Decision Records

One file per decision, numbered, never rewritten: a changed decision gets a new ADR that
supersedes the old one. Copy [0000-template.md](0000-template.md).

| ADR                                     | Decision                                               |
| --------------------------------------- | ------------------------------------------------------ |
| [0001](0001-bilingual-monorepo.md)      | One monorepo for Python (API) and TypeScript (web)     |
| [0002](0002-modular-monolith.md)        | Modular monolith with enforced module boundaries       |
| [0003](0003-fastapi-and-angular.md)     | FastAPI for the API, Angular (SSR) for the web app     |
| [0004](0004-mercado-pago.md)            | Mercado Pago as the first payment provider             |
| [0005](0005-vps-with-docker.md)         | Host on a single VPS with Docker                       |
| [0006](0006-outbox-from-day-one.md)     | Transactional outbox for domain events from day one    |
| [0007](0007-sqlalchemy-core-and-mappers.md) | SQLAlchemy Core tables + explicit mappers (no ORM mapping) |
| [0008](0008-result-type-and-manual-composition-root.md) | `Result` for expected errors; manual composition root |
| [0009](0009-opaque-sessions-in-postgres.md) | Opaque back-office sessions in PostgreSQL, carried by an httpOnly cookie |
