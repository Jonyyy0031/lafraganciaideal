"""Worker entrypoint: `arq fragancia_api.main.worker.WorkerSettings`.

Runs background jobs on Valkey. For now: relaying the outbox every 2 seconds.
"""

import logging
from typing import Any

import structlog
from arq import cron
from arq.connections import RedisSettings

from fragancia_api.config import Settings
from fragancia_api.container import Container, build_container
from fragancia_api.shared.infrastructure.logging import configure_logging

log = structlog.get_logger(__name__)

_settings = Settings()  # values come from the environment


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging(_settings.log_level, json=_settings.app_env != "development")
    # arq installs its own handler: avoid duplicated lines through the root logger, and hide
    # the start/finish lines of the 2-second outbox cron (failures are still logged).
    logging.getLogger("arq").propagate = False
    logging.getLogger("arq.worker").addFilter(_hide_outbox_cron_runs)
    ctx["container"] = build_container(_settings)
    log.info("worker.started")


def _hide_outbox_cron_runs(record: logging.LogRecord) -> bool:
    is_run_line = record.levelno <= logging.INFO and "cron:relay_outbox" in record.getMessage()
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


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(_settings.valkey_url)
    functions: list[Any] = []
    cron_jobs = [cron(relay_outbox, second=set(range(0, 60, 2)), run_at_startup=True, timeout=60)]
    on_startup = startup
    on_shutdown = shutdown
    keep_result = 0
