"""`HmacOrderLinks` (review repair of plan 003, decision 31): the private-link token is derived
from the order id with a server secret."""

import base64
import hashlib
import hmac
from uuid import UUID

from fragancia_api.modules.orders.infrastructure.order_links import HmacOrderLinks

SECRET = "s" * 32


def test_the_token_is_stable_for_one_order_id() -> None:
    links = HmacOrderLinks(SECRET)

    assert links.token(UUID(int=1)) == links.token(UUID(int=1))
    assert links.token(UUID(int=1)) == HmacOrderLinks(SECRET).token(UUID(int=1))


def test_the_token_differs_across_ids_and_secrets() -> None:
    links = HmacOrderLinks(SECRET)

    assert links.token(UUID(int=1)) != links.token(UUID(int=2))
    assert links.token(UUID(int=1)) != HmacOrderLinks("t" * 32).token(UUID(int=1))


def test_the_token_is_the_unpadded_base64url_hmac_of_the_prefixed_id() -> None:
    order_id = UUID(int=7)
    expected = hmac.new(SECRET.encode(), b"order-link:" + order_id.bytes, hashlib.sha256).digest()

    token = HmacOrderLinks(SECRET).token(order_id)

    assert token == base64.urlsafe_b64encode(expected).rstrip(b"=").decode()
    assert "=" not in token
    assert len(token) == 43


def test_the_digest_is_the_sha256_hex_of_the_token() -> None:
    links = HmacOrderLinks(SECRET)
    token = links.token(UUID(int=1))

    assert links.digest(token) == hashlib.sha256(token.encode()).hexdigest()
    assert len(links.digest(token)) == 64
