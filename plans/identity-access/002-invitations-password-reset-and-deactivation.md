---
status: done
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

- Step 6 (cosmetic): migration `0004_identity_invitations_and_password_resets.py` was written by
  hand (not autogenerated) following `0003`; `alembic check` reports no differences and
  `db-migrate` / `db-migrate --test` applied it. The downgrade round trip was NOT run.
- Step 10 (cosmetic, fix forward): two existing tests failed only because of this change and
  were minimally updated, both inside the step 11 test directories: (1)
  `apps/api/tests/unit/identity/test_auth_http.py` asserts the exact set of `auth` operations; the
  three new public routes were added to the expected set; (2)
  `apps/api/tests/integration/identity/conftest.py` truncated identity tables, which now fail
  because of the new foreign keys; `identity.invitations` and `identity.password_resets` were
  added to that `TRUNCATE` (test database only). The tester should review both.
- `plans-scope` lists `plans/identity-access/001-...md` as out of scope: it was already modified
  in the worktree before this run (git status at start) and is not touched by the implementer.
- Declared but unchanged (tester's step 11): `tests/support.py`, `tests/unit/test_http_platform.py`,
  `tests/unit/test_settings.py`.
- Not exercised by the implementer: end-to-end emails via worker and Mailpit (the verifier's job).
- Step 11 (scope, accepted by the user on 2026-10-06): the tester created
  `apps/api/tests/unit/test_require_permission.py` and
  `apps/api/tests/unit/test_smtp_email_sender.py`, which step 11 does not list. They test
  `shared/` code, so they stay in `tests/unit/`.
- `plans/identity-access/001-...md` (out of scope): the user's Scalar note, committed separately
  from this plan.

### Repair round 1 (2026-10-06)

Review round 1 failed: findings 1–5 in `## Review findings` need product-code changes. The main
session moved the plan from `review` back to `implementing`. The implementer fixes all five:
finding 3 is fixed, not accepted as a limitation, and finding 4 is fixed even though it is
uncertain. Results recorded before this repair (test coverage, review round 1) are superseded
for the code it changes. After the repair the plan goes through testing → review → verify again.

Repair done (implementer, 2026-10-06):

1. Finding 1 and 2: `InvitationRepository.get` and `PasswordResetRepository.get` take
   `for_update: bool = False` (protocol, SQL and in-memory). `SendInvitationEmail` and
   `RevokeInvitation` read with `for_update=True`. `SendPasswordResetEmail` locks in the order
   user, then reset (plain `get` to find the user id, `get_for_update` on the user, then
   `get(for_update=True)` on the reset, re-checking pending).
2. Finding 3: `DeactivateUser` locks the actor's row and the target's in id order, and refuses
   with a new error `ActorInactive` (`UnauthenticatedError`, `IDENTITY_ACTOR_INACTIVE`, 401) when
   the actor is missing or no longer active. Deviation (cosmetic, fix forward): the error is new
   and is not in step 3's table; `domain/errors.py` is in the plan's file list.
3. Finding 4: `ResetPassword` now locks user first, then reset (plain token lookup to find the
   user id, `get_for_update` on the user, then the reset `for_update=True`, re-checking pending),
   the same order as `DeactivateUser` and `SendPasswordResetEmail`.
4. Finding 5: `POST /auth/password-reset` has `response_class=Response` and returns
   `Response(status_code=202)`; `openapi.json` regenerated. Deviation (cosmetic): the tester's
   strict `xfail` marker on `test_team_http.py::test_requesting_a_reset_answers_with_no_body`
   was removed (it would XPASS-fail); the test now passes. No other test was touched.
5. Not covered by new tests (the tester's next phase): concurrency of findings 1-4 and the new
   `ActorInactive` path. Existing suites still pass.

Runs: `uv run just check` green (unit 600 passed, 3 skipped; harness 684 passed),
`uv run just test-integration` 71 passed, 2 skipped, `uv run just openapi` regenerated.
`plans-scope` is unchanged: the same three out-of-scope files as before (the two accepted tester
files and plan 001's note).

### Repair round 2 (2026-10-06)

Review round 2 failed: round-2 findings 1–4 in `## Review findings`. The main session moved the
plan from `review` back to `implementing`. The user decided:

- **Finding 1 (password change undoes a deactivation):** fix it in `ChangePassword`. Its final
  transaction re-reads the user with `get_for_update` and refuses if the user is missing or
  inactive, the same way `LogIn` closes its race. Scope deviation accepted by the user on
  2026-10-06: `apps/api/src/fragancia_api/modules/identity/application/commands/change_password.py`
  (a plan 001 file) is added to this plan's files, plus its existing unit test if it needs to
  follow.
- **Finding 2 (`RequestPasswordReset` deadlock):** fix it by locking the user before cancelling
  the open resets, so every flow locks the user first.
- **Finding 3:** name `IDENTITY_ACTOR_INACTIVE` (401) in the `deactivate_user` docstring and its
  declared responses, then regenerate OpenAPI.
- **Finding 4 (re-invite racing an acceptance):** fix it too, not accepted as a limitation.

Results recorded before this repair (test coverage, review round 2) are superseded for the code
it changes. After the repair the plan goes through testing → review → verify again.

Repair 2 done (implementer, 2026-10-06):

1. Finding 1: `ChangePassword.work()` re-reads the user with `get_for_update` and returns
   `ActorInactive` (401 `IDENTITY_ACTOR_INACTIVE`) when the user is missing or inactive. It then
   changes and saves the locked object, not the one loaded before the hash. The error code is the
   existing one from repair 1 (cosmetic choice: the plan did not name an error). Only
   `change_password.py` changed in plan 001's files; no test needed to follow.
2. Finding 2: `RequestPasswordReset` locks the user (`get_for_update`, re-checking active) before
   `cancel_open_for_user`.
3. Finding 3: `deactivate_user` declares a 401 response and its docstring names
   `IDENTITY_ACTOR_INACTIVE`; OpenAPI regenerated.
4. Finding 4: `InviteUser` now calls `revoke_open_for_email` before the `get_by_email` check. The
   UPDATE waits for an acceptance in flight, so the check then sees the new account, and the `Err`
   rolls the revoke back. Behavior is otherwise unchanged.
5. Not covered by new tests (the tester's next phase): the `ChangePassword` race, the
   `RequestPasswordReset` lock order, the re-invite vs accept interleaving, and the 401 in OpenAPI.

Runs: `uv run just openapi` regenerated; `uv run just check` green (unit 603 passed, 3 skipped;
harness 684 passed); `uv run just test-integration` 77 passed, 2 skipped.

## Test coverage

Runs (tester). Baseline before writing tests: `uv run just check` green (unit 404 passed, 3
skipped; harness 684 passed) and `uv run just test-integration` 46 passed, 1 skipped. Closing:
`uv run just check` green (unit 599 passed, 3 skipped, **1 xfailed** = the GAP below; harness 684
passed), `uv run just test-integration` 71 passed, 2 skipped, `uv run just plans-lint` OK. Note:
the first closing `just check` stopped at `mypy` (5 typing errors in my new test files); I fixed
them, ran only `uv run mypy`, then repeated the full `just check`, so that is one extra full run.

New tests, by layer: domain 3 files (`test_invitation_domain.py`,
`test_password_reset_domain.py`, one test appended to `test_user_domain.py`); application 6 files
(`test_invitations.py`, `test_password_reset.py`, `test_user_status.py`,
`test_account_email_handlers.py`, `test_log_in_race.py`, `test_team_queries.py`, plus two tests
appended to `test_resolve_session_actor.py`); http 3 files (`test_team_http.py`,
`tests/unit/test_require_permission.py`, settings tests appended to `tests/unit/test_settings.py`)
and `tests/unit/test_smtp_email_sender.py` (adapter against a fake `smtplib.SMTP`); integration 1
file (`tests/integration/identity/test_sql_account_links.py`, 25 passed + 1 skipped). Helpers: the
`Identity` world in `tests/unit/identity/conftest.py` gained the new fakes, commands and handlers
(`message_of` turns a published event into the relay's `EventMessage`). `tests/support.py` and
`tests/unit/test_http_platform.py` needed no change (the plan only said they "may").

Step 10 deviations reviewed: the `test_auth_http.py` operation set and the integration `TRUNCATE`
additions are correct (the first test passes only with the three new public routes; the truncate
is needed by the new foreign keys).

| Behavior (from plan / code) | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| Invitation: `issue` sets expiry, no token, records one id-only event; `is_pending` at the expiry boundary; accept/revoke single-shot and mutually exclusive | `domain/invitation.py:42-76` | domain | `test_invitation_domain.py` (11 tests) | CONFIRMED |
| PasswordReset: same lifecycle (`use` single-shot, cancelled cannot be used, boundary, one event) | `domain/password_reset.py:36-54` | domain | `test_password_reset_domain.py` (6 tests) | CONFIRMED |
| `User.deactivate` / `reactivate` flip `is_active`, idempotent | `domain/user.py:135-139` | domain | `test_user_domain.py::test_deactivating_and_reactivating_flip_is_active_and_are_idempotent` | CONFIRMED |
| Invite: validates email then name (email error wins), refuses an email that has a user (active or not), publishes the event, supersedes the open invitation of that email only | `commands/invitations.py:49-73` | application | `test_invitations.py::test_inviting_*`, `test_an_inactive_user_still_makes_the_email_taken`, `test_the_email_error_wins_...` | CONFIRMED |
| Revoke: pending only; unknown / revoked / expired / accepted give `IDENTITY_INVITATION_NOT_FOUND` | `commands/invitations.py:90-100` | application | `test_invitations.py::test_revoking_*`, `test_an_*_invitation_cannot_be_revoked` | CONFIRMED |
| Accept: creates an active staff user with the hashed password, works once, rejects unknown/expired/revoked tokens BEFORE hashing, superseded link dead, weak password first and link stays usable, email taken | `commands/invitations.py:123-160` | application | `test_invitations.py::test_accepting_*`, `test_a_link_works_once`, `test_*_is_rejected_without_hashing` (CountingHasher) | CONFIRMED |
| Reset request: Ok for unknown / inactive / malformed email with nothing stored or published; cancels the open reset; own `reset-` throttle keys; email and IP limits give `IDENTITY_TOO_MANY_ATTEMPTS`; does not use the login allowance | `commands/password_reset.py:48-83` | application | `test_password_reset.py::test_requesting_*`, `test_an_unknown_*`, `test_the_*_limit_*`, `test_a_reset_request_does_not_consume_the_login_allowance` | CONFIRMED |
| Reset password: changes the hash, uses the reset, closes ALL sessions of that user only, single use, expiry boundary, second request kills the first link, weak password keeps the link, inactive or missing user gives `LinkInvalid` | `commands/password_reset.py:108-145` | application | `test_password_reset.py::test_resetting_*`, `test_a_link_*`, `test_an_expired_link_*`, `test_a_user_deactivated_after_the_request_*` | CONFIRMED |
| Deactivate: self refused (owner can deactivate another owner), revokes the user's sessions only, cancels open resets, blocks login, unknown id 404, idempotent; reactivate restores login but not old sessions | `commands/user_status.py:33-67` | application | `test_user_status.py` (11 tests) | CONFIRMED |
| Handlers: link is the fragment URL, Spanish copy verbatim, only the digest stored, redelivery sends a fresh token that kills the old one, skip revoked / accepted / expired / used / cancelled / unknown / inactive-user, a failing sender raises | `handlers/account_emails.py:61-142` | application | `test_account_email_handlers.py` (17 tests) | CONFIRMED |
| `LogIn` race: user deactivated, password changed or user removed between the password check and the session gives `InvalidCredentials`, no session, attempt stays counted; unchanged user still signs in | `commands/log_in.py:104-120` | application | `test_log_in_race.py` (5 tests; a transaction runner fires the change right before `open_session`) | CONFIRMED |
| `ResolveSessionActor` carries permissions (owner vs staff) | `commands/resolve_session_actor.py:46-53` | application | `test_resolve_session_actor.py` (updated step 10 test + 2 new) | CONFIRMED |
| `ListUsers` / `ListPendingInvitations` order, filters and expiry boundary (fakes) | `queries/team.py` | application | `test_team_queries.py` (5 tests) | CONFIRMED |
| `require_permission`: allowed, 403 `FORBIDDEN` without it, 401 first, 403 documented, still declared protected | `shared/http/access.py:43-50` | http | `tests/unit/test_require_permission.py` | CONFIRMED |
| Team routes: staff / permission-less / session-less actors get 403, no cookie 401, on all six routes; the six operations declare the cookie scheme and a 403 | `http/router.py:team` | http | `test_team_http.py::test_a_staff_session_*`, `test_no_cookie_*`, `test_an_admin_without_*`, `test_the_team_routes_also_need_a_session_*`, `test_the_new_operations_are_declared_*` | CONFIRMED |
| Team routes status and error codes: users list, deactivate (204 / 404 / self 422 / bad uuid 422), reactivate (204 / 404), invite (201 shape, 409, 422 email / name, payload shape), list, revoke (204 then 404) | `http/router.py` | http | `test_team_http.py` users / invitations sections | CONFIRMED |
| Public routes: accept (204, 422 link invalid, 422 weak, 409, payload shape), confirm (204 closes sessions and switches password, 422 twice, 422 weak, payload shape), no cookie needed | `http/router.py` | http | `test_team_http.py` accept / password reset sections | CONFIRMED |
| `POST /auth/password-reset`: 202, same answer for known, unknown, inactive and malformed email, 429 after the limit, payload shape | `http/router.py:211-224` | http | `test_team_http.py::test_requesting_a_reset_is_202_and_queues_the_event`, `test_the_answer_is_the_same_*`, `test_requesting_too_often_is_429` | CONFIRMED |
| `POST /auth/password-reset` answers "202 with no body" (step 7) | `http/router.py` | http | `test_team_http.py::test_requesting_a_reset_answers_with_no_body` | CONFIRMED after repair round 1 (the earlier GAP is fixed; the xfail marker was removed by the implementer) |
| Repair 1: `DeactivateUser` refuses an inactive or unknown actor with `IDENTITY_ACTOR_INACTIVE` before looking at the target, changing nothing | `commands/user_status.py:46-52` | application | `test_user_status.py::test_an_inactive_actor_*`, `test_an_unknown_actor_*`, `test_the_actor_check_comes_before_the_target_lookup` | CONFIRMED |
| Repair 1: `get(id, for_update=True)` on invitations and resets locks the row (second locker waits) and returns the row or None | `sql_invitation_repository.py`, `sql_password_reset_repository.py` | integration | `test_sql_account_links.py::test_get_for_update_on_an_invitation_by_id_*`, `..._on_a_reset_by_id_*`, `test_get_by_id_for_update_finds_*` | CONFIRMED |
| Repair 1: actor re-check on the real tables; two owners deactivating each other at once give exactly one Ok and one `IDENTITY_ACTOR_INACTIVE`, one owner stays active, no deadlock (10 s timeout) | `user_status.py:40-52` | integration | `test_an_inactive_actor_is_refused_on_the_real_tables`, `test_two_owners_deactivating_each_other_at_once_leave_one_active` | CONFIRMED |
| Repair 1: `ResetPassword` racing `DeactivateUser` finishes without deadlock or exception (link refused or password changed) | `password_reset.py:127-145` | integration | `test_confirming_a_reset_while_the_user_is_deactivated_does_not_deadlock` | CONFIRMED (a single run does not prove the absence of a deadlock; it guards the lock order) |
| Repair 1: handlers' lock-then-recheck (finding 1) and `RevokeInvitation` vs `AcceptInvitation` (finding 2) | `account_emails.py`, `invitations.py` | integration | no test | NOT CONFIRMED: only the repository lock is tested; the interleaving needs a controlled pause inside the handler that no existing seam offers. The verifier or reviewer may exercise it |
| Settings: defaults, `ADMIN_WEB_URL` scheme / trailing slash, TTL and port bounds, `SecretStr` not in repr | `config.py:62-78` | http (settings) | `tests/unit/test_settings.py` (4 test functions, 12 cases) | CONFIRMED |
| `SmtpEmailSender`: headers, UTF-8 plain-text body, starttls then login then send, login only with both credentials, errors propagate | `shared/infrastructure/smtp_email_sender.py` | unit (adapter, fake `smtplib.SMTP`) | `tests/unit/test_smtp_email_sender.py` | CONFIRMED |
| SQL: `is_active` persisted; `get_for_update` finds the row; the user, invitation-token and reset-token `FOR UPDATE` lookups block a second locker until commit while plain reads are not blocked | `sql_user_repository.py`, `sql_invitation_repository.py`, `sql_password_reset_repository.py` | integration | `test_sql_account_links.py::test_*_for_update_*`, `test_a_plain_user_read_is_not_blocked_by_a_row_lock` | CONFIRMED |
| SQL: invitation and reset round trips, `token_hash` unique (many NULLs allowed), FKs, bulk revoke / cancel spare closed rows, other emails / users and keep the original time | `sql_invitation_repository.py`, `sql_password_reset_repository.py` | integration | `test_sql_account_links.py` invitations / password resets sections | CONFIRMED |
| SQL queries: users order and no secrets; pending invitations filter (revoked, accepted, expired) and order, `expires_at > now` boundary | `sql_account_queries.py:67-95` | integration | `test_sql_account_links.py::test_users_are_listed_*`, `test_pending_invitations_are_filtered_*` | CONFIRMED |
| Whole flow against the real outbox relay and Mailpit: event carries only the id, no token before the send, digest = sha256 of the emailed token, token stored in no table nor in the outbox, accept then login as staff, link works once, "invite again" kills the first link, revoked invitation is published without an email, reset closes all sessions in the DB | `module.py` wiring, handlers, repositories | integration | `test_sql_account_links.py::test_an_invitation_travels_*`, `test_inviting_again_*`, `test_a_revoked_invitation_*`, `test_a_password_reset_travels_*` | CONFIRMED (needs `just up`; skips with `NOT CONFIRMED` if Mailpit is down; the tests delete only the messages addressed to their own unique recipients) |
| SMTP down: the digest rolls back with the failed send, outbox row keeps `attempts=1`, `last_error`, `published_at` null, invitation still pending (acceptance criterion for Mailpit stopped, without stopping it) | `outbox.py:_relay_one`, handlers | integration | `test_sql_account_links.py::test_a_failing_smtp_rolls_the_token_back_*` (a second container with `smtp_port=1`) | CONFIRMED |
| Deactivation on real tables: sessions closed, resets cancelled, `is_active` false then true, self / unknown id errors; the login race with a real row lock (a deactivation committed right before `open_session` wins) | `user_status.py`, `log_in.py` | integration | `test_sql_account_links.py::test_deactivating_closes_*`, `test_nobody_deactivates_*`, `test_a_deactivation_during_the_login_check_*` | CONFIRMED |
| Migration 0004 round trip (downgrade -1, upgrade head, `alembic check`) | `migrations/versions/0004_*.py` | integration | `test_sql_account_links.py::test_migration_0004_downgrades_and_upgrades_cleanly` (skipped) | NOT CONFIRMED in the suite (automating it would race the other integration tests); **run by hand on `fragancia_test`**: `alembic -x test=true downgrade -1`, `upgrade head`, `check` all clean ("No new upgrade operations detected") |

Repair round 1 test phase (tester, 2026-10-06). Baseline: `uv run just check` green (unit 600
passed, 3 skipped; harness 684) and `uv run just test-integration` 71 passed, 2 skipped. Closing:
`uv run just check` green (unit 603 passed, 3 skipped, no xfail left; harness 684 passed) and
`uv run just test-integration` 77 passed, 2 skipped. One extra full `check` run: the first closing
run stopped at mypy (`no-any-return` in a new helper), fixed, then repeated. New: 3 application tests
in `test_user_status.py`, 6 integration tests in `test_sql_account_links.py`. The earlier "1 xfailed"
numbers above are superseded.

Not covered: the real `just worker` process polling the relay and `just api` over HTTP (the
verifier drives them; the relay itself is covered above); rate-limit behavior across several API
processes; `SmtpEmailSender` with STARTTLS or login against a real server (Mailpit needs neither).

Repair round 2 test phase (tester, 2026-10-06). Baseline: `uv run just check` green (harness 684
passed; unit count as in repair 2) and `uv run just test-integration` 77 passed, 2 skipped.
Closing: `uv run just check` green (unit 613 passed, 3 skipped, no xfail; harness 684 passed) and
`uv run just test-integration` 82 passed, 2 skipped. Exactly two full runs. New: 10 unit tests in
`tests/unit/identity/test_account_lock_races.py`, 1 http test in `test_team_http.py`, 5 integration
tests appended to `test_sql_account_links.py`. Results recorded before this repair are superseded
for the code it changes.

| Behavior (repair 2) | Source | Layer | Test | State |
| --- | --- | --- | --- | --- |
| `ChangePassword` re-reads the user under a lock and refuses an inactive or missing one with `IDENTITY_ACTOR_INACTIVE`; hash, sessions and throttle untouched; an unchanged user still changes the password | `change_password.py` `work()` | application | `test_account_lock_races.py::test_a_user_deactivated_while_changing_*`, `test_a_user_removed_while_changing_*`, `test_a_refused_password_change_*`, `test_an_unchanged_user_changes_*` | CONFIRMED |
| Same, on the real tables: a deactivation committed right before the final transaction stays, and the password hash is unchanged | `change_password.py` `work()` | integration | `test_sql_account_links.py::test_a_password_change_does_not_undo_a_deactivation_committed_meanwhile` | CONFIRMED |
| `RequestPasswordReset` locks the user before `cancel_open_for_user` and `add`; a user deactivated between lookup and lock gets no reset and no event (same `Ok` answer); an unknown email takes no lock | `password_reset.py:70-83` | application | `test_account_lock_races.py::test_a_reset_request_locks_*`, `test_a_user_deactivated_between_*`, `test_an_unknown_email_takes_no_lock` | CONFIRMED (call order, in-memory fakes) |
| The request queues behind a held user lock and then sees the deactivation; request racing `DeactivateUser` with an open reset does not deadlock (15 s timeout) | `password_reset.py:70-83`, `user_status.py` | integration | `test_a_reset_request_waits_for_the_user_lock_and_sees_a_deactivation`, `test_a_reset_request_racing_a_deactivation_does_not_deadlock` | CONFIRMED (a single run does not prove the absence of a deadlock; the first test pins the lock order) |
| `InviteUser` revokes open invitations before the user lookup; a user that appears during the revoke makes the email taken and nothing is added or published | `invitations.py:49-73` | application | `test_account_lock_races.py::test_inviting_revokes_*`, `test_an_acceptance_that_lands_*` | CONFIRMED (call order and decision; the fakes keep no rollback) |
| A refused invite rolls the revoke back (pending invitation stays pending, no outbox row); the re-invite waits for an acceptance in flight and then returns `IDENTITY_EMAIL_TAKEN`, leaving the accepted row accepted | `invitations.py:49-73`, `sql_invitation_repository.py` | integration | `test_inviting_an_email_that_has_a_user_*`, `test_inviting_waits_for_an_acceptance_in_flight_*` | CONFIRMED |
| `deactivate_user` declares a 401 response and names `IDENTITY_ACTOR_INACTIVE` in its description | `http/router.py` `deactivate_user` | http | `test_team_http.py::test_deactivate_declares_the_401_for_a_caller_deactivated_meanwhile` | CONFIRMED |

## Review findings

Round 1 (2026-10-06, reviewer subagent). Diff reviewed: uncommitted worktree against `main`
(`feat/identity-access`); no PR exists for this plan yet.

### Checklist: FAILED (13/15 pass, 1 fail, 1 n/a)

- [ ] **`plans-scope` — FAIL.** `uv run just plans-scope` exits 1 with three out-of-scope files:
  - `apps/api/tests/unit/test_require_permission.py` and `apps/api/tests/unit/test_smtp_email_sender.py`.
    The tester created both. Step 11 declares `tests/unit/identity/`, `test_settings.py`,
    `test_http_platform.py` and `support.py`, but not new files directly under `tests/unit/`.
    Neither `## Deviations` nor `## Test coverage` records this as a deviation. Fixing it needs
    no product code: record a deviation, and the user accepts it or the files move.
  - `plans/identity-access/001-...md`: the user's own dated note (Scalar verified by hand). It
    was already in the worktree before this run, and the implementer's deviation reports it
    honestly. It is not part of this plan's diff; commit it separately.
  - The hot file `container.py` gets an import and an `email=` keyword in `Platform(...)`.
    That is not append-only, but step 2 names it explicitly, and `MODULES` is untouched. Accepted.
- [x] `uv run just check` green (live run: unit 599 passed, 3 skipped, 1 xfailed; harness 684
  passed; arch, types, lint, plans-lint, adapter drift, openapi drift test all green).
- [x] `uv run just test-integration` green (live run: 71 passed, 2 skipped).
- [x] Business rules in `domain/` (lifecycle in `Invitation`/`PasswordReset`/`User`). Routers
  contain no logic: `invite_user` reads the created invitation back, as step 7 says.
- [x] CQRS-lite: every command runs inside `TransactionRunner.run` and returns `Result`;
  `ListUsers`/`ListPendingInvitations` go through `AccountQueries` (`Database.reader()`).
  Repositories have no screen-specific methods.
- [x] Contracts in `contracts.py`; `openapi.json` matches the generator (the
  `test_openapi.py` drift test passes).
- [x] Errors are `Err(DomainError)` with `IDENTITY_*` codes; handlers raise only on an
  unexpected `Err`.
- [x] Datetimes from `Clock`, ids from `new_id()`; no money in this change.
- [x] Migration 0004 is new, `down_revision="0003"`, has no drops, has only same-schema FKs,
  and its downgrade keeps the schema. The tester ran the round trip by hand (Test coverage);
  I did not repeat it.
- [x] Every route is in `public_router`/`admin_router`; the `team` router runs `require_admin`
  and then `require_permission(USERS_MANAGE)`. Every team route also depends on `current_session`.
- [x] Wiring resolves (`test_container.py` green). Adapters are built only in `module.py`/`container.py`.
- [x] No secrets or real personal data. `SMTP_PASSWORD` is a `SecretStr` and is left out of `.env.example`.
- [x] `## Deviations` exists and is honest. I spot-checked the `test_auth_http.py` claim: the
  only change is the three new public operations added to the expected set.
- [x] Docs updated: architecture row, API README (worker, invite sentence, layout), ADR 0010 and its index.
- [n/a] PR body: no PR exists yet for this plan (PR #4 on this branch is plan 001's, merged).

### Findings

**Medium**

1. **Lost update: the email handlers can undo a revocation or cancellation that commits
   between their read and their write.**
   - Where: `application/handlers/account_emails.py:65-70` (`SendInvitationEmail`: unlocked
     `get`, then `save`) and `:116-124` (`SendPasswordResetEmail`). The write is
     `infrastructure/sql_invitation_repository.py:59-68` / `sql_password_reset_repository.py:54-63`.
   - What fails: `save` writes `token_hash`, `accepted_at` and `revoked_at` (or `used_at` and
     `cancelled_at`) from a row the handler read without a lock. Under READ COMMITTED, a
     revocation that commits between the handler's `SELECT` and its `UPDATE` is overwritten
     with `NULL`.
   - Scenario: the owner revokes an invitation, or invites the same email again
     (`revoke_open_for_email`), while the relay is delivering `InvitationIssued`. The relay
     reads the pending row. The revoke commits and the API answers 204. The relay's `UPDATE`
     then writes `revoked_at = NULL` plus a fresh `token_hash`, and the email goes out. The
     revoked invitation is pending again, its link works, and it shows up again in
     `GET /admin/invitations`. The same happens to a superseded reset (`cancel_open_for_user`
     from a second request): two working reset links at once. The reset case is still limited
     by `ResetPassword` re-checking `is_active` under the user lock.
   - Window: narrow (between two statements of one transaction), but it breaks the "revoke /
     invite again kills the link" guarantee (decision 9).
   - Repair direction (the implementer decides): read with a row lock in the handlers (e.g. a
     `for_update` flag on `get`), or make the handler's write conditional (`SET token_hash
     WHERE id=… AND accepted_at IS NULL AND revoked_at IS NULL`).

**Low**

2. **`RevokeInvitation` can overwrite a concurrent acceptance.**
   - Where: `commands/invitations.py:90-98`. `get` takes no lock, and `save` writes `accepted_at`.
   - Scenario: `AcceptInvitation` locks the row (`for_update=True`), creates the user, sets
     `accepted_at` and commits. A concurrent revoke that already read the row as pending then
     writes `accepted_at = NULL, revoked_at = now`. The staff account exists, but its
     invitation reads "revoked, never accepted". This is a data inconsistency, not an access
     problem. The same repair as finding 1 (lock on `get`) fixes it.
3. **Two owners can deactivate each other at the same moment and leave no active owner.**
   - Where: `commands/user_status.py:33-46`. The self-check compares ids only; the actor's own
     row is neither locked nor re-checked.
   - Scenario: owners A and B each send `POST /admin/users/{other}/deactivate` at the same
     time. Each transaction locks only the target row, so both commit. Nobody can manage users
     any more; recovery needs `just create-owner`.
   - Why it is a finding: the docstring and plan step 5 claim "at least one active owner
     always remains", and that is not true under concurrency. Fix: lock both rows in id order
     and refuse if the actor is no longer active. Or record the limitation as accepted.
4. **Lock-order inversion between `ResetPassword` and `DeactivateUser` (deadlock → 500).**
   This one is **uncertain**: I reasoned it from the lock order and did not reproduce it.
   - Where: `commands/password_reset.py:132-135` locks the reset row (`get_by_token_hash(...,
     for_update=True)`) and then the user row (`get_for_update`). `commands/user_status.py:39-45`
     locks the user row and then the reset rows (`cancel_open_for_user` UPDATE).
   - Scenario: a user confirms a reset at the moment an owner deactivates them. PostgreSQL
     detects the deadlock and aborts one transaction, and that request answers 500 instead of
     a clean outcome. No data is corrupted. Fix: take the locks in the same order (e.g. look up
     the reset without a lock, lock the user, then lock the reset).
5. **`POST /auth/password-reset` answers 202 with the body `null`, not an empty body.**
   - Where: `http/router.py:209-219`.
   - What fails: step 7 and the docstring promise "202 with no body". The route returns
     `None`, and FastAPI serializes it as the JSON `null` (`content-type: application/json`).
     `openapi.json` declares a 202 `application/json` response with schema `{}`, so phase 3's
     generated client types it as `unknown` instead of void.
   - The tester's strict xfail `test_team_http.py::test_requesting_a_reset_answers_with_no_body`
     pins this gap. Fix: return `Response(status_code=202)` (or set `response_class`), then
     regenerate OpenAPI and remove the xfail.

No out-of-scope discoveries; nothing was filed in `plans/findings/`.

Not verified by this review: the live worker and Mailpit flow (verifier's job); the migration
downgrade round trip (the tester reports running it by hand); findings 1–4 were reasoned from the
code and SQL semantics, not reproduced.

Status stays `review`: findings 1–5 need code changes (repair handoff to the implementer), and
the `plans-scope` failure needs a recorded deviation and the user's acceptance.

Round 2 (2026-10-06, reviewer subagent). Re-review after repair round 1. Diff reviewed:
uncommitted worktree against `main` (`feat/identity-access`); no PR yet. Round 1 is kept above
as history; its checklist and findings are superseded by this round.

### Checklist: PASSED (14/15, 1 n/a), with the scope note below

- [x] **`plans-scope`.** Exit 1, the same three out-of-scope files as round 1. All three are now
  accounted for: the two `shared/` test files are a deviation the user accepted on 2026-10-06,
  and plan 001's file is the user's note, already committed separately (`c478570`). It is not
  part of this plan's diff. Hot file `container.py`: one import plus the `email=` keyword, as
  step 2 names; `MODULES` untouched.
- [x] `uv run just check` green, run live: unit 603 passed, 3 skipped, no xfail; harness 684
  passed; lint, mypy, arch (7 contracts kept), plans-lint and adapter drift all green.
- [x] `uv run just test-integration` green, run live: 77 passed, 2 skipped.
- [x] Business rules live in `domain/`; routers have no logic.
- [x] CQRS-lite holds. The repair adds `for_update` to `get` on both repositories (protocol,
  SQL and in-memory), which is a locking flag and not a screen-specific method.
- [x] Contracts and OpenAPI. `POST /auth/password-reset` now declares a bare `202` with no
  content (`openapi.json:188-191`). The drift test passes.
- [x] Errors. The new `ActorInactive` (`UnauthenticatedError`, `IDENTITY_ACTOR_INACTIVE`) is
  recorded as a cosmetic deviation. See Low finding 3 for its route docstring.
- [x] Clock, `new_id()`; no money.
- [x] Migration 0004 unchanged since round 1.
- [x] Routes are declared, admin routes are protected, and team routes need a session.
- [x] Wiring resolves (`test_container.py` green).
- [x] No secrets or real personal data.
- [x] `## Deviations` is honest. I spot-checked repair item 3: `ResetPassword` does a plain
  token lookup, then locks the user, then locks the reset (`password_reset.py:133-141`).
- [x] Docs are unchanged since round 1 and still accurate.
- [n/a] PR body: no PR yet.

### Round 1 findings: status

- 1 (handlers lost update): **fixed.** `SendInvitationEmail` locks the invitation
  (`account_emails.py:65`). `SendPasswordResetEmail` locks the user and then the reset, and
  re-checks pending (`:116-125`).
- 2 (revoke vs accept): **fixed.** `RevokeInvitation` reads with `for_update=True`
  (`invitations.py:93`).
- 3 (two owners deactivate each other): **fixed.** Both rows are locked in a stable order and
  the actor is re-checked (`user_status.py:47-52`). An integration test covers it.
- 4 (ResetPassword vs DeactivateUser lock order): **fixed** for that pair. A related cycle
  remains; see new finding 2.
- 5 (202 body `null`): **fixed.**

### New findings

**Medium**

1. **`ChangePassword` can silently reactivate a user who was deactivated while they were
   changing their password.** This is a regression from this plan.
   - Where: `UserRepository.save` now also writes `is_active`
     (`infrastructure/sql_user_repository.py:66-75`, steps 3 and 6). `ChangePassword` (plan
     001, `application/commands/change_password.py:50-54,72-76`) reads the user in one
     transaction without a lock. It hashes outside any transaction (argon2, hundreds of ms).
     It then calls `users.save(user)` with that stale object.
   - Failure scenario:
     1. Staff U submits `PUT /admin/auth/password`. The request passes `require_admin`, loads
        U (`is_active=True`) and starts hashing.
     2. Owner A runs `POST /admin/users/{U}/deactivate`, which commits: `is_active=false`, and
        every session of U is revoked.
     3. U's `work()` runs and writes `is_active=True` along with the new hash. The API answers
        204.
     4. U is active again and can log in with the new password. The owner's deactivation is
        undone, and `GET /admin/users` shows U as active.
     Before this plan, `save` did not write `is_active`, so this could not happen. The same
     stale write also loses a password set by a concurrent `ResetPassword`, but that part
     already existed in plan 001.
   - Repair direction (the implementer decides):
     - `ChangePassword.work()` re-reads the user with `get_for_update` and refuses when the
       user is inactive.
     - Or `save` stops writing columns the caller did not change.
   - Either fix touches `change_password.py` or changes `save`'s contract. `change_password.py`
     is not in the plan's file list, so the repair needs a recorded deviation.
   - Confidence: high. The code is sequential and unambiguous. I did not reproduce it.

**Low**

2. **Deadlock between `RequestPasswordReset` and the three flows that lock the user before the
   reset. Reproduced.**
   - The "lock user, then reset" order is used by `SendPasswordResetEmail`
     (`account_emails.py:120,123`), `ResetPassword` (`password_reset.py:136,139`) and
     `DeactivateUser` (`user_status.py:48-49,59`).
   - `RequestPasswordReset` (`password_reset.py:77-79`) takes the opposite order without saying
     so:
     - `cancel_open_for_user` row-locks the user's open reset.
     - The `INSERT` into `identity.password_resets` then runs the FK check, which takes
       `FOR KEY SHARE` on the `identity.users` row.
     - `FOR KEY SHARE` conflicts with `FOR UPDATE`.
   - Scenario: a user clicks "forgot password" twice. The second request runs while the relay
     is delivering the first event. The handler holds U and waits on R1; the request holds R1
     and waits on U. PostgreSQL aborts one of them:
     - the request answers 500; or
     - the relay row is retried, which is harmless.
     The same happens when an owner deactivates the user, or the user confirms an older link,
     at that moment. No data is corrupted.
   - Reproduced on `fragancia_test` with the exact statement order, using two connections:
     `psycopg.errors.DeadlockDetected` on the request's `INSERT`. The script is
     `<scratchpad>/deadlock_repro.py`. It left one throwaway user and two reset rows in
     `fragancia_test`, which the next integration run truncates.
   - Repair direction: `RequestPasswordReset` locks the user (`get_for_update`) before
     `cancel_open_for_user`. Then every reset flow locks the user first.
3. **`deactivate_user`'s docstring and responses do not name the new
   `IDENTITY_ACTOR_INACTIVE` (401).**
   - Where: `http/router.py` `deactivate_user`.
   - Step 7 says "Docstrings name the codes". The 401 comes from the shared admin responses, but
     the new code appears nowhere in OpenAPI. Phase 3 will not know to handle it apart from an
     expired session.
   - Cosmetic; fix it with the repair above.
4. **Uncertain, informational: a re-invite racing an acceptance can leave a pending
   invitation for an email that now has a user.**
   - Where: `InviteUser` checks `get_by_email` before `revoke_open_for_email`
     (`invitations.py:49-73`).
   - Scenario: an acceptance commits between those two steps. The `UPDATE` then skips the
     accepted row, and a new invitation is created for an existing user.
   - Effect: accepting the new invitation gives 409 `IDENTITY_EMAIL_TAKEN`, and it lingers in
     `GET /admin/invitations` until it expires or is revoked. There is no access impact. I
     reasoned this and did not reproduce it. Accepting it as a limitation is reasonable.

No out-of-scope discoveries. Nothing was filed in `plans/findings/`. Finding 1 sits in a plan
001 file, but this plan's change to `save` causes it, so it belongs here.

Not verified by this review: the live worker and Mailpit flow (verifier's job); finding 1 and
finding 4 were reasoned, not reproduced.

Status stays `review`: finding 1 (and 2, 3) need product-code changes; repair handoff to the
implementer.

Round 3 (2026-10-06, reviewer subagent). Re-review after repair round 2. Diff reviewed:
uncommitted worktree against `main` (`feat/identity-access`); no PR yet. Rounds 1 and 2 are kept
above as history; their checklists and findings are superseded by this round.

### Checklist: PASSED (14/15, 1 n/a)

- [x] **`plans-scope`.** Exit 1 with four out-of-scope files, all accounted for:
  `change_password.py` (scope deviation the user accepted on 2026-10-06, repair round 2), the two
  `shared/` test files (accepted 2026-10-06), and plan 001's file (the user's note, committed
  separately in `c478570`, not part of this diff). Hot file `container.py`: one import plus the
  `email=` keyword, as step 2 names; `MODULES` untouched.
- [x] `uv run just check` green, run live: ruff clean, mypy clean (159 files), arch 7 contracts
  kept, plans OK, unit 613 passed / 3 skipped, harness 684 passed, adapter drift and compose
  config OK.
- [x] `uv run just test-integration` green, run live: 82 passed, 2 skipped.
- [x] Business rules in `domain/`; routers have no logic.
- [x] CQRS-lite holds; no screen-specific repository methods added by the repair.
- [x] Contracts and OpenAPI: the `deactivate_user` description now names
  `IDENTITY_ACTOR_INACTIVE` (`openapi.json:625`); the drift test passes.
- [x] Errors: `ChangePassword` reuses `ActorInactive` (401); deviation recorded.
- [x] Clock, `new_id()`; no money.
- [x] Migration 0004 unchanged since round 1.
- [x] Routes declared, admin routes protected, team routes need a session.
- [x] Wiring resolves (`test_container.py` green).
- [x] No secrets or real personal data.
- [x] `## Deviations` is honest. Spot-checked repair-2 item 1: `git diff main` of
  `change_password.py` shows only the `ActorInactive` import and the locked re-read in `work()`
  (`change_password.py:81-86`); plans-scope confirms no other plan 001 product file changed.
- [x] Docs unchanged since round 1 and still accurate.
- [n/a] PR body: no PR yet.

### Round 2 findings: status

- 1 (password change undoes a deactivation): **fixed.** `ChangePassword.work()` locks the user,
  refuses a missing or inactive one, and saves the locked object (`change_password.py:81-85`).
  Unit and integration tests cover it.
- 2 (`RequestPasswordReset` deadlock): **fixed.** It locks the user before
  `cancel_open_for_user` (`password_reset.py:76-81`). I re-traced every flow's lock order: all
  reset flows (request, send email, confirm, deactivate) take the user row first; the only
  multi-user locker (`DeactivateUser`) sorts its two ids; invitation flows lock only invitation
  rows (plus FK key-share on the inviter's user row, which no invitation-locking flow holds
  against). I found no remaining cycle.
- 3 (`IDENTITY_ACTOR_INACTIVE` undocumented on `deactivate_user`): **fixed.**
- 4 (re-invite racing an acceptance): **fixed.** `revoke_open_for_email` runs before the user
  check (`invitations.py:65-67`). Under READ COMMITTED the `UPDATE` waits on the acceptance's row
  lock, re-evaluates its `WHERE` (row now accepted, skipped), and the next statement's fresh
  snapshot sees the new user; the `Err` rolls the revoke back. Reasoned and covered by the
  tester's integration test.

### New findings

**Low (non-blocking, recorded for the user)**

1. **`change_my_password`'s description does not name the new `IDENTITY_ACTOR_INACTIVE` (401)**
   (`http/router.py:146-156`). Repair 2 made `ChangePassword` return it. The 401 response is
   already declared through the shared admin responses, and in this path the caller's session
   was revoked by the deactivation anyway, so the client's "session expired" handling is
   correct. I do not block on it; it is the same kind of gap as round-2 finding 3, so the user
   may want the one-line docstring and an `uv run just openapi` for consistency.

**Out of scope (filed)**

2. **A password change in flight can override a password reset that commits meanwhile.**
   `ChangePassword.work()` re-checks only `is_active`, not that the hash it verified is still
   current (`change_password.py:81-84`), unlike `LogIn`. Someone with the old password and a
   session can undo a concurrent emailed reset within the hashing window. This predates plan 002
   and the user scoped repair 2 to the inactive check, so it is filed as
   `plans/findings/identity-change-password-overrides-concurrent-reset.md`, not blocking here.
   Reasoned, not reproduced.

Not verified by this review: the live worker and Mailpit flow (verifier's job); the lock-order
analysis and round-2 finding 4's fix were reasoned from code and PostgreSQL READ COMMITTED
semantics, backed by the tester's integration tests, not reproduced by me.

All checklist items pass and no in-scope finding needs a code change: status → `verify`.

**User decisions on round 3 (2026-10-06):**

- Finding 1: fixed before verification as a cosmetic repair by the main session. The
  `change_my_password` docstring now names 401 `IDENTITY_ACTOR_INACTIVE`, and `openapi.json` was
  regenerated with `uv run just openapi`. Docstring only; no behavior change.
- Finding 2: the finding is `deferred` until its own plan exists; the user wants it planned after this plan closes.

## Verification

**2026-10-06 — main session (inline, after review round 3 and the docstring repair).**

Suites and migration:
- `uv run just check`: green. ruff clean, `Contracts: 7 kept, 0 broken`, `plans OK (8 plans, 4 findings)`, unit `613 passed, 3 skipped`, harness `684 passed`.
- `uv run just test-integration`: `82 passed, 2 skipped`.
- `uv run just db-migrate` on the dev database: clean, already at head.
- Migration round trip on `fragancia_test` (`alembic -x test=true downgrade -1` then `upgrade head`): `0004 -> 0003`, then `0003 -> 0004`. `alembic check`: `No new upgrade operations detected`.

Live run:
- The API ran on port **8110**, not 8100: another local process (`php`) holds 8100 and 8101. The command was the same `uvicorn fragancia_api.main.http:create_app --factory`, without `--reload`.
- `uv run just worker` and Mailpit were running. Two synthetic owners were created with `just create-owner`.
- The script is `verify_002.py` in the session scratchpad (httpx, Mailpit `/api/v1/search`, `just psql`). Emails carry a run suffix (`staff1-b454ed@example.test`, …).

Acceptance criteria:
- [x] Invite → 201 `AdminInvitation`. One email arrived within 10 s, with the subject `Te invitaron al panel de La Fragancia Ideal` and a link `http://localhost:4200/admin/activar-cuenta#token=…`.
- [x] `token_hash` is 64-character hex. The outbox payload is exactly `{"invitation_id": "<id>"}`. The raw token matched 0 rows across `identity.invitations`, `identity.password_resets`, `identity.sessions` and `platform.outbox`.
- [x] Re-invite → 201 and a second email. The first link → 422 `IDENTITY_LINK_INVALID`; the second link works.
- [x] Accept → 204. The staff1 login → 200 with `"role":"staff","permissions":["catalog:manage"]` (inside `user`). Accepting again → 422 `IDENTITY_LINK_INVALID`.
- [x] An expired invitation (`UPDATE … SET expires_at = now() - 1 min WHERE id = …`) → 422 `IDENTITY_LINK_INVALID`.
- [x] Inviting an existing user's email → 409 `IDENTITY_EMAIL_TAKEN`. Revoke → 204, and the invitation leaves the list. Revoking again → 404 `IDENTITY_INVITATION_NOT_FOUND`. A missing `name` → 422 `VALIDATION_ERROR`.
- [x] staff1 → 403 `FORBIDDEN` on `GET /admin/users`, `POST /admin/invitations` and `POST /admin/users/{id}/deactivate`. The owner's `GET /admin/users` lists both users with `is_active`.
- [x] Reset request → 202 with an empty body (`b''`). The email has the subject `Restablece tu contraseña de La Fragancia Ideal` and a `restablecer-contrasena#token=` link. An unknown email → 202 and no email.
- [x] Reset confirm → 204. Both of staff1's cookie jars → 401 on `/admin/auth/me`. The old password → 401; the new one → 200. Reusing the token → 422 `IDENTITY_LINK_INVALID`.
- [x] Two requests: the first link → 422, the second → 204. The reset throttle answered `[202, 429]`: 429 `IDENTITY_TOO_MANY_ATTEMPTS` on the 6th request in the window (limit 5).
- [x] Deactivate staff1 → 204. Its open session → 401, and its login → 401 `IDENTITY_INVALID_CREDENTIALS`. A reset request for it → 202 and no email. Reactivate → 204, and the login works again.
- [x] Self-deactivation → 422 `IDENTITY_CANNOT_DEACTIVATE_SELF`. Owner B deactivated by A → 204. An unknown id → 404 `IDENTITY_USER_NOT_FOUND`.
- [x] Mailpit stopped (`docker compose stop mailpit`): invite → 201. After 8 s the outbox row showed `attempts=2`, unpublished, `last_error=ConnectionRefusedError(111, 'Connection refused')`. After `docker compose start mailpit` the email arrived and the row was published.
- [x] `check` and `test-integration` are green. `plans-scope` exits 1 on four files, all accounted for: `change_password.py` (user-accepted, repair 2), the two `shared/` test files (user-accepted), and plan 001's note (committed in `c478570`).

NOT VERIFIED:
- The concurrency fixes (findings round 1 1–4 and round 2 1, 2 and 4) were not driven live. Races cannot be timed reliably from HTTP; the tester's integration tests cover them against real PostgreSQL locks.
- SMTP with STARTTLS or with login against a real provider. Mailpit needs neither.
- The `IDENTITY_ACTOR_INACTIVE` paths over HTTP. They are unit- and integration-tested only.

Side effects:
- The dev database keeps the synthetic run-`b454ed` users (owners A and B, staff1), their invitations (staff1–4, of which staff2 is expired and staff3 revoked), resets and outbox rows.
- Mailpit keeps the run's messages.
- Nothing was deleted.

All 14 acceptance criteria pass. Status stays `verify`; the user sets `done`.
