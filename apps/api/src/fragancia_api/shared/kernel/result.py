"""Result type for expected outcomes.

Expected failures (validation, business rules, not found, conflicts) are returned as
`Err(DomainError)`; exceptions are reserved for the unexpected. Consume with `match`:

    match await create_brand.execute(name):
        case Ok(brand_id): ...
        case Err(error): ...
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Ok[T]:
    value: T


@dataclass(frozen=True, slots=True)
class Err[E]:
    error: E


type Result[T, E] = Ok[T] | Err[E]
