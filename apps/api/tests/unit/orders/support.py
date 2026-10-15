"""Builders shared by the orders unit tests."""

from fragancia_api.modules.orders.domain.delivery import DeliverySettings
from fragancia_api.shared.kernel import Err, Ok


def build_settings(**overrides: object) -> DeliverySettings:
    """Valid settings (everything on) with the given `DeliverySettings.create` fields replaced."""
    values: dict[str, object] = {
        "home_enabled": True,
        "home_price_cents": 18000,
        "home_free_from_cents": 250000,
        "local_enabled": True,
        "local_price_cents": 9000,
        "local_postal_codes": ["77500", "77505"],
        "pickup_enabled": True,
        "pickup_address": "Calle 1 #2, Cancun",
        "pickup_hours": "Lun-Vie 10-18",
    }
    values.update(overrides)
    result = DeliverySettings.create(**values)  # type: ignore[arg-type]
    match result:
        case Ok(settings):
            return settings
        case Err(error):
            raise AssertionError(f"the test settings are invalid: {error}")
