import hashlib
import secrets


class SecureSessionTokens:
    """256 random bits per token; only the SHA-256 hex digest is stored."""

    def new(self) -> str:
        return secrets.token_urlsafe(32)

    def digest(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()
