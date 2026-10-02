from datetime import datetime
from decimal import Decimal
from enum import Enum

import pytest

from fragancia_api.shared.kernel import (
    AggregateRoot,
    ConflictError,
    DomainError,
    Err,
    Money,
    Ok,
    Result,
    new_id,
)
from tests.support import SomethingHappened


def describe(result: Result[int, DomainError]) -> str:
    match result:
        case Ok(value):
            return f"ok {value}"
        case Err(error):
            return f"err {error.code}"


def test_result_is_consumed_with_match() -> None:
    assert describe(Ok(3)) == "ok 3"
    assert describe(Err(ConflictError("X_TAKEN", "taken"))) == "err X_TAKEN"


def test_concrete_errors_keep_their_category() -> None:
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Taken(ConflictError):
        code: str = "X_TAKEN"
        message: str = "Already taken"

    error = Taken()
    assert isinstance(error, ConflictError)
    assert (error.code, error.message, error.details) == ("X_TAKEN", "Already taken", None)


def test_ids_are_uuid7_and_time_ordered() -> None:
    first, second = new_id(), new_id()
    assert first.version == 7
    assert first < second


def test_aggregate_events_are_pulled_once() -> None:
    aggregate = AggregateRoot()
    event = SomethingHappened(thing_id=new_id(), label="x")
    aggregate.record(event)

    assert aggregate.pull_events() == [event]
    assert aggregate.pull_events() == []


def test_event_payload_is_json_compatible_and_excludes_the_envelope() -> None:
    class Color(Enum):
        RED = "red"

    thing_id = new_id()
    event = SomethingHappened(thing_id=thing_id, label="x")

    assert event.payload() == {"thing_id": str(thing_id), "label": "x"}
    assert event.name == "tests.something.happened"
    assert isinstance(event.occurred_at, datetime) and event.occurred_at.tzinfo is not None
    from fragancia_api.shared.kernel.events import _to_json

    assert _to_json({"c": [Color.RED, (1, 2)]}) == {"c": ["red", [1, 2]]}


def test_money_arithmetic_in_cents() -> None:
    price = Money.from_decimal("1299.995")
    assert price == Money(130000)
    assert price.to_decimal() == Decimal("1300")
    assert (Money(150) + Money(50)).cents == 200
    assert (Money(150) - Money(50)).cents == 100
    assert Money(250).times(3) == Money(750)
    assert Money(1) < Money(2)


def test_money_rejects_mixing_currencies() -> None:
    other = Money(100, "USD")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Cannot combine"):
        Money(100) + other
