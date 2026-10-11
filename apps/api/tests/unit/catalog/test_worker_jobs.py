"""Plan 007 repair round 1 (L3): each module job runs at most once at a time per worker
process, so a long validation never fills arq's slots. The worker module is imported with
synthetic environment variables because `main/worker.py` builds `Settings()` at import."""

import asyncio
import importlib
from types import ModuleType
from typing import Any

import pytest

from fragancia_api.shared.module import ScheduledJob


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:5433/fragancia")
    monkeypatch.setenv("VALKEY_URL", "redis://127.0.0.1:6380/0")
    return importlib.import_module("fragancia_api.main.worker")


class _Job:
    def __init__(self) -> None:
        self.started = 0
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def run(self, services: Any) -> int:
        self.started += 1
        self.entered.set()
        await self.release.wait()
        return 3


class _Container:
    services = object()


def _cron(worker: ModuleType, job: _Job, name: str = "slow") -> Any:
    return worker._module_cron(
        ScheduledJob(name=name, run=job.run, every_seconds=60, timeout_seconds=1800)
    )


async def test_a_run_that_finds_the_previous_one_going_returns_zero_without_running_the_job(
    worker: ModuleType,
) -> None:
    job = _Job()
    run = _cron(worker, job).coroutine
    ctx = {"container": _Container()}

    first = asyncio.create_task(run(ctx))
    await job.entered.wait()
    second = await run(ctx)  # the first is still going

    assert second == 0
    assert job.started == 1
    job.release.set()
    assert await first == 3


async def test_the_next_run_goes_ahead_once_the_previous_one_finished(worker: ModuleType) -> None:
    job = _Job()
    job.release.set()
    run = _cron(worker, job).coroutine
    ctx = {"container": _Container()}

    assert await run(ctx) == 3
    assert await run(ctx) == 3
    assert job.started == 2


async def test_the_lock_is_released_when_the_job_raises(worker: ModuleType) -> None:
    async def boom(services: Any) -> int:
        raise RuntimeError("job failed")

    run = worker._module_cron(
        ScheduledJob(name="boom", run=boom, every_seconds=60, timeout_seconds=10)
    ).coroutine
    ctx = {"container": _Container()}

    with pytest.raises(RuntimeError):
        await run(ctx)
    with pytest.raises(RuntimeError):  # it ran again: the lock was not left held
        await run(ctx)


async def test_each_job_has_its_own_lock(worker: ModuleType) -> None:
    held, other = _Job(), _Job()
    other.release.set()
    run_held = _cron(worker, held, "held").coroutine
    run_other = _cron(worker, other, "other").coroutine
    ctx = {"container": _Container()}

    task = asyncio.create_task(run_held(ctx))
    await held.entered.wait()

    assert await run_other(ctx) == 3
    held.release.set()
    await task
