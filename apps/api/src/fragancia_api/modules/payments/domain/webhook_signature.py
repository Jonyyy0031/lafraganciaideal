"""Mercado Pago's webhook signature, as the official SDK validates it (no timestamp tolerance).

The header is `x-signature: ts=<seconds>,v1=<hex>`. The signed manifest is
`id:<data.id>;request-id:<x-request-id>;ts:<ts>;`, where each of the first two pairs is present
only when its value is, and `data.id` comes from the query string. `v1` is the HMAC-SHA256 hex
of the manifest with the application's webhook secret.
"""

import hashlib
import hmac


def _parts(x_signature: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for part in x_signature.split(","):
        key, separator, value = part.partition("=")
        if separator:
            parts[key.strip().lower()] = value
    return parts


def signature_valid(
    secret: str, x_signature: str | None, x_request_id: str | None, data_id: str | None
) -> bool:
    if not x_signature:
        return False
    parts = _parts(x_signature)
    ts = parts.get("ts")
    v1 = parts.get("v1")
    if not ts or not ts.isascii() or not ts.isdigit() or not v1:
        return False
    manifest = ""
    if data_id:
        manifest += f"id:{data_id};"
    if x_request_id:
        manifest += f"request-id:{x_request_id};"
    manifest += f"ts:{ts};"
    expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    # Bytes, so a non-ASCII header is a mismatch rather than a TypeError.
    return hmac.compare_digest(expected.encode(), v1.encode())
