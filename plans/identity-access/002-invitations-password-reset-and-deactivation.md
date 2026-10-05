---
status: draft
module: identity
min_implementer: mid
depends_on: ["001"]
---

# 002 — Invitations, password reset by email, and deactivating users

## Context

**Today** (plan 001, `done`):

- `User` has `is_active`, but nothing can change it. New users are always active
  (`modules/identity/domain/user.py:88-124`). `User.change_password` is the only mutator
  (`user.py:130-132`).
- `SqlUserRepository.save` persists only `password_hash` and `password_changed_at`
  (`infrastructure/sql_user_repository.py:63-68`).
- The only way to create a user is `just create-owner` (`main/cli.py`, README decision 4).
- `ResolveSessionActor` already refuses inactive users on every request
  (`application/commands/resolve_session_actor.py:40-42`). It builds
  `Actor(id, is_admin=True, session_id)` (`resolve_session_actor.py:46`). `Actor` has no
  permissions (`shared/application/actor.py:6-16`).
- Permissions exist only as data: `ROLE_PERMISSIONS` gives `users:manage` to owners only
  (`user.py:79-85`). No route checks a permission. `admin_router` runs `require_admin` first and
  then any extra `dependencies` (`shared/http/access.py:53-59`). `require_admin` stores the actor
  in `request.state.actor` (`access.py:38`).
- `LogIn` checks `is_active` and the password **outside** any transaction
  (`application/commands/log_in.py:101-105`). It then opens the session in a separate
  transaction without reloading the user (`log_in.py:108-123`). So a login in flight can open a
  session for a user who was deactivated, or whose password was reset, a moment earlier. Plan
  001 left this for plan 002 (its Out of scope, last bullet).
- **Events.** Aggregates record events (`shared/kernel/entity.py:4-16`). Commands publish them
  inside their transaction (`catalog/application/commands/create_brand.py:41`). The worker's
  relay delivers each outbox row to the subscribers **inside the relay's own unit of work**
  (`shared/infrastructure/outbox.py:103-130`). A `TransactionRunner.run` called by a subscriber
  joins that transaction (`shared/infrastructure/database.py:65-66`). If a subscriber raises,
  everything rolls back and the row is retried with backoff (`outbox.py:127-129,132-152`).
  Nothing subscribes to any event yet.
- **Email.** Nothing sends email. Mailpit listens on `127.0.0.1:${SMTP_PORT:-1026}`, with its UI
  on `${MAILPIT_UI_PORT:-8026}` (`infra/docker/compose.yaml:74-80`). `docs/architecture.md:149-150`
  says email goes out through the worker, never inside an HTTP request. The planned
  `notifications` module is "Email first, WhatsApp later, driven by events" (`docs/modules.json`).
- `SecureSessionTokens` makes 256-bit URL-safe tokens and their SHA-256 digests
  (`infrastructure/secure_session_tokens.py:5-12`).

**What this plan builds** (README decisions 4, 5, and 9–13):

- The owner invites **staff** by email (decision 11). The link is valid for 72 hours and works
  once; inviting the same email again invalidates the previous link (decision 9).
- Password reset by an emailed link, valid for 60 minutes and usable once. Using it closes
  **every** session of that user (decision 10).
- The owner lists users and deactivates or reactivates any user except themselves (decisions
  12, 13). Deactivating closes all of that user's sessions.
- The first permission-gated routes: `users:manage`.
- A shared `EmailSender` port with an SMTP adapter (Mailpit in development).
- The login race above is closed.

**Approach and why.**

- **The link's token is created when the email is sent, not when the request is made.**
  - Option A: the command creates the token and puts it in the outbox payload. The raw secret
    then stays in `platform.outbox` after delivery. Anyone who can read that table can take
    over an account while the link is valid.
  - Option B: the command creates the record with no token and publishes an event that only
    carries the record's id. The identity subscriber, running in the worker inside the relay's
    transaction, does three things: creates the token, stores only its digest on the record, and
    sends the email.
  - Option B is chosen. No raw token is ever stored. A redelivery after a failure creates a new
    token, so only the newest email's link works. That is the same rule as "invite again", and
    the next attempt sends a fresh link anyway.
  - The cost: a send that succeeds followed by a failed relay commit delivers a dead link. Then
    the retry sends a working one. This is rare and harmless.
- **Who sends these emails.** The identity module owns these two emails, through a **shared**
  `EmailSender` port:
  - The token must be created next to identity's tables. Another module may not read or write
    them (module rule 1).
  - The future `notifications` module will send business emails (orders), driven by events, and
    can reuse the same port.
  - Routing these emails through `notifications` now would force the raw token into the event,
    which is option A.
- **SMTP through the standard library.** `smtplib` runs in `asyncio.to_thread`, so no new
  dependency is added. Messages are plain text; this plan adds no HTML templates.
- **Permissions travel in the `Actor`.** `require_permission(name)` is a shared HTTP dependency
  that reads `request.state.actor`. It adds no database query.
- **Link endpoints are public and the token goes in the body.** The web links put the token in
  the URL **fragment** (`#token=…`). A fragment is never sent to a server, so it does not end up
  in access logs or `Referer` headers. Phase 3's pages read the fragment and `POST` it.
- **The reset request never reveals whether an account exists.** It always answers 202. It is
  throttled per email and per IP like a login (keys `reset-email:` / `reset-ip:`), so it cannot
  be used to flood someone's inbox.
- **Fixing the login race.** In its final transaction, `LogIn` reloads the user with
  `SELECT … FOR UPDATE`. It refuses to open the session if the user is now inactive or the
  password hash changed. Deactivating a user and resetting a password lock the same row. So
  either the login finishes first and its session is then revoked, or the login sees the change
  and fails.

**Defaults chosen by the architect** (not business rules; every one is an env var, and the user
confirms them by approving this plan):

| Setting | Default |
| --- | --- |
| `INVITATION_TTL_HOURS` | 72 (decision 9) |
| `PASSWORD_RESET_TTL_MINUTES` | 60 (decision 10) |
| `ADMIN_WEB_URL` | `http://localhost:4200` (base of the links) |
| `SMTP_HOST` / `SMTP_PORT` | `127.0.0.1` / `1026` (Mailpit) |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | empty (Mailpit needs none) |
| `SMTP_STARTTLS` | `false` |
| `MAIL_FROM` | `La Fragancia Ideal <no-reply@lafraganciaideal.test>` |

The link paths are a contract with phase 3:

- `{ADMIN_WEB_URL}/admin/activar-cuenta#token=<token>`
- `{ADMIN_WEB_URL}/admin/restablecer-contrasena#token=<token>`

**Imitated files.** Copy them by name:

| New | Copies |
| --- | --- |
| Aggregates with events | `modules/catalog/domain/brand.py` (records `BrandCreated`), `modules/identity/domain/session.py:15-70` |
| Errors | `modules/identity/domain/errors.py:12-57` |
| Commands | `modules/identity/application/commands/create_user.py:18-75` (slow hashing outside the transaction), `catalog/application/commands/create_brand.py:28-44` (publish in `run`) |
| SQL repositories | `modules/identity/infrastructure/sql_session_repository.py:12-74` |
| Queries | `modules/identity/infrastructure/sql_account_queries.py:12-57` |
| Fakes | `modules/identity/infrastructure/in_memory.py:13-59` |
| Router | `modules/identity/http/router.py:56-157` |
| Migration | `apps/api/migrations/versions/0003_identity_users_and_sessions.py` |

## Out of scope

- **Inviting owners.** An invitation always creates a `staff` user (decision 11). More owners are
  created with `just create-owner`.
- Changing a user's role, name or email; deleting users; a "user detail" endpoint.
- **A "preview invitation" endpoint** that shows the invited email before the password form.
  Phase 3 can add it if the design needs it.
- Signing in automatically after accepting an invitation or resetting a password. The web app
  sends the user to the login page.
- HTML emails, branded templates and translations (plain text, Spanish copy below).
- The `notifications` module and any business emails.
- **Production email.** Choosing a provider, SPF/DKIM, and refusing `SMTP_STARTTLS=false` in
  production belong to the deployment plan.
- Purging old invitations, password resets and throttle rows (a later cron, as in plan 001).
- An audit log of who deactivated whom. The rows keep `invited_by`; nothing else is recorded.
- Clearing the login throttle when a password is reset.
- Angular screens (phase 3).
- Cleaning stale wording elsewhere in the docs beyond the lines this plan names.

## Dependencies

- **001** (`done`): `User`, `Session`, `UserRepository`, `SessionRepository`
  (`domain/repositories.py:11-44`), `LoginThrottle`, `PasswordHasher`, `SessionTokens`,
  `AccountQueries` (`application/ports.py:8-58`), `AuthPolicy` (`application/policy.py:8-16`),
  `ResolveSessionActor`, the routers and `module.py` wiring, `SESSION_COOKIE`, `admin_router`
  and `public_router`.

## Steps

All module paths below are under `apps/api/src/fragancia_api/modules/identity/`. Every
`Files:` line still names the full path.

1. **Settings**
   - Files: `apps/api/src/fragancia_api/config.py` (modify), `apps/api/.env.example` (modify)
   - Do:
     - **`Settings`.** After the identity fields (`config.py:56-61`), add these fields under the
       comment `# Account emails (identity) and SMTP.`:

       | Field | Type and default |
       | --- | --- |
       | `invitation_ttl_hours` | `int = Field(default=72, ge=1)` |
       | `password_reset_ttl_minutes` | `int = Field(default=60, ge=1)` |
       | `admin_web_url` | `str = "http://localhost:4200"` |
       | `smtp_host` | `str = "127.0.0.1"` |
       | `smtp_port` | `int = Field(default=1026, ge=1, le=65535)` |
       | `smtp_username` | `str \| None = None` |
       | `smtp_password` | `SecretStr \| None = None` |
       | `smtp_starttls` | `bool = False` |
       | `mail_from` | `str = "La Fragancia Ideal <no-reply@lafraganciaideal.test>"` |

       Import `SecretStr` from pydantic. Add a model validator `_check_admin_web_url`. It
       raises `ValueError("ADMIN_WEB_URL must start with http:// or https:// and not end with /")`
       unless the value starts with `http://` or `https://` and does not end with `/`.
     - **`.env.example`.** After the session block (line 21), add the block below. Leave
       `SMTP_USERNAME` and `SMTP_PASSWORD` out: empty values are the defaults.

       ```
       # Account emails: invitation and password-reset links (identity). Mailpit in development:
       # http://127.0.0.1:8026 shows every message.
       INVITATION_TTL_HOURS=72
       PASSWORD_RESET_TTL_MINUTES=60
       ADMIN_WEB_URL=http://localhost:4200
       SMTP_HOST=127.0.0.1
       SMTP_PORT=1026
       SMTP_STARTTLS=false
       MAIL_FROM="La Fragancia Ideal <no-reply@lafraganciaideal.test>"
       ```

   - Observable result:
     - `make_settings()` loads with the defaults.
     - `make_settings(admin_web_url="localhost:4200")` and
       `make_settings(admin_web_url="http://x/")` raise `ValidationError`.
     - `make_settings(invitation_ttl_hours=0)` raises `ValidationError`.

2. **Shared: the email port and its adapters, permissions in the actor, `require_permission`**
   - Files: `apps/api/src/fragancia_api/shared/application/email.py` (create),
     `apps/api/src/fragancia_api/shared/infrastructure/smtp_email_sender.py` (create),
     `apps/api/src/fragancia_api/shared/infrastructure/in_memory.py` (modify),
     `apps/api/src/fragancia_api/shared/application/actor.py` (modify),
     `apps/api/src/fragancia_api/shared/http/access.py` (modify),
     `apps/api/src/fragancia_api/shared/http/__init__.py` (modify),
     `apps/api/src/fragancia_api/shared/module.py` (modify),
     `apps/api/src/fragancia_api/container.py` (modify)
   - Do:
     - **`shared/application/email.py`.**
       - A frozen, slotted dataclass `EmailMessage` with `to: str`, `subject: str` and
         `body: str` (plain text).
       - A `Protocol` `EmailSender` with `async def send(self, message: EmailMessage) -> None`.
         Its docstring: "Sends one email. Raises on failure; call it only from the worker (an
         outbox subscriber), never inside an HTTP request."
     - **`SmtpEmailSender`** (`smtp_email_sender.py`). Its constructor takes `host`, `port`,
       `username: str | None`, `password: str | None`, `starttls: bool` and `sender: str`.
       - `send` builds an `email.message.EmailMessage` with `From` = `sender`, `To`, `Subject`,
         and `set_content(body)` (UTF-8).
       - It sends through `await asyncio.to_thread(self._send_blocking, msg)`. The blocking
         function opens `smtplib.SMTP(host, port, timeout=10)` in a `with`. It calls
         `starttls()` if `starttls` is set, and `login(username, password)` if both are set.
         Then it calls `send_message(msg)`.
       - It never catches errors: a failure must reach the relay so the event is retried.
     - **`RecordingEmailSender`**, in `shared/infrastructure/in_memory.py`, next to
       `RecordingEventPublisher`. It keeps `sent: list[EmailMessage]` and has a flag
       `fail: bool = False`. When `fail` is true, `send` raises `ConnectionError("smtp down")`.
     - **`Actor`** (`actor.py:6-16`). Add `permissions: frozenset[str] = frozenset()` after
       `session_id`. Extend the docstring: "`permissions` are the user's permission names; the
       identity resolver sets them."
     - **`require_permission`** in `access.py`, after `require_admin`:

       ```python
       def require_permission(permission: str) -> Callable[[Request], None]:
           """Router dependency for admin routes that need a permission (after require_admin)."""

           def check(request: Request) -> None:
               if permission not in request.state.actor.permissions:
                   raise Forbidden

           return check
       ```

       - Import `Callable` from `collections.abc`.
       - Change the description of `ADMIN_RESPONSES[403]` (`access.py:49`) to
         `"The actor is not an admin or lacks the permission"`.
       - The team router imports `USERS_MANAGE` from `modules/identity/domain/user.py:80`.
       - Export `require_permission` from `shared/http/__init__.py`.
     - **`Platform`** (`shared/module.py:16-26`). Add the field `email: EmailSender` after
       `events`.
     - **`build_container`** (`container.py:48-58`). Build `email=SmtpEmailSender(...)` from the
       settings. `password` is `settings.smtp_password.get_secret_value()` when it is set,
       else `None`. Pass it to `Platform`.
   - Observable result:
     - `uv run just arch` is green. The port lives in `shared.application`, so module
       application code may import it.
     - `Actor(id="x", is_admin=True)` still builds.

3. **Domain**
   - Files: `apps/api/src/fragancia_api/modules/identity/domain/errors.py` (modify),
     `apps/api/src/fragancia_api/modules/identity/domain/user.py` (modify),
     `apps/api/src/fragancia_api/modules/identity/domain/events.py` (create),
     `apps/api/src/fragancia_api/modules/identity/domain/invitation.py` (create),
     `apps/api/src/fragancia_api/modules/identity/domain/password_reset.py` (create),
     `apps/api/src/fragancia_api/modules/identity/domain/repositories.py` (modify)
   - Do:
     - **`errors.py`.** Append these errors, same style:

       | Error | Category | Code | Message |
       | --- | --- | --- | --- |
       | `LinkInvalid` | `InvalidValueError` | `IDENTITY_LINK_INVALID` | "This link is invalid or has expired" |
       | `UserNotFound` | `NotFoundError` | `IDENTITY_USER_NOT_FOUND` | "User not found" |
       | `InvitationNotFound` | `NotFoundError` | `IDENTITY_INVITATION_NOT_FOUND` | "Invitation not found" |
       | `CannotDeactivateSelf` | `BusinessRuleViolationError` | `IDENTITY_CANNOT_DEACTIVATE_SELF` | "You cannot deactivate your own account" |

       Import `BusinessRuleViolationError` from the kernel.
     - **`user.py`.**
       - Add `deactivate()` (sets `is_active = False`) and `reactivate()` (sets
         `is_active = True`). Both are idempotent.
       - Update the module docstring: "new users are active; an owner may deactivate and
         reactivate them".
     - **`events.py`.** Two `DomainEvent`s (frozen, `kw_only`, like the example in
       `shared/kernel/events.py:21-24`):
       - `InvitationIssued`, name `"identity.invitation.issued"`, field `invitation_id: UUID`.
       - `PasswordResetRequested`, name `"identity.password_reset.requested"`, field
         `reset_id: UUID`.
     - **`invitation.py`.** `Invitation(AggregateRoot)` with the fields `id`, `email: Email`,
       `name: DisplayName`, `invited_by: UUID`, `created_at`, `expires_at`,
       `token_hash: str | None`, `accepted_at: datetime | None` and
       `revoked_at: datetime | None`.
       - `Invitation.issue(email, name, invited_by, *, now, ttl: timedelta)` sets
         `expires_at = now + ttl` and `token_hash=None`. It records
         `InvitationIssued(invitation_id=id)`.
       - `is_pending(now)`: not accepted, not revoked, and `now < expires_at`.
       - `attach_token(token_hash)` sets `token_hash`.
       - `accept(now)` and `revoke(now)` set their timestamp only while the invitation is
         neither accepted nor revoked.
       - The invitee's role is always `Role.STAFF` (decision 11), so there is no role field.
         Write that in the docstring.
     - **`password_reset.py`.** `PasswordReset(AggregateRoot)` with the fields `id`, `user_id`,
       `created_at`, `expires_at`, `token_hash: str | None`, `used_at: datetime | None` and
       `cancelled_at: datetime | None`.
       - `PasswordReset.request(user_id, *, now, ttl)` records
         `PasswordResetRequested(reset_id=id)`.
       - `is_pending(now)`: not used, not cancelled, and `now < expires_at`.
       - `attach_token(token_hash)`.
       - `use(now)`, only while it is neither used nor cancelled.
     - **`repositories.py`:**
       - **`UserRepository`:**
         - Add `get_for_update(user_id) -> User | None`, docstring "Lock the row until the
           transaction ends".
         - `save` now persists `is_active` as well. Update its docstring.
       - **`InvitationRepository`** (new): `add(invitation)`, `get(invitation_id)`,
         `get_by_token_hash(token_hash, *, for_update: bool = False)`, `save(invitation)`
         (persists `token_hash`, `accepted_at`, `revoked_at`), and
         `revoke_open_for_email(email: Email, *, now)`. The last one sets `revoked_at = now` on
         every invitation for that email that is neither accepted nor revoked.
       - **`PasswordResetRepository`** (new): `add(reset)`, `get(reset_id)`,
         `get_by_token_hash(token_hash, *, for_update: bool = False)`, `save(reset)` (persists
         `token_hash`, `used_at`, `cancelled_at`), and `cancel_open_for_user(user_id, *, now)`.
         The last one sets `cancelled_at = now` on every reset of that user that is neither
         used nor cancelled.
   - Observable result: `uv run just arch` is green (the domain imports only the kernel and the
     standard library).

4. **Contracts**
   - Files: `apps/api/src/fragancia_api/modules/identity/contracts.py` (modify)
   - Do: append these models. As with the existing ones, they bound sizes only:

     | Model | Fields |
     | --- | --- |
     | `InviteUserRequest` | `email: str = Field(max_length=320)`, `name: str = Field(max_length=200)` |
     | `AdminInvitation` | `id: UUID`, `email: str`, `name: str`, `created_at: datetime`, `expires_at: datetime` — docstring "A pending invitation (always for a staff account)." |
     | `AdminUser` | `id: UUID`, `email: str`, `name: str`, `role: Literal["owner", "staff"]`, `is_active: bool`, `created_at: datetime` |
     | `AcceptInvitationRequest` | `token: str = Field(max_length=128)`, `password: str = Field(max_length=1024)` |
     | `PasswordResetRequest` | `email: str = Field(max_length=320)` |
     | `ResetPasswordRequest` | `token: str = Field(max_length=128)`, `new_password: str = Field(max_length=1024)` |

   - Observable result: the schemas appear in OpenAPI after step 8.

5. **Application**
   - Files: `apps/api/src/fragancia_api/modules/identity/application/policy.py` (modify),
     `apps/api/src/fragancia_api/modules/identity/application/ports.py` (modify),
     `apps/api/src/fragancia_api/modules/identity/application/commands/log_in.py` (modify),
     `apps/api/src/fragancia_api/modules/identity/application/commands/resolve_session_actor.py` (modify),
     `apps/api/src/fragancia_api/modules/identity/application/commands/invitations.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/commands/password_reset.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/commands/user_status.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/handlers/__init__.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/handlers/account_emails.py` (create),
     `apps/api/src/fragancia_api/modules/identity/application/queries/team.py` (create)
   - Do:
     - **`policy.py`.** Add a frozen, slotted dataclass `AccountLinks` with the fields
       `admin_web_url: str`, `invitation_ttl: timedelta` and `reset_ttl: timedelta`. It has
       two methods:
       - `invitation_url(token)` returns `f"{admin_web_url}/admin/activar-cuenta#token={token}"`.
       - `reset_url(token)` returns
         `f"{admin_web_url}/admin/restablecer-contrasena#token={token}"`.
     - **`ports.py`.** Add these methods to `AccountQueries`:
       - `users() -> list[AdminUser]`: every user, `created_at` ascending, then `id`.
       - `pending_invitations(now) -> list[AdminInvitation]`: invitations that are not
         accepted, not revoked and not expired, newest `created_at` first.

       Update the `SessionTokens` docstring to "Opaque random tokens (sessions and emailed
       links): only their digest is stored."
     - **`LogIn` closes the race** (`log_in.py:108-123`). At the start of `open_session`:
       1. `current = await self._users.get_for_update(signed_in.id)`.
       2. If `current is None or not current.is_active or current.password_hash != signed_in.password_hash`,
          return `Err(InvalidCredentials())`. The transaction rolls back; the attempt stays
          counted from the first transaction.
       3. Otherwise continue unchanged.

       Add one sentence to the class docstring: "The final transaction re-reads the user under
       a row lock, so a deactivation or password reset that commits during the check wins."
     - **`ResolveSessionActor`** (`resolve_session_actor.py:46`). Build
       `Actor(id=str(user.id), is_admin=True, session_id=session.id, permissions=user.permissions)`.
     - **`commands/invitations.py`:**
       - **`InviteUser`**. Its constructor takes `users`, `invitations`, `transactions`,
         `events: EventPublisher`, `clock` and `links: AccountLinks`.
         `execute(invited_by: UUID, email: str, name: str) -> Result[UUID, DomainError]`:
         1. Validate `Email`, then `DisplayName`; the first error wins.
         2. In one `run`:
            - If `users.get_by_email` finds anyone, active or not, return `Err(EmailTaken())`.
            - `invitations.revoke_open_for_email(email, now=now)`.
            - `Invitation.issue(..., ttl=links.invitation_ttl)`, `add`.
            - `events.publish(invitation.pull_events())`.
            - Return `Ok(invitation.id)`.
       - **`RevokeInvitation`**. `execute(invitation_id) -> Result[None, DomainError]`. In one
         `run`: `get`; return `Err(InvitationNotFound())` unless it `is_pending(now)`.
         Otherwise `revoke(now)` and `save`.
       - **`AcceptInvitation`**. Its constructor takes `users`, `invitations`, `hasher`,
         `tokens`, `transactions` and `clock`.
         `execute(token: str, password: str) -> Result[UUID, DomainError]`:
         1. Validate `PlainPassword` first.
         2. `digest = tokens.digest(token)`. In one `run`:
            `get_by_token_hash(digest)`; if it is missing or not `is_pending(now)`, return
            `Err(LinkInvalid())`. This check comes before hashing, so random tokens never cost
            an argon2 hash.
         3. `password_hash = await hasher.hash(...)`, outside any transaction.
         4. In one `run`:
            - `get_by_token_hash(digest, for_update=True)`; if it is no longer pending, return
              `Err(LinkInvalid())`. The lock makes the link single-use under concurrency.
            - `User.create(invitation.email, invitation.name, Role.STAFF, password_hash, now=now)`.
            - `users.add`. On `Err(EmailTaken)`, return it.
            - `invitation.accept(now)`, `save`, and return `Ok(user.id)`.
     - **`commands/password_reset.py`:**
       - **`RequestPasswordReset`**. Its constructor takes `users`, `resets`, `throttle`,
         `transactions`, `events`, `clock`, `policy: AuthPolicy` and `links: AccountLinks`.
         `execute(email: str, *, ip: str | None) -> Result[None, DomainError]`:
         1. Build the keys. `email_key` is `"reset-" + _email_key(email)`: import `_email_key`
            from `log_in.py`, rename it to `email_throttle_key` there, and update its one call
            site (`log_in.py:70`). `ip_key` is `f"reset-ip:{ip or 'unknown'}"`.
         2. Count both keys in one `run`, always returning `Ok`, exactly like
            `log_in.py:73-86`. Above `policy.email_max_attempts` or `policy.ip_max_attempts`,
            return `Err(TooManyAttempts())`.
         3. In one `run`:
            - If `Email.create(email)` fails, or the user is missing or inactive, return
              `Ok(None)`. Nothing is sent, and the answer is the same.
            - Otherwise `resets.cancel_open_for_user(user.id, now=now)`.
            - `PasswordReset.request(user.id, now=now, ttl=links.reset_ttl)`, `add`, and
              `events.publish(reset.pull_events())`.
            - Return `Ok(None)`.
       - **`ResetPassword`**. Its constructor takes `users`, `sessions`, `resets`, `hasher`,
         `tokens`, `transactions` and `clock`.
         `execute(token: str, new_password: str) -> Result[None, DomainError]`:
         1. Validate `PlainPassword`.
         2. `digest = tokens.digest(token)`. In one `run`: `get_by_token_hash(digest)`. If it
            is missing or not pending, return `Err(LinkInvalid())`.
         3. Hash outside any transaction.
         4. In one `run`:
            - Reload the reset `for_update=True`; if it is not pending, return
              `Err(LinkInvalid())`.
            - `user = users.get_for_update(reset.user_id)`; if it is missing or inactive,
              return `Err(LinkInvalid())`.
            - `user.change_password(hash, now=now)` and `save`.
            - `reset.use(now)` and `save`.
            - `sessions.revoke_all_for_user(user.id, except_id=None, now=now)` (decision 10).
            - Return `Ok(None)`.
     - **`commands/user_status.py`:**
       - **`DeactivateUser`**. Its constructor takes `users`, `sessions`, `resets`,
         `transactions` and `clock`.
         `execute(actor_id: UUID, user_id: UUID) -> Result[None, DomainError]`:
         1. If `actor_id == user_id`, return `Err(CannotDeactivateSelf())` (decision 12).
            Because the actor can never deactivate themselves, at least one active owner always
            remains.
         2. In one `run`:
            - `users.get_for_update(user_id)`; if it is missing, return `Err(UserNotFound())`.
            - `user.deactivate()` and `save`.
            - `sessions.revoke_all_for_user(user_id, except_id=None, now=now)`.
            - `resets.cancel_open_for_user(user_id, now=now)`.
            - Return `Ok(None)`. This is idempotent.
       - **`ReactivateUser`**. `execute(user_id)`. In one `run`: `get_for_update`; if it is
         missing, return `Err(UserNotFound())`. Otherwise `reactivate()`, `save`, and return
         `Ok(None)` (decision 13).
     - **`handlers/account_emails.py`.** These are outbox subscribers. Each is a class with
       `async def __call__(self, message: EventMessage) -> None`. They must be idempotent:
       delivery is at-least-once (`shared/application/events.py:29-33`).
       - **`SendInvitationEmail`**. Its constructor takes `invitations`, `tokens`,
         `email: EmailSender`, `transactions`, `clock` and `links`.
         1. Read `invitation_id = UUID(str(message.payload["invitation_id"]))`.
         2. In one `run`: `get`. If it is missing or not `is_pending(now)`, return `Ok(None)`.
            Nothing is sent: it was revoked, accepted or expired.
         3. Otherwise `token = tokens.new()`, `attach_token(tokens.digest(token))` and `save`.
         4. Then `await email.send(EmailMessage(to=invitation.email.value, subject=…, body=…))`.
         5. Return `Ok(None)`.

         Inside the relay, `run` joins the relay's transaction, so a failed send rolls back the
         digest and the event is retried. Raise any `Err` from `run` as
         `RuntimeError(error.code)`; none is expected.
       - **`SendPasswordResetEmail`**. Its constructor takes `resets`, `users`, `tokens`,
         `email`, `transactions`, `clock` and `links`. It works the same way:
         1. Load the reset; if it is not pending, do nothing.
         2. Load the user; if it is missing or inactive, do nothing.
         3. Create and attach the token, save, and send to `user.email.value`.
       - **Copy (Spanish, verbatim).** `{hours}` is `links.invitation_ttl` in whole hours and
         `{minutes}` is `links.reset_ttl` in whole minutes.
         - Invitation subject: `Te invitaron al panel de La Fragancia Ideal`
         - Invitation body:

           ```
           Hola {name}:

           Te invitaron a administrar la tienda La Fragancia Ideal. Para crear tu contraseña, abre este enlace (vence en {hours} horas y solo funciona una vez):

           {url}

           Si no esperabas este correo, ignóralo.
           ```

         - Reset subject: `Restablece tu contraseña de La Fragancia Ideal`
         - Reset body:

           ```
           Hola {name}:

           Recibimos una solicitud para restablecer tu contraseña. Abre este enlace (vence en {minutes} minutos y solo funciona una vez):

           {url}

           Al cambiarla se cerrarán todas tus sesiones abiertas. Si no la pediste, ignora este correo: tu contraseña no cambia.
           ```

     - **`queries/team.py`.** Thin wrappers over `AccountQueries`, like `my_account.py:9-33`:
       - `ListUsers.execute() -> list[AdminUser]`.
       - `ListPendingInvitations.execute() -> list[AdminInvitation]`, which passes
         `clock.now()`.
   - Observable result: `uv run just arch` is green (the application imports no FastAPI and no
     SQLAlchemy).

6. **Infrastructure and migration**
   - Files: `apps/api/src/fragancia_api/modules/identity/infrastructure/tables.py` (modify),
     `apps/api/src/fragancia_api/modules/identity/infrastructure/sql_user_repository.py` (modify),
     `apps/api/src/fragancia_api/modules/identity/infrastructure/sql_invitation_repository.py` (create),
     `apps/api/src/fragancia_api/modules/identity/infrastructure/sql_password_reset_repository.py` (create),
     `apps/api/src/fragancia_api/modules/identity/infrastructure/sql_account_queries.py` (modify),
     `apps/api/src/fragancia_api/modules/identity/infrastructure/in_memory.py` (modify),
     `apps/api/migrations/versions/0004_identity_invitations_and_password_resets.py` (create)
   - Do:
     - **Tables** in schema `identity`, after `login_throttle`:

       | Table | Columns |
       | --- | --- |
       | `invitations` | `id` uuid pk, `email` String(254) not null with an index, `name` String(80), `invited_by` uuid FK → `identity.users.id`, `created_at`, `expires_at` timestamptz, `token_hash` String(64) null unique, `accepted_at` timestamptz null, `revoked_at` timestamptz null |
       | `password_resets` | `id` uuid pk, `user_id` uuid FK → `identity.users.id` with an index, `created_at`, `expires_at` timestamptz, `token_hash` String(64) null unique, `used_at` timestamptz null, `cancelled_at` timestamptz null |

     - **`SqlUserRepository`:**
       - `get_for_update` is `self._one(select(users).where(users.c.id == user_id).with_for_update())`.
       - `save` also writes `is_active` (`sql_user_repository.py:63-68`).
     - **`SqlInvitationRepository` and `SqlPasswordResetRepository`.**
       - Explicit `_to_row` / `_to_*` mappers, like `sql_session_repository.py:12-37`.
       - `for_update=True` adds `.with_for_update()`.
       - The bulk revoke and cancel are a single `UPDATE … WHERE … IS NULL`, like
         `sql_session_repository.py:60-70`.
     - **`SqlAccountQueries`:**
       - `users()` selects the six `AdminUser` columns, ordered by `created_at, id`.
       - `pending_invitations(now)` filters `accepted_at IS NULL`, `revoked_at IS NULL` and
         `expires_at > now`, ordered by `created_at DESC, id DESC`.
       - Both use `Database.reader()`.
     - **`in_memory.py`:**
       - `InMemoryUsers.get_for_update` delegates to `get`.
       - Add `InMemoryInvitations` and `InMemoryPasswordResets` over dicts, honoring the
         same contracts.
       - `InMemoryAccountQueries.__init__` gains `invitations: InMemoryInvitations | None = None`.
         It is a keyword with a default, so the existing call `InMemoryAccountQueries(users, sessions)`
         keeps working. Implement `users()` and `pending_invitations(now)` over the fakes.
     - **Migration.** Generate it with
       `uv run just db-revision "identity invitations and password resets"`. Rename the file to
       the path above if needed, and check `down_revision = "0003"`. The downgrade drops both
       tables and their indexes and **keeps** the `identity` schema.
   - Observable result:
     - `uv run just db-migrate` creates both tables.
     - `cd apps/api && uv run alembic check` reports no differences.
     - Downgrade -1 then upgrade head works on the test database.

7. **HTTP**
   - Files: `apps/api/src/fragancia_api/modules/identity/http/router.py` (modify)
   - Do:
     - **A new router** for user management:

       ```python
       team = admin_router(
           tags=["identity · users"],
           dependencies=[Depends(require_permission(USERS_MANAGE))],
       )
       ```

       That gives paths `/api/v1/admin/users…` and `/api/v1/admin/invitations…`. Set
       `routers = (public, admin, team)`.
     - **Routes.** No logic in the bodies, and `unwrap` everywhere. Declare the error responses
       like `router.py:56-63`. Docstrings name the codes.

       | Route | Function | Behavior |
       | --- | --- | --- |
       | `POST /auth/invitations/accept` (public) | `accept_invitation` | 204. Calls `AcceptInvitation.execute(body.token, body.password)`. Errors: 422 `IDENTITY_LINK_INVALID`, `IDENTITY_PASSWORD_TOO_WEAK`; 409 `IDENTITY_EMAIL_TAKEN`. |
       | `POST /auth/password-reset` (public) | `request_password_reset` | 202 with no body, whether or not the account exists. `ip` comes from `request.client`, as in `log_in`. Error: 429 `IDENTITY_TOO_MANY_ATTEMPTS`. |
       | `POST /auth/password-reset/confirm` (public) | `reset_password` | 204. Closes every session of the user. Errors: 422 `IDENTITY_LINK_INVALID`, `IDENTITY_PASSWORD_TOO_WEAK`. |
       | `GET /admin/users` | `list_users` | `list[AdminUser]`. |
       | `POST /admin/users/{user_id}/deactivate` | `deactivate_user` | 204. Takes `current_user` and `current_session`. Errors: 404 `IDENTITY_USER_NOT_FOUND`, 422 `IDENTITY_CANNOT_DEACTIVATE_SELF`. |
       | `POST /admin/users/{user_id}/reactivate` | `reactivate_user` | 204. Takes `current_session`. Error: 404 `IDENTITY_USER_NOT_FOUND`. |
       | `GET /admin/invitations` | `list_invitations` | `list[AdminInvitation]` (pending only). |
       | `POST /admin/invitations` | `invite_user` | 201. Returns the created `AdminInvitation`, read back from `ListPendingInvitations` by id. Errors: 409 `IDENTITY_EMAIL_TAKEN`, 422 `IDENTITY_EMAIL_INVALID` / `IDENTITY_NAME_INVALID`. |
       | `DELETE /admin/invitations/{invitation_id}` | `revoke_invitation` | 204. Error: 404 `IDENTITY_INVITATION_NOT_FOUND`. |

     - **Session check on every team route.** Each team route depends on `current_session`
       (`router.py:32-38`), like the existing admin routes, so the test resolver's
       session-less actor is refused.
   - Observable result:
     - A staff session gets 403 `FORBIDDEN` on every `/admin/users*` and `/admin/invitations*`
       route.
     - `assert_admin_routes_are_protected` still passes.

8. **Wiring, worker subscription and OpenAPI**
   - Files: `apps/api/src/fragancia_api/modules/identity/module.py` (modify),
     `apps/api/openapi.json` (modify)
   - Do:
     - **`AccountLinks`.** In `register`, build it from the settings: `admin_web_url`,
       `timedelta(hours=invitation_ttl_hours)` and `timedelta(minutes=password_reset_ttl_minutes)`.
     - **Adapters.** Build `SqlInvitationRepository` and `SqlPasswordResetRepository`.
     - **Services.** Register `InviteUser`, `RevokeInvitation`, `AcceptInvitation`,
       `RequestPasswordReset`, `ResetPassword`, `DeactivateUser`, `ReactivateUser`, `ListUsers`
       and `ListPendingInvitations`.
     - **Subscriptions.** Subscribe the handlers:
       - `platform.subscriptions.subscribe(InvitationIssued.name, SendInvitationEmail(...))`.
       - `platform.subscriptions.subscribe(PasswordResetRequested.name, SendPasswordResetEmail(...))`.
       - Both use `platform.email`.
     - **OpenAPI.** Run `uv run just openapi`.
   - Observable result:
     - `apps/api/openapi.json` shows the nine new operations.
     - With `uv run just worker` running, an invitation reaches Mailpit within a few seconds.

9. **ADR and documentation**
   - Files: `docs/adr/0010-account-links-minted-at-send-time.md` (create), `docs/adr/README.md` (modify),
     `docs/architecture.md` (modify), `apps/api/README.md` (modify)
   - Do:
     - **ADR 0010** (template `docs/adr/0000-template.md`) covers:
       - Emailed links: the token is created by the outbox subscriber, and only its digest is
         stored.
       - The link lifetimes and single use.
       - Why identity sends these emails through the shared `EmailSender` and not through
         `notifications`.
       - The fragment URLs.
       - The alternatives: a token in the outbox payload, routing through `notifications`, and
         sending inside the HTTP request.
     - **ADR index.** Add the row to `docs/adr/README.md`.
     - **`docs/architecture.md:59`.** The identity row becomes "✔ Back-office users (owner,
       staff), opaque sessions in a cookie, login throttle, invitations, password reset by
       email, deactivation".
     - **`apps/api/README.md`:**
       - **Line 11.** The worker relays the outbox every 2 seconds and sends account emails.
       - **Lines 14-17.** Add one sentence: after the first owner, invite staff with
         `POST /api/v1/admin/invitations`; the email arrives in Mailpit
         (`http://127.0.0.1:8026`) while the worker runs.
       - **Lines 30-32 (Layout).**
         - The `shared/application/` line adds `EmailSender`.
         - The `shared/infrastructure/` line replaces "dev token resolver" (removed in plan
           001) with "SMTP email sender".
   - Observable result: the docs name only files, routes and commands that exist.

10. **Existing tests follow the change (implementer)**
    - Files: `apps/api/tests/unit/identity/test_resolve_session_actor.py` (modify)
    - Do: the minimum to keep the suite green. No new behavior tests here.
      - **Line 22.** Expect `permissions=user.permissions` in the `Actor`.
      - If another existing test fails only because of the new `Actor` field or the new
        `Platform.email`, stop and record it as a deviation. Do not touch unlisted files.
    - Observable result: `uv run just check` is green.

11. **Tests (tester)**
    - Files: `apps/api/tests/unit/identity/` (create), `apps/api/tests/integration/identity/` (create),
      `apps/api/tests/unit/test_settings.py` (modify), `apps/api/tests/unit/test_http_platform.py` (modify),
      `apps/api/tests/support.py` (modify)
    - Do: derived by the tester from the layers below. `support.py` may gain helpers, for
      example a resolver whose actor has `USERS_MANAGE`.
    - Observable result: `uv run just check` and `uv run just test-integration` are green.

## Acceptance criteria

Run against `uv run just api` **and** `uv run just worker`, with the development database
migrated. Mailpit's messages are read from `http://127.0.0.1:8026` (UI or `/api/v1/messages`).
Owner A comes from `just create-owner`; its cookie is in a scratchpad file.

- [ ] `POST /api/v1/admin/invitations` `{"email":"staff1@example.test","name":"Staff Uno"}` as
  owner A → 201 `AdminInvitation`. Within 10 s Mailpit holds one message to
  `staff1@example.test` with the subject `Te invitaron al panel de La Fragancia Ideal`. Its link
  is `http://localhost:4200/admin/activar-cuenta#token=…`.
- [ ] `select token_hash from identity.invitations` shows a 64-character hex digest.
  `select payload from platform.outbox where name='identity.invitation.issued'` holds only
  `invitation_id`. The token from the email appears in no table.
- [ ] Inviting the same email again → 201 and a second email. The first link's token is now
  rejected (422 `IDENTITY_LINK_INVALID`) and the second one works.
- [ ] `POST /api/v1/auth/invitations/accept` with the token and a 12+ character password → 204.
  `POST /auth/login` as staff1 → 200 with `role: "staff"` and
  `permissions: ["catalog:manage"]`. Accepting the same token again → 422
  `IDENTITY_LINK_INVALID`.
- [ ] An invitation whose `expires_at` was moved into the past (an `UPDATE` with a `WHERE id`
  on the development database) → accept gives 422 `IDENTITY_LINK_INVALID`.
- [ ] Inviting the email of an existing user → 409 `IDENTITY_EMAIL_TAKEN`.
  `DELETE /admin/invitations/{id}` on a pending one → 204, and it disappears from
  `GET /admin/invitations`. Repeating it → 404 `IDENTITY_INVITATION_NOT_FOUND`.
- [ ] As staff1, `GET /admin/users`, `POST /admin/invitations` and
  `POST /admin/users/{id}/deactivate` → 403 `FORBIDDEN`. As owner A, `GET /admin/users` lists
  both users with `is_active`.
- [ ] `POST /api/v1/auth/password-reset` `{"email":"staff1@example.test"}` → 202, and Mailpit
  gets `Restablece tu contraseña de La Fragancia Ideal` with a `restablecer-contrasena#token=`
  link. The same call for `nobody@example.test` → 202 and no email.
- [ ] With staff1 signed in on two cookie jars, `POST /auth/password-reset/confirm` with the
  token and a new password → 204. Both cookies now get 401 on `GET /admin/auth/me`. Login with
  the old password → 401; with the new one → 200. Reusing the token → 422
  `IDENTITY_LINK_INVALID`.
- [ ] A second reset request invalidates the first link (422 on the first, 204 on the second).
  After `LOGIN_EMAIL_MAX_ATTEMPTS` requests in the window, the next one → 429
  `IDENTITY_TOO_MANY_ATTEMPTS`.
- [ ] As owner A, `POST /admin/users/{staff1}/deactivate` → 204. staff1's open session now gets
  401, and staff1's login → 401 `IDENTITY_INVALID_CREDENTIALS`. A reset request for staff1 →
  202 and no email. `POST …/reactivate` → 204, and staff1 can log in again.
- [ ] Owner A deactivating themselves → 422 `IDENTITY_CANNOT_DEACTIVATE_SELF`. A second owner B
  (created with `just create-owner`) can be deactivated by A → 204. An unknown id → 404
  `IDENTITY_USER_NOT_FOUND`.
- [ ] With Mailpit stopped (`docker compose stop mailpit`, never `down -v`), an invitation is
  still 201. `platform.outbox` shows `attempts > 0` and a `last_error`. After
  `docker compose start mailpit`, the email arrives and the row is published.
- [ ] `uv run just check` and `uv run just test-integration` are green;
  `uv run just plans-scope identity-access/002` is clean.

## Test layers required

| Layer       | Applies | Focus |
| ----------- | ------- | ----- |
| domain      | yes     | `Invitation` / `PasswordReset` lifecycle (`is_pending` at the expiry boundary, single use, revoke/cancel are idempotent, events recorded once); `User.deactivate` / `reactivate` |
| application | yes     | Every new command and handler with the fakes. Invite supersedes the open invitation; accept rejects an unknown/expired/used/revoked token before hashing; reset closes ALL sessions; reset of an inactive user → `LinkInvalid`; a request for an unknown or inactive email sends nothing yet answers Ok; throttle at the limit; deactivate self → error; deactivate revokes sessions and cancels resets; handlers skip non-pending records, send through `RecordingEmailSender`, and a failing sender raises; the copy carries the fragment URL; the `LogIn` race (user deactivated or password changed between check and session → `InvalidCredentials`, no session); `ResolveSessionActor` carries permissions |
| http        | yes     | Status codes and error codes of the nine routes; 403 for an actor without `users:manage`; 202 with no body regardless of existence; `require_permission`; OpenAPI declares the cookie scheme on the new admin routes; settings validation of `ADMIN_WEB_URL` |
| integration | yes     | SQL repositories, `with_for_update` lookups, bulk revoke/cancel, queries' filters and order, the migration round trip; `SmtpEmailSender` against Mailpit only if the integration suite already reaches compose services (else mark it NOT COVERED, the verifier drives it) |
| e2e         | no      | (no e2e infrastructure yet) |

## Deviations

## Test coverage

## Review findings

## Verification
