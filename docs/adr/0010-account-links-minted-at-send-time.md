# 0010 — Account links are minted at send time and only their digest is stored

- **Status**: Accepted
- **Date**: 2026-10-06

## Context

Staff are invited by email and users reset a forgotten password through an emailed link. A link
carries a secret token: whoever holds it can create an account or take one over while it is
valid. Email must leave through the worker, never inside an HTTP request
(`docs/architecture.md`), and events that must not be lost go through the transactional outbox
(ADR 0006). The outbox keeps every event payload in `platform.outbox` after delivery.

## Decision

- The command that starts the flow (invite, request a reset) saves the record with **no token**
  and publishes an event that carries only the record's id.
- An identity subscriber, running in the worker inside the relay's transaction, creates the
  token, stores **only its SHA-256 digest** on the record and sends the email through the shared
  `EmailSender` port (SMTP adapter; Mailpit in development). No raw token is stored anywhere. A
  redelivery creates a new token, so only the newest email's link works.
- Lifetimes: invitations 72 hours, resets 60 minutes (`INVITATION_TTL_HOURS`,
  `PASSWORD_RESET_TTL_MINUTES`). Each link works once. Inviting the same email again, or asking
  for another reset, invalidates the previous link.
- Links put the token in the URL **fragment** (`{ADMIN_WEB_URL}/admin/activar-cuenta#token=…`,
  `…/admin/restablecer-contrasena#token=…`). A fragment is never sent to a server, so it does
  not reach access logs or `Referer` headers; the web page reads it and `POST`s it in a body.
- Identity sends these two emails itself, through the shared `EmailSender` port. The future
  `notifications` module sends business emails and can reuse the port.

## Alternatives considered

- **Token in the outbox payload**: the raw secret would stay in `platform.outbox` after
  delivery; anyone who can read that table could take over an account while the link is valid.
- **Routing through `notifications`**: the token must be created next to identity's tables (no
  cross-module table access), so the raw token would have to travel in the event, which is the
  option above.
- **Sending inside the HTTP request**: slow, fails the request when SMTP is down, and cannot be
  retried.

## Consequences

- No raw token is ever stored; a database or outbox leak does not expose live links.
- A send that succeeds followed by a failed relay commit delivers a dead link; the retry then
  sends a working one. Rare and harmless.
- The relay's transaction holds a row lock while SMTP answers (10 s timeout). Revisit if account
  emails become frequent.
- Production needs a real SMTP provider and TLS (`SMTP_STARTTLS`); that belongs to the
  deployment plan.
