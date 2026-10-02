from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

type Currency = Literal["MXN"]


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """An amount in integer cents. Only MXN for now; mixing currencies is a programming error."""

    cents: int
    currency: Currency = "MXN"

    @classmethod
    def from_decimal(cls, amount: Decimal | str, currency: Currency = "MXN") -> Money:
        cents = (Decimal(amount) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return cls(int(cents), currency)

    def to_decimal(self) -> Decimal:
        return Decimal(self.cents) / 100

    def __add__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.cents + other.cents, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.cents - other.cents, self.currency)

    def times(self, quantity: int) -> Money:
        return Money(self.cents * quantity, self.currency)

    def _same_currency(self, other: Money) -> None:
        if other.currency != self.currency:
            raise ValueError(f"Cannot combine {self.currency} and {other.currency}")
