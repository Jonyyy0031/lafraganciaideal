"""Regression tests for plan 001, repair round 1, M1: argon2 hashing and verifying run in a
worker thread, so the event loop keeps serving other requests."""

import asyncio
import threading
import time

import argon2
import pytest

from fragancia_api.modules.identity.infrastructure.argon2_password_hasher import (
    Argon2PasswordHasher,
)

BLOCKING_SECONDS = 0.3
TICK_SECONDS = 0.01


class Calls:
    """The thread of every call that reached the argon2 library."""

    def __init__(self) -> None:
        self.threads: list[int] = []


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    recorded = Calls()

    def fake_hash(self: argon2.PasswordHasher, password: str) -> str:
        recorded.threads.append(threading.get_ident())
        return "$argon2id$fake"

    def fake_verify(self: argon2.PasswordHasher, password_hash: str, password: str) -> bool:
        recorded.threads.append(threading.get_ident())
        return True

    monkeypatch.setattr(argon2.PasswordHasher, "hash", fake_hash)
    monkeypatch.setattr(argon2.PasswordHasher, "verify", fake_verify)
    return recorded


async def test_hash_runs_in_a_worker_thread(calls: Calls) -> None:
    hasher = Argon2PasswordHasher()  # computes its dummy hash here, on this thread
    calls.threads.clear()

    await hasher.hash("correct horse battery")

    assert len(calls.threads) == 1
    assert calls.threads[0] != threading.get_ident()


async def test_verify_runs_in_a_worker_thread(calls: Calls) -> None:
    hasher = Argon2PasswordHasher()
    calls.threads.clear()

    assert await hasher.verify("$argon2id$fake", "correct horse battery")

    assert len(calls.threads) == 1
    assert calls.threads[0] != threading.get_ident()


async def test_the_dummy_hash_is_computed_once_when_the_hasher_is_built(calls: Calls) -> None:
    hasher = Argon2PasswordHasher()
    built_on = list(calls.threads)

    await hasher.verify(hasher.dummy_hash, "anything at all")

    assert built_on == [threading.get_ident()]  # startup, not a request: sync is fine
    assert hasher.dummy_hash == "$argon2id$fake"


async def test_the_event_loop_keeps_running_while_a_slow_verify_is_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow_verify(self: argon2.PasswordHasher, password_hash: str, password: str) -> bool:
        time.sleep(BLOCKING_SECONDS)  # a blocking call, like the real argon2 work
        return True

    monkeypatch.setattr(argon2.PasswordHasher, "verify", slow_verify)
    hasher = Argon2PasswordHasher()
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(TICK_SECONDS)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await hasher.verify("$argon2id$fake", "correct horse battery")
    finally:
        task.cancel()

    # A blocked loop would let at most one tick through; a free one gets about thirty.
    assert ticks >= 5
