"""The bounds of the back-office session and throttle settings (plan 001, step 1)."""

import pytest
from pydantic import ValidationError

from tests.support import make_settings

BOUNDED = [
    "session_idle_minutes",
    "session_max_hours",
    "login_email_max_attempts",
    "login_ip_max_attempts",
    "login_window_minutes",
]


@pytest.mark.parametrize("name", BOUNDED)
@pytest.mark.parametrize("value", [0, -1])
def test_a_session_or_throttle_setting_below_one_is_refused(name: str, value: int) -> None:
    with pytest.raises(ValidationError, match=name):
        make_settings(**{name: value})


@pytest.mark.parametrize("name", BOUNDED)
def test_a_session_or_throttle_setting_of_one_is_accepted(name: str) -> None:
    assert getattr(make_settings(**{name: 1}), name) == 1


def test_the_defaults_are_the_ones_the_plan_chose() -> None:
    settings = make_settings()

    assert settings.session_idle_minutes == 120
    assert settings.session_max_hours == 12
    assert settings.login_email_max_attempts == 5
    assert settings.login_ip_max_attempts == 50
    assert settings.login_window_minutes == 15
