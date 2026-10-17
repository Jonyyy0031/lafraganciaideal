import base64
import hashlib
import hmac
from uuid import UUID


class HmacOrderLinks:
    """The private-link token is HMAC-SHA256(secret, `order-link:` + order id), base64url without
    padding (decision 31). It is derived, so the confirmation email can regenerate it; only its
    SHA-256 hex digest is stored. Rotating the secret breaks every link already sent."""

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode()

    def token(self, order_id: UUID) -> str:
        mac = hmac.new(self._secret, b"order-link:" + order_id.bytes, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()

    def digest(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()
