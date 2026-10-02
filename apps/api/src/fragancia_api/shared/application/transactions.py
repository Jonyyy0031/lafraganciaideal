from collections.abc import Awaitable, Callable
from typing import Protocol

from fragancia_api.shared.kernel import Result


class TransactionRunner(Protocol):
    """Runs a unit of work atomically.

    Commits when `work` returns `Ok`, rolls back when it returns `Err` or raises. Repositories
    and the event publisher join the active transaction without receiving it as a parameter.
    Nested calls join the outer transaction.
    """

    async def run[T, E](self, work: Callable[[], Awaitable[Result[T, E]]]) -> Result[T, E]: ...
