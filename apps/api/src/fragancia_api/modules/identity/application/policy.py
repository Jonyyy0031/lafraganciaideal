from dataclasses import dataclass
from datetime import timedelta

# A session's `last_seen_at` is written at most once per interval, not on every request.
TOUCH_INTERVAL = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class AuthPolicy:
    """Session lifetimes and login throttle limits (built from settings by `module.py`)."""

    session_idle: timedelta
    session_max_age: timedelta
    throttle_window: timedelta
    email_max_attempts: int
    ip_max_attempts: int
