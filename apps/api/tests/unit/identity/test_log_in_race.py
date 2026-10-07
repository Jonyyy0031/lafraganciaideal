"""The final login transaction re-reads the user: a deactivation or password change that commits
after the password check, right before the session opens, wins."""

from collections.abc import Awaitable, Callable

from fragancia_api.modules.identity.application.commands.log_in import LoginResult
from fragancia_api.modules.identity.domain.user import User
from fragancia_api.shared.infrastructure.in_memory import InMemoryTransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result
from tests.unit.identity.conftest import PASSWORD, Identity

OWNER = "owner@example.test"


class RacingTransactions(InMemoryTransactionRunner):
    """Runs `before_open_session` after every earlier step of `LogIn` and right before its final
    transaction (the unit of work named `open_session`), as a concurrent request would."""

    def __init__(self) -> None:
        self.before_open_session: Callable[[], Awaitable[None]] | None = None

    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]:
        if work.__name__ == "open_session" and self.before_open_session is not None:
            await self.before_open_session()
        return await work()


async def _attempt(identity: Identity) -> Result[LoginResult, DomainError]:
    return await identity.log_in.execute(OWNER, PASSWORD, ip="203.0.113.7", user_agent="tests")


async def test_a_user_deactivated_after_the_check_gets_no_session() -> None:
    transactions = RacingTransactions()
    identity = Identity(transactions=transactions)
    user = identity.add_user()

    async def deactivate() -> None:
        user.deactivate()

    transactions.before_open_session = deactivate

    result = await _attempt(identity)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_INVALID_CREDENTIALS"
    assert identity.sessions.by_id == {}


async def test_a_password_changed_after_the_check_gets_no_session() -> None:
    transactions = RacingTransactions()
    identity = Identity(transactions=transactions)
    user = identity.add_user()

    async def reset_password() -> None:
        identity.users.by_id[user.id] = User(
            id=user.id,
            email=user.email,
            name=user.name,
            role=user.role,
            password_hash=identity.hasher.encode("someone reset it"),
            is_active=True,
            created_at=user.created_at,
            password_changed_at=identity.clock.now(),
        )

    transactions.before_open_session = reset_password

    result = await _attempt(identity)

    assert isinstance(result, Err)
    assert result.error.code == "IDENTITY_INVALID_CREDENTIALS"
    assert identity.sessions.by_id == {}


async def test_a_user_removed_after_the_check_gets_no_session() -> None:
    transactions = RacingTransactions()
    identity = Identity(transactions=transactions)
    user = identity.add_user()

    async def remove() -> None:
        del identity.users.by_id[user.id]

    transactions.before_open_session = remove

    result = await _attempt(identity)

    assert isinstance(result, Err)
    assert identity.sessions.by_id == {}


async def test_a_rejected_race_keeps_the_attempt_counted_and_clears_nothing() -> None:
    transactions = RacingTransactions()
    identity = Identity(transactions=transactions)
    user = identity.add_user()

    async def deactivate() -> None:
        user.deactivate()

    transactions.before_open_session = deactivate

    await _attempt(identity)

    assert identity.throttle.counts[f"email:{OWNER}"][0] == 1
    assert identity.throttle.counts["ip:203.0.113.7"][0] == 1


async def test_an_unchanged_user_still_signs_in_through_the_recheck() -> None:
    transactions = RacingTransactions()
    identity = Identity(transactions=transactions)
    identity.add_user()

    async def nothing() -> None:
        return None

    transactions.before_open_session = nothing

    result = await _attempt(identity)

    assert isinstance(result, Ok)
    assert len(identity.sessions.by_id) == 1
