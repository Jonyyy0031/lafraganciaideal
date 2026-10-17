"""Builders shared by the orders unit tests."""

from datetime import UTC, datetime
from uuid import UUID

from fragancia_api.modules.orders.domain.cart import Offer
from fragancia_api.modules.orders.domain.delivery import DeliverySettings
from fragancia_api.shared.kernel import Err, Ok

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def presentation_id(n: int) -> UUID:
    """A stable, readable id per test line."""
    return UUID(int=n)


def make_offer(n: int = 1, **overrides: object) -> Offer:
    """A visible in-stock offer of 2500.00 MXN with 5 units; the given fields are replaced."""
    values: dict[str, object] = {
        "presentation_id": presentation_id(n),
        "perfume_id": UUID(int=1000 + n),
        "perfume_slug": f"perfume-{n}",
        "label": f"Brand Perfume {n} EDT 100 ml",
        "ml": 100,
        "card_url": f"http://media.test/card-{n}.webp",
        "price_cents": 250_000,
        "regular_price_cents": 250_000,
        "availability": "in_stock",
        "lead_time_min_days": None,
        "lead_time_max_days": None,
        "is_visible": True,
        "available_units": 5,
    }
    values.update(overrides)
    return Offer(**values)  # type: ignore[arg-type]


def made_to_order_offer(n: int = 1, *, max_days: int = 5, **overrides: object) -> Offer:
    """A visible made-to-order offer with a 2-day-wide lead time ending at `max_days`."""
    values: dict[str, object] = {
        "availability": "made_to_order",
        "lead_time_min_days": max(1, max_days - 2),
        "lead_time_max_days": max_days,
        "available_units": None,
    }
    values.update(overrides)
    return make_offer(n, **values)


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


class FakeOrderLinks:
    """The `OrderLinks` port without a secret: the token is `link:<order id>` and the digest is
    `digest:<token>`."""

    def token(self, order_id: UUID) -> str:
        return f"link:{order_id}"

    def digest(self, token: str) -> str:
        return f"digest:{token}"
