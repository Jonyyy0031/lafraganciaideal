"""Worker entrypoint: `arq fragancia_api.main.worker.WorkerSettings`.

Runs background jobs on Valkey: relaying the outbox every 2 seconds, plus the scheduled jobs
modules declare (`AppModule.jobs`), each as an arq cron named `cron:<job name>` that runs at
most once at a time per worker process.
"""

import asyncio
import logging
from typing import Any

import structlog
from arq import cron
from arq.connections import RedisSettings
from arq.cron import CronJob

from fragancia_api.config import Settings
from fragancia_api.container import MODULES, Container, build_container
from fragancia_api.shared.infrastructure.logging import configure_logging
from fragancia_api.shared.module import ScheduledJob

log = structlog.get_logger(__name__)

_settings = Settings()  # values come from the environment


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging(_settings.log_level, json=_settings.app_env != "development")
    # arq installs its own handler: avoid duplicated lines through the root logger, and hide
    # the start/finish lines of the frequent crons (failures are still logged).
    logging.getLogger("arq").propagate = False
    logging.getLogger("arq.worker").addFilter(_hide_cron_runs)
    ctx["container"] = build_container(_settings)
    log.info("worker.started")


def _hide_cron_runs(record: logging.LogRecord) -> bool:
    is_run_line = record.levelno <= logging.INFO and "cron:" in record.getMessage()
    return not is_run_line


async def shutdown(ctx: dict[str, Any]) -> None:
    container: Container = ctx["container"]
    await container.close()


async def relay_outbox(ctx: dict[str, Any]) -> int:
    container: Container = ctx["container"]
    processed = await container.relay.relay_batch()
    if processed:
        log.info("outbox.relayed", processed=processed)
    return processed


def _module_cron(job: ScheduledJob) -> CronJob:
    # One run of each module job at a time per worker: a run that finds the previous one still
    # going returns at once, so long jobs never fill arq's slots and starve the outbox relay.
    running = asyncio.Lock()

    async def run(ctx: dict[str, Any]) -> int:
        if running.locked():
            return 0
        async with running:
            container: Container = ctx["container"]
            processed = await job.run(container.services)
        if processed > 0:
            log.info("job.processed", job=job.name, processed=processed)
        return processed

    seconds = {0} if job.every_seconds == 60 else set(range(0, 60, job.every_seconds))
    return cron(
        run,
        name=f"cron:{job.name}",
        second=seconds,
        timeout=job.timeout_seconds,
        run_at_startup=False,
    )


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(_settings.valkey_url)
    functions: list[Any] = []
    cron_jobs = [
        cron(relay_outbox, second=set(range(0, 60, 2)), run_at_startup=True, timeout=60),
        *(_module_cron(job) for module in MODULES for job in module.jobs),
    ]
    on_startup = startup
    on_shutdown = shutdown
    keep_result = 0
