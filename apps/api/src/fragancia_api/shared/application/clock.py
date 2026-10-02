from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """The current time (UTC). Injected so tests control it."""

    def now(self) -> datetime: ...
