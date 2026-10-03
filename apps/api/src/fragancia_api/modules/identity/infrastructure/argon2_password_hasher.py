import secrets

import argon2
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


class Argon2PasswordHasher:
    """argon2id with the library's default parameters."""

    def __init__(self) -> None:
        self._hasher = argon2.PasswordHasher()
        self.dummy_hash = self._hasher.hash(secrets.token_urlsafe(32))

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except VerifyMismatchError, VerificationError, InvalidHashError:
            return False
