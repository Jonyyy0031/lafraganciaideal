from dataclasses import dataclass

from fastapi import Response

CART_COOKIE = "fragancia_cart"
COOKIE_PATH = "/api/v1"


@dataclass(frozen=True, slots=True)
class CartCookie:
    """The cart token's only transport: httpOnly, `SameSite=Lax` (so the cart survives a link
    from WhatsApp or email), Secure in production. Refreshed on every write."""

    secure: bool
    max_age_seconds: int

    def set(self, response: Response, token: str) -> None:
        response.set_cookie(
            CART_COOKIE,
            token,
            max_age=self.max_age_seconds,
            path=COOKIE_PATH,
            httponly=True,
            samesite="lax",
            secure=self.secure,
        )
