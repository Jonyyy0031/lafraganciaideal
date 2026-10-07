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


@dataclass(frozen=True, slots=True)
class AccountLinks:
    """Where emailed links point and how long they live (built from settings by `module.py`).
    The token goes in the URL fragment so it never reaches access logs or `Referer`."""

    admin_web_url: str
    invitation_ttl: timedelta
    reset_ttl: timedelta

    def invitation_url(self, token: str) -> str:
        return f"{self.admin_web_url}/admin/activar-cuenta#token={token}"

    def reset_url(self, token: str) -> str:
        return f"{self.admin_web_url}/admin/restablecer-contrasena#token={token}"
