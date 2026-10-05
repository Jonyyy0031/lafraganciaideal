from uuid import UUID

from fragancia_api.modules.identity.application.ports import PasswordHasher
from fragancia_api.modules.identity.domain.errors import EmailTaken
from fragancia_api.modules.identity.domain.repositories import UserRepository
from fragancia_api.modules.identity.domain.user import (
    DisplayName,
    Email,
    PlainPassword,
    Role,
    User,
)
from fragancia_api.shared.application.clock import Clock
from fragancia_api.shared.application.transactions import TransactionRunner
from fragancia_api.shared.kernel import DomainError, Err, Ok, Result


class CreateUser:
    """Command: register a new, active back-office user with a role."""

    def __init__(
        self,
        *,
        users: UserRepository,
        hasher: PasswordHasher,
        transactions: TransactionRunner,
        clock: Clock,
    ) -> None:
        self._users = users
        self._hasher = hasher
        self._transactions = transactions
        self._clock = clock

    async def execute(
        self, email: str, name: str, password: str, role: Role
    ) -> Result[UUID, DomainError]:
        match Email.create(email):
            case Err(invalid_email):
                return Err(invalid_email)
            case Ok(valid_email):
                pass
        match DisplayName.create(name):
            case Err(invalid_name):
                return Err(invalid_name)
            case Ok(display_name):
                pass
        match PlainPassword.create(password):
            case Err(weak):
                return Err(weak)
            case Ok(plain):
                pass

        async def find() -> Result[User | None, DomainError]:
            return Ok(await self._users.get_by_email(valid_email))

        match await self._transactions.run(find):
            case Ok(None):
                pass
            case Ok(_):
                return Err(EmailTaken())
            case Err(error):
                return Err(error)

        password_hash = await self._hasher.hash(plain.value)  # slow: outside any transaction

        async def work() -> Result[UUID, DomainError]:
            user = User.create(
                valid_email, display_name, role, password_hash, now=self._clock.now()
            )
            match await self._users.add(user):  # also catches a concurrent duplicate
                case Err(conflict):
                    return Err(conflict)
            return Ok(user.id)

        return await self._transactions.run(work)
