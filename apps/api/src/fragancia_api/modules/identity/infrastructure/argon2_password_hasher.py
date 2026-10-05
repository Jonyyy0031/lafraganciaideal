import asyncio
import secrets

import argon2
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


class Argon2PasswordHasher:
    """argon2id with the library's default parameters. Hashing and verifying run in a worker
    thread (`asyncio.to_thread`), so the event loop keeps serving other requests meanwhile."""

    def __init__(self) -> None:
        self._hasher = argon2.PasswordHasher()
        self.dummy_hash = self._hasher.hash(secrets.token_urlsafe(32))

    async def hash(self, password: str) -> str:
        return await asyncio.to_thread(self._hasher.hash, password)

    async def verify(self, password_hash: str, password: str) -> bool:
        return await asyncio.to_thread(self._verify, password_hash, password)

    def _verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except VerifyMismatchError, VerificationError, InvalidHashError:
            return False
