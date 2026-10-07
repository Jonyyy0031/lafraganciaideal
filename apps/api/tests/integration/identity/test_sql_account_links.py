"""Plan 002 against `fragancia_test`: invitations, password resets, deactivation, the row locks
and the whole emailed-link flow through the real outbox relay and the Mailpit SMTP server."""

import asyncio
import hashlib
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text

from fragancia_api.config import Settings
from fragancia_api.container import Container, build_container
from fragancia_api.modules.identity.application.commands.change_password import ChangePassword
from fragancia_api.modules.identity.application.commands.create_user import CreateUser
from fragancia_api.modules.identity.application.commands.invitations import (
    AcceptInvitation,
    InviteUser,
    RevokeInvitation,
)
from fragancia_api.modules.identity.application.commands.log_in import LogIn
from fragancia_api.modules.identity.application.commands.password_reset import (
    RequestPasswordReset,
    ResetPassword,
)
from fragancia_api.modules.identity.application.commands.user_status import (
    DeactivateUser,
    ReactivateUser,
)
from fragancia_api.modules.identity.application.policy import AuthPolicy
from fragancia_api.modules.identity.application.queries.team import (
    ListPendingInvitations,
    ListUsers,
)
from fragancia_api.modules.identity.domain.invitation import Invitation
from fragancia_api.modules.identity.domain.password_reset import PasswordReset
from fragancia_api.modules.identity.domain.user import DisplayName, Email, Role
from fragancia_api.modules.identity.infrastructure.argon2_password_hasher import (
    Argon2PasswordHasher,
)
from fragancia_api.modules.identity.infrastructure.secure_session_tokens import (
    SecureSessionTokens,
)
from fragancia_api.modules.identity.infrastructure.sql_account_queries import SqlAccountQueries
from fragancia_api.modules.identity.infrastructure.sql_invitation_repository import (
    SqlInvitationRepository,
)
from fragancia_api.modules.identity.infrastructure.sql_login_throttle import SqlLoginThrottle
from fragancia_api.modules.identity.infrastructure.sql_password_reset_repository import (
    SqlPasswordResetRepository,
)
from fragancia_api.modules.identity.infrastructure.sql_session_repository import (
    SqlSessionRepository,
)
from fragancia_api.modules.identity.infrastructure.sql_user_repository import SqlUserRepository
from fragancia_api.modules.identity.infrastructure.tables import invitations as invitations_table
from fragancia_api.modules.identity.infrastructure.tables import (
    password_resets as resets_table,
)
from fragancia_api.modules.identity.infrastructure.tables import sessions as sessions_table
from fragancia_api.modules.identity.infrastructure.tables import users as users_table
from fragancia_api.shared.application.actor import ActorResolver
from fragancia_api.shared.infrastructure.clock import SystemClock
from fragancia_api.shared.infrastructure.database import SqlTransactionRunner
from fragancia_api.shared.infrastructure.outbox import outbox
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result, new_id
from tests.integration.identity.conftest import (
    NOW,
    in_transaction,
    make_session,
    make_user,
    store_session,
    store_user,
)

pytestmark = pytest.mark.integration

TTL = timedelta(hours=72)
RESET_TTL = timedelta(minutes=60)
PASSWORD = "a long enough password"
NEW_PASSWORD = "another long enough password"
IP = "203.0.113.7"
MAILPIT_UI = f"http://127.0.0.1:{os.environ.get('MAILPIT_UI_PORT', '8026')}"
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def make_invitation(
    inviter: UUID, email: str = "staff@example.test", *, created: datetime = NOW
) -> Invitation:
    return Invitation.issue(
        Email(email),
        DisplayName("Staff Uno"),
        inviter,
        now=created,
        ttl=TTL,
    )


def make_reset(user_id: UUID) -> PasswordReset:
    return PasswordReset.request(user_id, now=NOW, ttl=RESET_TTL)


async def store_invitation(container: Container, invitation: Invitation) -> None:
    await in_transaction(
        container, lambda: SqlInvitationRepository(container.database).add(invitation)
    )


async def store_reset(container: Container, reset: PasswordReset) -> None:
    await in_transaction(
        container, lambda: SqlPasswordResetRepository(container.database).add(reset)
    )


async def _inviter(container: Container) -> UUID:
    owner = make_user("inviter@example.test")
    await store_user(container, owner)
    return owner.id


# --- users: is_active and the row lock -------------------------------------------------------


async def test_saving_a_user_persists_is_active(container: Container) -> None:
    repository = SqlUserRepository(container.database)
    user = make_user()
    await store_user(container, user)

    user.deactivate()
    await in_transaction(container, lambda: repository.save(user))
    stored = await in_transaction(container, lambda: repository.get(user.id))
    assert stored is not None and stored.is_active is False

    user.reactivate()
    await in_transaction(container, lambda: repository.save(user))
    stored = await in_transaction(container, lambda: repository.get(user.id))
    assert stored is not None and stored.is_active is True


async def test_get_for_update_finds_the_user_and_none_for_an_unknown_id(
    container: Container,
) -> None:
    repository = SqlUserRepository(container.database)
    user = make_user()
    await store_user(container, user)

    found = await in_transaction(container, lambda: repository.get_for_update(user.id))
    missing = await in_transaction(container, lambda: repository.get_for_update(new_id()))

    assert found is not None and found.id == user.id
    assert missing is None


async def _second_waits_for_the_first[T](
    container: Container,
    lock: Callable[[], Awaitable[T]],
) -> None:
    """Hold `lock()` inside one open transaction; a second transaction taking the same lock must
    not finish until the first commits."""
    runner = SqlTransactionRunner(container.database)
    first_holds = asyncio.Event()
    release_first = asyncio.Event()
    second_done = asyncio.Event()

    async def first_work() -> Result[None, DomainError]:
        await lock()
        first_holds.set()
        await release_first.wait()
        return Ok(None)

    async def second_work() -> Result[None, DomainError]:
        await lock()
        second_done.set()
        return Ok(None)

    first = asyncio.create_task(runner.run(first_work))
    await first_holds.wait()
    second = asyncio.create_task(runner.run(second_work))
    await asyncio.sleep(0.5)
    blocked = not second_done.is_set()
    release_first.set()
    await asyncio.gather(first, second)

    assert blocked, "the second transaction got the lock while the first still held it"
    assert second_done.is_set()


async def test_get_for_update_on_a_user_blocks_a_second_locker_until_commit(
    container: Container,
) -> None:
    repository = SqlUserRepository(container.database)
    user = make_user()
    await store_user(container, user)

    await _second_waits_for_the_first(container, lambda: repository.get_for_update(user.id))


async def test_a_plain_user_read_is_not_blocked_by_a_row_lock(container: Container) -> None:
    repository = SqlUserRepository(container.database)
    user = make_user()
    await store_user(container, user)
    runner = SqlTransactionRunner(container.database)
    locked = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> Result[None, DomainError]:
        await repository.get_for_update(user.id)
        locked.set()
        await release.wait()
        return Ok(None)

    task = asyncio.create_task(runner.run(holder))
    await locked.wait()
    try:
        read = await asyncio.wait_for(
            in_transaction(container, lambda: repository.get(user.id)), timeout=2
        )
    finally:
        release.set()
        await task

    assert read is not None


async def test_the_invitation_token_lookup_for_update_blocks_a_second_locker(
    container: Container,
) -> None:
    repository = SqlInvitationRepository(container.database)
    inviter = await _inviter(container)
    invitation = make_invitation(inviter)
    invitation.attach_token("a" * 64)
    await store_invitation(container, invitation)

    await _second_waits_for_the_first(
        container, lambda: repository.get_by_token_hash("a" * 64, for_update=True)
    )


async def test_the_reset_token_lookup_for_update_blocks_a_second_locker(
    container: Container,
) -> None:
    repository = SqlPasswordResetRepository(container.database)
    user = make_user()
    await store_user(container, user)
    reset = make_reset(user.id)
    reset.attach_token("b" * 64)
    await store_reset(container, reset)

    await _second_waits_for_the_first(
        container, lambda: repository.get_by_token_hash("b" * 64, for_update=True)
    )


# --- invitations -----------------------------------------------------------------------------


async def test_an_invitation_round_trips_and_is_found_by_its_token_hash(
    container: Container,
) -> None:
    repository = SqlInvitationRepository(container.database)
    inviter = await _inviter(container)
    invitation = make_invitation(inviter)
    await store_invitation(container, invitation)

    by_id = await in_transaction(container, lambda: repository.get(invitation.id))

    assert by_id is not None
    assert (by_id.email, by_id.name, by_id.invited_by) == (
        invitation.email,
        invitation.name,
        inviter,
    )
    assert (by_id.created_at, by_id.expires_at) == (NOW, NOW + TTL)
    assert (by_id.token_hash, by_id.accepted_at, by_id.revoked_at) == (None, None, None)
    assert await in_transaction(container, lambda: repository.get(new_id())) is None
    assert await in_transaction(container, lambda: repository.get_by_token_hash("c" * 64)) is None


async def test_saving_an_invitation_persists_the_token_hash_and_the_timestamps(
    container: Container,
) -> None:
    repository = SqlInvitationRepository(container.database)
    inviter = await _inviter(container)
    accepted, revoked = (
        make_invitation(inviter, "a@example.test"),
        make_invitation(inviter, "b@example.test"),
    )
    await store_invitation(container, accepted)
    await store_invitation(container, revoked)
    accepted.attach_token("d" * 64)
    accepted.accept(NOW + timedelta(hours=1))
    revoked.revoke(NOW + timedelta(hours=2))

    await in_transaction(container, lambda: repository.save(accepted))
    await in_transaction(container, lambda: repository.save(revoked))

    found = await in_transaction(container, lambda: repository.get_by_token_hash("d" * 64))
    assert found is not None
    assert (found.id, found.accepted_at, found.revoked_at) == (
        accepted.id,
        NOW + timedelta(hours=1),
        None,
    )
    other = await in_transaction(container, lambda: repository.get(revoked.id))
    assert other is not None and other.revoked_at == NOW + timedelta(hours=2)


async def test_two_invitations_cannot_share_a_token_hash_but_many_may_have_none(
    container: Container,
) -> None:
    repository = SqlInvitationRepository(container.database)
    inviter = await _inviter(container)
    first, second, third = (make_invitation(inviter, f"{n}@example.test") for n in "abc")
    for invitation in (first, second, third):
        await store_invitation(container, invitation)  # three NULL token hashes are fine
    first.attach_token("e" * 64)
    second.attach_token("e" * 64)
    await in_transaction(container, lambda: repository.save(first))

    with pytest.raises(Exception, match="token_hash"):
        await in_transaction(container, lambda: repository.save(second))


async def test_an_invitation_needs_an_existing_inviter(container: Container) -> None:
    with pytest.raises(Exception, match="invited_by|foreign key"):
        await store_invitation(container, make_invitation(new_id()))


async def test_revoking_open_invitations_for_an_email_spares_closed_ones_and_other_emails(
    container: Container,
) -> None:
    repository = SqlInvitationRepository(container.database)
    inviter = await _inviter(container)
    open_one, open_two = make_invitation(inviter), make_invitation(inviter)
    accepted, already_revoked = make_invitation(inviter), make_invitation(inviter)
    other_email = make_invitation(inviter, "other@example.test")
    first_revoked_at = NOW + timedelta(hours=1)
    accepted.accept(NOW)
    already_revoked.revoke(first_revoked_at)
    for invitation in (open_one, open_two, accepted, already_revoked, other_email):
        await store_invitation(container, invitation)

    later = NOW + timedelta(hours=5)
    await in_transaction(
        container, lambda: repository.revoke_open_for_email(Email("staff@example.test"), now=later)
    )

    async def stored(invitation: Invitation) -> Invitation:
        found = await in_transaction(container, lambda: repository.get(invitation.id))
        assert found is not None
        return found

    assert (await stored(open_one)).revoked_at == later
    assert (await stored(open_two)).revoked_at == later
    accepted_row = await stored(accepted)
    assert (accepted_row.accepted_at, accepted_row.revoked_at) == (NOW, None)
    assert (await stored(already_revoked)).revoked_at == first_revoked_at  # time kept
    assert (await stored(other_email)).revoked_at is None


# --- password resets -------------------------------------------------------------------------


async def test_a_password_reset_round_trips_and_is_found_by_its_token_hash(
    container: Container,
) -> None:
    repository = SqlPasswordResetRepository(container.database)
    user = make_user()
    await store_user(container, user)
    reset = make_reset(user.id)
    await store_reset(container, reset)
    reset.attach_token("f" * 64)
    reset.use(NOW + timedelta(minutes=5))
    await in_transaction(container, lambda: repository.save(reset))

    by_id = await in_transaction(container, lambda: repository.get(reset.id))
    by_hash = await in_transaction(container, lambda: repository.get_by_token_hash("f" * 64))

    for found in (by_id, by_hash):
        assert found is not None
        assert (found.id, found.user_id, found.created_at, found.expires_at) == (
            reset.id,
            user.id,
            NOW,
            NOW + RESET_TTL,
        )
        assert (found.token_hash, found.used_at, found.cancelled_at) == (
            "f" * 64,
            NOW + timedelta(minutes=5),
            None,
        )
    assert await in_transaction(container, lambda: repository.get(new_id())) is None


async def test_two_resets_cannot_share_a_token_hash(container: Container) -> None:
    repository = SqlPasswordResetRepository(container.database)
    user = make_user()
    await store_user(container, user)
    first, second = make_reset(user.id), make_reset(user.id)
    await store_reset(container, first)
    await store_reset(container, second)
    first.attach_token("9" * 64)
    second.attach_token("9" * 64)
    await in_transaction(container, lambda: repository.save(first))

    with pytest.raises(Exception, match="token_hash"):
        await in_transaction(container, lambda: repository.save(second))


async def test_a_reset_needs_an_existing_user(container: Container) -> None:
    with pytest.raises(Exception, match="user_id|foreign key"):
        await store_reset(container, make_reset(new_id()))


async def test_cancelling_open_resets_spares_used_ones_and_other_users(
    container: Container,
) -> None:
    repository = SqlPasswordResetRepository(container.database)
    user, other = make_user("a@example.test"), make_user("b@example.test", role=Role.STAFF)
    await store_user(container, user)
    await store_user(container, other)
    open_one, open_two, used = make_reset(user.id), make_reset(user.id), make_reset(user.id)
    theirs = make_reset(other.id)
    used.use(NOW)
    for reset in (open_one, open_two, used, theirs):
        await store_reset(container, reset)

    later = NOW + timedelta(minutes=10)
    await in_transaction(container, lambda: repository.cancel_open_for_user(user.id, now=later))

    async def stored(reset: PasswordReset) -> PasswordReset:
        found = await in_transaction(container, lambda: repository.get(reset.id))
        assert found is not None
        return found

    assert (await stored(open_one)).cancelled_at == later
    assert (await stored(open_two)).cancelled_at == later
    used_row = await stored(used)
    assert (used_row.used_at, used_row.cancelled_at) == (NOW, None)
    assert (await stored(theirs)).cancelled_at is None


# --- queries ---------------------------------------------------------------------------------


async def test_users_are_listed_oldest_first_with_their_status(container: Container) -> None:
    older = make_user("older@example.test")
    newer = make_user("newer@example.test", role=Role.STAFF)
    newer.created_at = NOW + timedelta(minutes=1)
    newer.is_active = False
    await store_user(container, newer)
    await store_user(container, older)
    await in_transaction(container, lambda: SqlUserRepository(container.database).save(newer))

    users = await SqlAccountQueries(container.database).users()

    assert [(u.email, u.role, u.is_active) for u in users] == [
        ("older@example.test", "owner", True),
        ("newer@example.test", "staff", False),
    ]
    assert "password_hash" not in users[0].model_dump()


async def test_pending_invitations_are_filtered_and_ordered_newest_first(
    container: Container,
) -> None:
    inviter = await _inviter(container)
    old = make_invitation(inviter, "old@example.test", created=NOW)
    new = make_invitation(inviter, "new@example.test", created=NOW + timedelta(minutes=1))
    revoked = make_invitation(inviter, "revoked@example.test")
    accepted = make_invitation(inviter, "accepted@example.test")
    expired = make_invitation(inviter, "expired@example.test", created=NOW - timedelta(hours=100))
    revoked.revoke(NOW)
    accepted.accept(NOW)
    for invitation in (old, new, revoked, accepted, expired):
        await store_invitation(container, invitation)
    queries = SqlAccountQueries(container.database)

    pending = await queries.pending_invitations(NOW + timedelta(hours=1))

    assert [i.email for i in pending] == ["new@example.test", "old@example.test"]
    assert set(pending[0].model_dump()) == {"id", "email", "name", "created_at", "expires_at"}
    # `expires_at > now`: still listed one second before its expiry, gone exactly at it
    just_before = await queries.pending_invitations(NOW + TTL - timedelta(seconds=1))
    at_expiry = await queries.pending_invitations(NOW + TTL)
    assert "old@example.test" in [i.email for i in just_before]
    assert [i.email for i in at_expiry] == ["new@example.test"]


# --- the whole flow: command -> outbox -> relay -> SMTP (Mailpit) -> link -> command ---------


@pytest.fixture
async def mailpit() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=MAILPIT_UI, timeout=5) as client:
        try:
            await client.get("/api/v1/info")
        except httpx.TransportError:
            pytest.skip(
                f"NOT CONFIRMED: Mailpit is not reachable at {MAILPIT_UI} (run `just up`); the "
                "emailed-link flow needs a real SMTP server"
            )
        yield client


async def _messages_to(mailpit: httpx.AsyncClient, address: str) -> list[dict[str, object]]:
    response = await mailpit.get("/api/v1/search", params={"query": f"to:{address}"})
    messages: list[dict[str, object]] = response.json()["messages"]
    return messages


async def _link_token(mailpit: httpx.AsyncClient, message: dict[str, object]) -> str:
    detail = (await mailpit.get(f"/api/v1/message/{message['ID']}")).json()
    match = re.search(r"#token=([\w-]+)", detail["Text"])
    assert match is not None, detail["Text"]
    return match.group(1)


async def _delete_messages_to(mailpit: httpx.AsyncClient, address: str) -> None:
    ids = [m["ID"] for m in await _messages_to(mailpit, address)]
    if ids:  # only the messages this test addressed to its own unique recipient
        await mailpit.request("DELETE", "/api/v1/messages", json={"IDs": ids})


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{new_id().hex[:12]}@example.test"


async def _owner_in_the_database(container: Container) -> UUID:
    owner = make_user("flow-owner@example.test")
    await store_user(container, owner)
    return owner.id


async def test_an_invitation_travels_through_the_relay_to_a_working_staff_account(
    container: Container, mailpit: httpx.AsyncClient
) -> None:
    owner_id = await _owner_in_the_database(container)
    address = _unique_email("invitee")
    try:
        invited = await container.services.get(InviteUser).execute(owner_id, address, "Staff Uno")
        assert isinstance(invited, Ok)
        async with container.database.reader() as session:
            [event] = (await session.execute(select(outbox))).mappings().all()
            before = (await session.execute(select(invitations_table))).mappings().one()
        assert event["name"] == "identity.invitation.issued"
        assert event["payload"] == {"invitation_id": str(invited.value)}  # only the id
        assert before["token_hash"] is None  # no token until the email is sent

        assert await container.relay.relay_batch() == 1

        [message] = await _messages_to(mailpit, address)
        assert message["Subject"] == "Te invitaron al panel de La Fragancia Ideal"
        token = await _link_token(mailpit, message)
        async with container.database.reader() as session:
            row = (await session.execute(select(invitations_table))).mappings().one()
            published = (await session.execute(select(outbox.c.published_at))).scalar_one()
            dump = (
                await session.execute(text("SELECT payload::text FROM platform.outbox"))
            ).scalar_one()
        assert SHA256_HEX.match(row["token_hash"])
        assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in dump and token not in str(dict(row))  # the secret is stored nowhere
        assert published is not None

        accepted = await container.services.get(AcceptInvitation).execute(token, PASSWORD)
        assert isinstance(accepted, Ok)
        login = await container.services.get(LogIn).execute(
            address, PASSWORD, ip=IP, user_agent="tests"
        )
        assert isinstance(login, Ok)
        resolver = container.services.get(ActorResolver)  # type: ignore[type-abstract]
        actor = await resolver.resolve(login.value.token)
        assert actor is not None
        assert actor.permissions == frozenset({"catalog:manage"})  # staff, not owner
        again = await container.services.get(AcceptInvitation).execute(token, PASSWORD)
        assert isinstance(again, Err) and again.error.code == "IDENTITY_LINK_INVALID"
        assert await container.services.get(ListPendingInvitations).execute() == []
    finally:
        await _delete_messages_to(mailpit, address)


async def test_inviting_again_sends_a_second_email_and_only_the_newest_link_works(
    container: Container, mailpit: httpx.AsyncClient
) -> None:
    owner_id = await _owner_in_the_database(container)
    address = _unique_email("again")
    invite = container.services.get(InviteUser)
    accept = container.services.get(AcceptInvitation)
    try:
        first = await invite.execute(owner_id, address, "Staff Uno")
        assert isinstance(first, Ok)
        await container.relay.relay_batch()
        [first_message] = await _messages_to(mailpit, address)
        first_token = await _link_token(mailpit, first_message)
        second = await invite.execute(owner_id, address, "Staff Uno")
        assert isinstance(second, Ok)

        await container.relay.relay_batch()

        messages = await _messages_to(mailpit, address)
        assert len(messages) == 2
        tokens = {await _link_token(mailpit, m) for m in messages}
        second_token = (tokens - {first_token}).pop()
        stale = await accept.execute(first_token, PASSWORD)
        fresh = await accept.execute(second_token, PASSWORD)
        assert isinstance(stale, Err) and stale.error.code == "IDENTITY_LINK_INVALID"
        assert isinstance(fresh, Ok)
    finally:
        await _delete_messages_to(mailpit, address)


async def test_a_revoked_invitation_is_delivered_without_an_email(
    container: Container, mailpit: httpx.AsyncClient
) -> None:
    owner_id = await _owner_in_the_database(container)
    address = _unique_email("revoked")
    invited = await container.services.get(InviteUser).execute(owner_id, address, "Staff Uno")
    assert isinstance(invited, Ok)
    await container.services.get(RevokeInvitation).execute(invited.value)

    assert await container.relay.relay_batch() == 1

    assert await _messages_to(mailpit, address) == []
    async with container.database.reader() as session:
        published = (await session.execute(select(outbox.c.published_at))).scalar_one()
        token_hash = (await session.execute(select(invitations_table.c.token_hash))).scalar_one()
    assert published is not None  # handled (skipped), not retried forever
    assert token_hash is None


async def test_a_password_reset_travels_through_the_relay_and_closes_every_session(
    container: Container, mailpit: httpx.AsyncClient
) -> None:
    address = _unique_email("resetter")
    created = await container.services.get(CreateUser).execute(
        address, "Staff Uno", PASSWORD, Role.STAFF
    )
    assert isinstance(created, Ok)
    log_in = container.services.get(LogIn)
    first = await log_in.execute(address, PASSWORD, ip=IP, user_agent="tests")
    second = await log_in.execute(address, PASSWORD, ip=IP, user_agent="tests")
    assert isinstance(first, Ok) and isinstance(second, Ok)
    try:
        requested = await container.services.get(RequestPasswordReset).execute(address, ip=IP)
        assert isinstance(requested, Ok)
        unknown = _unique_email("nobody")
        await container.services.get(RequestPasswordReset).execute(unknown, ip=IP)
        async with container.database.reader() as session:
            events = (await session.execute(select(outbox))).mappings().all()
        assert [e["name"] for e in events] == [
            "identity.password_reset.requested"
        ]  # not for unknown
        assert set(events[0]["payload"]) == {"reset_id"}

        await container.relay.relay_batch()

        [message] = await _messages_to(mailpit, address)
        assert message["Subject"] == "Restablece tu contraseña de La Fragancia Ideal"
        token = await _link_token(mailpit, message)
        async with container.database.reader() as session:
            digest = (await session.execute(select(resets_table.c.token_hash))).scalar_one()
        assert digest == hashlib.sha256(token.encode()).hexdigest()

        done = await container.services.get(ResetPassword).execute(token, NEW_PASSWORD)

        assert isinstance(done, Ok)
        resolver = container.services.get(ActorResolver)  # type: ignore[type-abstract]
        assert await resolver.resolve(first.value.token) is None
        assert await resolver.resolve(second.value.token) is None
        async with container.database.reader() as session:
            open_sessions = (
                await session.execute(
                    select(sessions_table.c.id).where(sessions_table.c.revoked_at.is_(None))
                )
            ).all()
        assert open_sessions == []
        old = await log_in.execute(address, PASSWORD, ip=IP, user_agent="tests")
        new = await log_in.execute(address, NEW_PASSWORD, ip=IP, user_agent="tests")
        assert isinstance(old, Err) and isinstance(new, Ok)
        reused = await container.services.get(ResetPassword).execute(token, NEW_PASSWORD)
        assert isinstance(reused, Err) and reused.error.code == "IDENTITY_LINK_INVALID"
    finally:
        await _delete_messages_to(mailpit, address)


async def test_a_failing_smtp_rolls_the_token_back_and_leaves_the_event_to_retry(
    container: Container, settings: Settings
) -> None:
    broken = build_container(settings.model_copy(update={"smtp_port": 1}))  # nothing listens
    try:
        owner_id = await _owner_in_the_database(container)
        invited = await container.services.get(InviteUser).execute(
            owner_id, _unique_email("down"), "Staff Uno"
        )
        assert isinstance(invited, Ok)

        assert await broken.relay.relay_batch() == 1

        async with container.database.reader() as session:
            row = (await session.execute(select(outbox))).mappings().one()
            token_hash = (
                await session.execute(select(invitations_table.c.token_hash))
            ).scalar_one()
        assert row["published_at"] is None
        assert row["attempts"] == 1
        assert row["last_error"]
        assert token_hash is None  # the digest was rolled back with the failed send
        pending = await container.services.get(ListPendingInvitations).execute()
        assert [i.id for i in pending] == [invited.value]
    finally:
        await broken.close()


# --- deactivation against the real tables ----------------------------------------------------


async def test_deactivating_closes_sessions_cancels_resets_and_blocks_login_until_reactivated(
    container: Container,
) -> None:
    owner_id = await _owner_in_the_database(container)
    staff = make_user("staff@example.test", role=Role.STAFF)
    await store_user(container, staff)
    session_one, session_two = make_session(staff.id, "1" * 64), make_session(staff.id, "2" * 64)
    await store_session(container, session_one)
    await store_session(container, session_two)
    reset = make_reset(staff.id)
    await store_reset(container, reset)

    deactivated = await container.services.get(DeactivateUser).execute(owner_id, staff.id)

    assert isinstance(deactivated, Ok)
    async with container.database.reader() as session:
        active = (
            await session.execute(
                select(users_table.c.is_active).where(users_table.c.id == staff.id)
            )
        ).scalar_one()
        open_sessions = (
            await session.execute(
                select(sessions_table.c.id).where(
                    sessions_table.c.user_id == staff.id, sessions_table.c.revoked_at.is_(None)
                )
            )
        ).all()
        cancelled = (
            await session.execute(
                select(resets_table.c.cancelled_at).where(resets_table.c.id == reset.id)
            )
        ).scalar_one()
    assert (active, open_sessions) == (False, [])
    assert cancelled is not None
    users = await container.services.get(ListUsers).execute()
    assert {u.email: u.is_active for u in users}["staff@example.test"] is False

    reactivated = await container.services.get(ReactivateUser).execute(staff.id)

    assert isinstance(reactivated, Ok)
    users = await container.services.get(ListUsers).execute()
    assert {u.email: u.is_active for u in users}["staff@example.test"] is True


async def test_nobody_deactivates_themselves_and_an_unknown_user_is_not_found(
    container: Container,
) -> None:
    owner_id = await _owner_in_the_database(container)
    deactivate = container.services.get(DeactivateUser)

    own = await deactivate.execute(owner_id, owner_id)
    unknown = await deactivate.execute(owner_id, new_id())

    assert isinstance(own, Err) and own.error.code == "IDENTITY_CANNOT_DEACTIVATE_SELF"
    assert isinstance(unknown, Err) and unknown.error.code == "IDENTITY_USER_NOT_FOUND"


async def test_a_deactivation_during_the_login_check_wins_over_the_login(
    container: Container,
) -> None:
    """The final login transaction re-reads the user under a row lock: a deactivation that
    commits right before it (after the password was verified) refuses the session."""
    address = _unique_email("raced")
    created = await container.services.get(CreateUser).execute(
        address, "Staff Uno", PASSWORD, Role.STAFF
    )
    assert isinstance(created, Ok)
    owner_id = await _owner_in_the_database(container)
    deactivate = container.services.get(DeactivateUser)
    staff_id = created.value

    class DeactivatingRunner(SqlTransactionRunner):
        """Commits a deactivation right before LogIn's final transaction."""

        async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
            if work.__name__ == "open_session":
                await deactivate.execute(owner_id, staff_id)
            return await super().run(work)

    database = container.database
    settings = container.settings
    racing = LogIn(
        users=SqlUserRepository(database),
        sessions=SqlSessionRepository(database),
        throttle=SqlLoginThrottle(database),
        hasher=Argon2PasswordHasher(),
        tokens=SecureSessionTokens(),
        transactions=DeactivatingRunner(database),
        clock=SystemClock(),
        policy=AuthPolicy(
            session_idle=timedelta(minutes=settings.session_idle_minutes),
            session_max_age=timedelta(hours=settings.session_max_hours),
            throttle_window=timedelta(minutes=settings.login_window_minutes),
            email_max_attempts=settings.login_email_max_attempts,
            ip_max_attempts=settings.login_ip_max_attempts,
        ),
    )

    result = await racing.execute(address, PASSWORD, ip=IP, user_agent="tests")

    assert isinstance(result, Err) and result.error.code == "IDENTITY_INVALID_CREDENTIALS"
    async with container.database.reader() as session:
        assert (await session.execute(select(sessions_table.c.id))).all() == []


@pytest.mark.skip(
    reason="NOT CONFIRMED: the 0004 round trip (downgrade -1 / upgrade head / alembic check) is "
    "not automated: downgrading fragancia_test inside the suite would race the other "
    "integration tests. The tester ran it by hand on the test database and it passed (plan 002, "
    "Test coverage)."
)
async def test_migration_0004_downgrades_and_upgrades_cleanly() -> None: ...


# --- repair round 1: lock on `get`, actor re-check, lock order -------------------------------


async def test_get_for_update_on_an_invitation_by_id_blocks_a_second_locker(
    container: Container,
) -> None:
    repository = SqlInvitationRepository(container.database)
    invitation = make_invitation(await _inviter(container))
    await store_invitation(container, invitation)

    await _second_waits_for_the_first(
        container, lambda: repository.get(invitation.id, for_update=True)
    )


async def test_get_for_update_on_a_reset_by_id_blocks_a_second_locker(
    container: Container,
) -> None:
    repository = SqlPasswordResetRepository(container.database)
    user = make_user()
    await store_user(container, user)
    reset = make_reset(user.id)
    await store_reset(container, reset)

    await _second_waits_for_the_first(container, lambda: repository.get(reset.id, for_update=True))


async def test_get_by_id_for_update_finds_the_row_and_none_for_an_unknown_id(
    container: Container,
) -> None:
    invitations = SqlInvitationRepository(container.database)
    resets = SqlPasswordResetRepository(container.database)
    invitation = make_invitation(await _inviter(container))
    await store_invitation(container, invitation)
    user = make_user("reset-owner@example.test")
    await store_user(container, user)
    reset = make_reset(user.id)
    await store_reset(container, reset)

    found_invitation = await in_transaction(
        container, lambda: invitations.get(invitation.id, for_update=True)
    )
    found_reset = await in_transaction(container, lambda: resets.get(reset.id, for_update=True))
    no_invitation = await in_transaction(
        container, lambda: invitations.get(new_id(), for_update=True)
    )
    no_reset = await in_transaction(container, lambda: resets.get(new_id(), for_update=True))

    assert found_invitation is not None and found_invitation.id == invitation.id
    assert found_reset is not None and found_reset.id == reset.id
    assert no_invitation is None and no_reset is None


async def _is_active(container: Container, user_id: UUID) -> bool:
    async with container.database.reader() as session:
        active = (
            await session.execute(
                select(users_table.c.is_active).where(users_table.c.id == user_id)
            )
        ).scalar_one()
    return bool(active)


async def test_an_inactive_actor_is_refused_on_the_real_tables(container: Container) -> None:
    actor = make_user("lost-owner@example.test")
    target = make_user("target-owner@example.test")
    await store_user(container, actor)
    await store_user(container, target)
    deactivate = container.services.get(DeactivateUser)
    await deactivate.execute(target.id, actor.id)  # the actor is out

    result = await deactivate.execute(actor.id, target.id)

    assert isinstance(result, Err) and result.error.code == "IDENTITY_ACTOR_INACTIVE"
    assert await _is_active(container, target.id) is True


async def test_two_owners_deactivating_each_other_at_once_leave_one_active(
    container: Container,
) -> None:
    first = make_user("owner-a@example.test")
    second = make_user("owner-b@example.test")
    await store_user(container, first)
    await store_user(container, second)
    deactivate = container.services.get(DeactivateUser)

    results = await asyncio.wait_for(
        asyncio.gather(
            deactivate.execute(first.id, second.id), deactivate.execute(second.id, first.id)
        ),
        timeout=10,  # a lock-order inversion would deadlock and fail here
    )

    codes = sorted("ok" if isinstance(r, Ok) else r.error.code for r in results)
    assert codes == ["IDENTITY_ACTOR_INACTIVE", "ok"]
    states = [await _is_active(container, first.id), await _is_active(container, second.id)]
    assert sorted(states) == [False, True]


async def test_confirming_a_reset_while_the_user_is_deactivated_does_not_deadlock(
    container: Container,
) -> None:
    owner_id = await _owner_in_the_database(container)
    staff = make_user("staff-race@example.test", role=Role.STAFF)
    await store_user(container, staff)
    reset = make_reset(staff.id)
    token = "token-for-the-race"
    reset.attach_token(SecureSessionTokens().digest(token))
    await store_reset(container, reset)

    confirmed, deactivated = await asyncio.wait_for(
        asyncio.gather(
            container.services.get(ResetPassword).execute(token, NEW_PASSWORD),
            container.services.get(DeactivateUser).execute(owner_id, staff.id),
        ),
        timeout=15,
    )

    assert isinstance(deactivated, Ok)
    # whichever won, the outcome is clean: the password changed, or the link was refused
    assert isinstance(confirmed, Ok) or confirmed.error.code == "IDENTITY_LINK_INVALID"
    assert await _is_active(container, staff.id) is False


# --- repair round 2: re-reads under a lock and the user-first lock order ---------------------


def _policy(container: Container) -> AuthPolicy:
    settings = container.settings
    return AuthPolicy(
        session_idle=timedelta(minutes=settings.session_idle_minutes),
        session_max_age=timedelta(hours=settings.session_max_hours),
        throttle_window=timedelta(minutes=settings.login_window_minutes),
        email_max_attempts=settings.login_email_max_attempts,
        ip_max_attempts=settings.login_ip_max_attempts,
    )


async def _password_hash(container: Container, user_id: UUID) -> str:
    async with container.database.reader() as session:
        found = (
            await session.execute(
                select(users_table.c.password_hash).where(users_table.c.id == user_id)
            )
        ).scalar_one()
    return str(found)


async def test_a_password_change_does_not_undo_a_deactivation_committed_meanwhile(
    container: Container,
) -> None:
    """`ChangePassword` re-reads the user under a lock in its final transaction and refuses an
    inactive one, instead of saving the stale object (which would reactivate the user)."""
    address = _unique_email("changer")
    created = await container.services.get(CreateUser).execute(
        address, "Staff Uno", PASSWORD, Role.STAFF
    )
    assert isinstance(created, Ok)
    staff_id = created.value
    owner_id = await _owner_in_the_database(container)
    deactivate = container.services.get(DeactivateUser)

    class DeactivatingRunner(SqlTransactionRunner):
        """Commits a deactivation right before ChangePassword's final transaction."""

        async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
            if work.__name__ == "work":
                await deactivate.execute(owner_id, staff_id)
            return await super().run(work)

    database = container.database
    racing = ChangePassword(
        users=SqlUserRepository(database),
        sessions=SqlSessionRepository(database),
        throttle=SqlLoginThrottle(database),
        hasher=Argon2PasswordHasher(),
        transactions=DeactivatingRunner(database),
        clock=SystemClock(),
        policy=_policy(container),
    )
    old_hash = await _password_hash(container, staff_id)

    result = await racing.execute(staff_id, new_id(), PASSWORD, NEW_PASSWORD)

    assert isinstance(result, Err) and result.error.code == "IDENTITY_ACTOR_INACTIVE"
    assert await _is_active(container, staff_id) is False
    assert await _password_hash(container, staff_id) == old_hash


async def test_a_reset_request_waits_for_the_user_lock_and_sees_a_deactivation(
    container: Container,
) -> None:
    """The request locks the user before touching their resets, so it queues behind whoever holds
    the user (here a deactivation) and then re-checks that the user is still active."""
    staff = make_user("staff-request@example.test", role=Role.STAFF)
    await store_user(container, staff)
    users = SqlUserRepository(container.database)
    runner = SqlTransactionRunner(container.database)
    request = container.services.get(RequestPasswordReset)
    holds, release = asyncio.Event(), asyncio.Event()

    async def holder() -> Result[None, DomainError]:
        locked = await users.get_for_update(staff.id)
        assert locked is not None
        holds.set()
        await release.wait()
        locked.deactivate()
        await users.save(locked)
        return Ok(None)

    first = asyncio.create_task(runner.run(holder))
    await holds.wait()
    second = asyncio.create_task(request.execute("staff-request@example.test", ip=IP))
    await asyncio.sleep(0.5)
    still_waiting = not second.done()
    release.set()
    await first
    answered = await asyncio.wait_for(second, timeout=10)

    assert still_waiting, "the request did not queue behind the user lock"
    assert isinstance(answered, Ok)
    async with container.database.reader() as session:
        resets = (await session.execute(select(resets_table.c.id))).all()
        queued = (await session.execute(select(outbox))).all()
    assert resets == [] and queued == []


async def test_a_reset_request_racing_a_deactivation_does_not_deadlock(
    container: Container,
) -> None:
    owner_id = await _owner_in_the_database(container)
    staff = make_user("staff-lockorder@example.test", role=Role.STAFF)
    await store_user(container, staff)
    await store_reset(container, make_reset(staff.id))  # an open reset both flows must touch

    requested, deactivated = await asyncio.wait_for(
        asyncio.gather(
            container.services.get(RequestPasswordReset).execute(
                "staff-lockorder@example.test", ip=IP
            ),
            container.services.get(DeactivateUser).execute(owner_id, staff.id),
        ),
        timeout=15,  # a lock-order inversion would deadlock and fail here
    )

    assert isinstance(requested, Ok)
    assert isinstance(deactivated, Ok)
    assert await _is_active(container, staff.id) is False


async def test_inviting_an_email_that_has_a_user_leaves_its_pending_invitation_pending(
    container: Container,
) -> None:
    """The revoke now runs before the user check; the refusal must roll it back."""
    owner_id = await _owner_in_the_database(container)
    pending = make_invitation(owner_id, "has-a-user@example.test")
    await store_invitation(container, pending)
    await store_user(container, make_user("has-a-user@example.test", role=Role.STAFF))

    result = await container.services.get(InviteUser).execute(
        owner_id, "has-a-user@example.test", "Staff Uno"
    )

    assert isinstance(result, Err) and result.error.code == "IDENTITY_EMAIL_TAKEN"
    async with container.database.reader() as session:
        rows = (await session.execute(select(invitations_table))).mappings().all()
        queued = (await session.execute(select(outbox))).all()
    assert [(row["id"], row["revoked_at"]) for row in rows] == [(pending.id, None)]
    assert queued == []


async def test_inviting_waits_for_an_acceptance_in_flight_and_then_finds_the_new_user(
    container: Container,
) -> None:
    """An acceptance holds the invitation row; the re-invite's revoke waits for it, so the user
    check that follows sees the account the acceptance created."""
    owner_id = await _owner_in_the_database(container)
    address = "accepting@example.test"
    pending = make_invitation(owner_id, address)
    pending.attach_token("c" * 64)
    await store_invitation(container, pending)
    invitations = SqlInvitationRepository(container.database)
    users = SqlUserRepository(container.database)
    runner = SqlTransactionRunner(container.database)
    invite = container.services.get(InviteUser)
    holds, release = asyncio.Event(), asyncio.Event()

    async def acceptance() -> Result[None, DomainError]:
        locked = await invitations.get_by_token_hash("c" * 64, for_update=True)
        assert locked is not None
        holds.set()
        await release.wait()
        await users.add(make_user(address, role=Role.STAFF))
        locked.accept(NOW)
        await invitations.save(locked)
        return Ok(None)

    first = asyncio.create_task(runner.run(acceptance))
    await holds.wait()
    second = asyncio.create_task(invite.execute(owner_id, address, "Staff Uno"))
    await asyncio.sleep(0.5)
    still_waiting = not second.done()
    release.set()
    await first
    result = await asyncio.wait_for(second, timeout=10)

    assert still_waiting, "the re-invite did not wait for the acceptance in flight"
    assert isinstance(result, Err) and result.error.code == "IDENTITY_EMAIL_TAKEN"
    async with container.database.reader() as session:
        rows = (await session.execute(select(invitations_table))).mappings().all()
    assert [(row["id"], row["accepted_at"] is not None) for row in rows] == [(pending.id, True)]
