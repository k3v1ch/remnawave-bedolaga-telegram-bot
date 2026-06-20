"""Cloner host entrypoint — ONE process that serves ALL white-label clone bots.

Boots a single shop Dispatcher (``build_shop_dispatcher``) + an in-memory ``CloneBotRegistry``,
exposes a generic webhook route ``{CLONE_WEBHOOK_PATH_PREFIX}/{clone_id}``, and runs a
``CloneCoordinator`` (Redis pub/sub + reconcile) so bots are added/removed/enabled/disabled
live without restarting (hot-swap). All clones run in parallel through one worker pool.

Run as its own container (``python cloner.py``) so a clone-bot storm can never starve the
main bot process.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
import uvicorn
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
from fastapi import FastAPI

import redis.asyncio as redis

from app.bot import build_shop_dispatcher
from app.config import settings
from app.logging_config import _resolve_log_level, setup_logging
from app.middlewares.tenant_context import TenantContextMiddleware
from app.services.clone_runtime.coordinator import CloneCoordinator
from app.services.clone_runtime.registry import CloneBotRegistry
from app.services.clone_runtime.webhook import MultiBotWebhookProcessor, create_clone_webhook_router


logger = structlog.get_logger(__name__)


async def _build_storage():
    """FSM storage keyed WITH bot id, so the same Telegram user talking to two different
    clone bots does not share FSM state."""
    try:
        client = redis.from_url(settings.REDIS_URL)
        await client.ping()
        logger.info('Cloner FSM storage: Redis')
        return RedisStorage(client, key_builder=DefaultKeyBuilder(with_bot_id=True))
    except Exception as error:
        logger.warning('Cloner FSM storage: Memory (Redis unavailable)', error=error)
        return MemoryStorage()


def _init_logging() -> None:
    """Attach log handlers (console + cloner file) — full logging for ALL clones.

    setup_logging() only configures structlog and returns formatters; the CALLER must
    wire handlers (main.py does this for the bot). The cloner used to skip that, so its
    stdlib loggers had no handler and everything fell through to Python's last-resort
    handler (WARNING+ only, raw format) — clone activity/errors were invisible. Now we
    log everything to stdout (docker logs) and to logs/current/cloner.log."""
    file_formatter, console_formatter, _notifier = setup_logging()
    handlers: list[logging.Handler] = []

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(console_formatter)
    handlers.append(console)

    try:
        log_dir = Path('logs/current')
        if not log_dir.exists():
            log_dir = Path('logs')
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / 'cloner.log', encoding='utf-8')
        file_handler.setFormatter(file_formatter)
        handlers.append(file_handler)
    except Exception as error:  # never let file logging break startup
        console.handle(logging.makeLogRecord({'msg': f'cloner file log disabled: {error}', 'levelno': logging.WARNING}))

    logging.basicConfig(level=_resolve_log_level(settings.LOG_LEVEL), handlers=handlers, force=True)


async def main() -> None:
    _init_logging()

    if not settings.CLONE_TOKEN_SECRET:
        logger.warning('CLONE_TOKEN_SECRET is not set — clone tokens cannot be decrypted; registry will be empty')

    storage = await _build_storage()
    dispatcher = build_shop_dispatcher(storage)
    registry = CloneBotRegistry()

    # Tenant context runs FIRST for every update (before AuthMiddleware).
    dispatcher.update.outer_middleware(TenantContextMiddleware(registry))

    processor = MultiBotWebhookProcessor(
        dispatcher=dispatcher,
        queue_maxsize=settings.CLONE_WEBHOOK_QUEUE_MAXSIZE,
        worker_count=settings.CLONE_WEBHOOK_WORKERS,
        enqueue_timeout=settings.CLONE_WEBHOOK_ENQUEUE_TIMEOUT,
    )
    coordinator = CloneCoordinator(registry)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await processor.start()
        await coordinator.start()
        logger.info('✅ Cloner host ready', active_clones=len(registry.active_clone_ids()))
        try:
            yield
        finally:
            await coordinator.stop()
            await processor.stop()

    app = FastAPI(title='Clone Bot Host', lifespan=lifespan)
    app.include_router(create_clone_webhook_router(registry, processor))

    # IMPORTANT: uvicorn's DEFAULT log config sets disable_existing_loggers=True, which
    # silences our structlog stdlib loggers configured by setup_logging() — the whole
    # clone host then logs almost nothing (no incoming-update logs, no handler errors),
    # making clones impossible to debug. Pass our own config with disable_existing_loggers
    # =False (mirrors app/webapi/server.py) so app logs keep flowing to stdout.
    log_config = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'default': {
                '()': 'uvicorn.logging.DefaultFormatter',
                'fmt': '%(levelprefix)s %(message)s',
                'use_colors': None,
            },
        },
        'handlers': {
            'default': {
                'formatter': 'default',
                'class': 'logging.StreamHandler',
                'stream': 'ext://sys.stderr',
            },
        },
        'loggers': {
            'uvicorn': {'handlers': ['default'], 'level': 'WARNING', 'propagate': False},
            'uvicorn.error': {'level': 'WARNING', 'propagate': False},
            'uvicorn.access': {'level': 'ERROR', 'propagate': False},
        },
    }

    config = uvicorn.Config(
        app=app,
        host=settings.CLONE_HOST_BIND,
        port=int(settings.CLONE_HOST_PORT),
        log_level='warning',
        lifespan='on',
        access_log=False,
        log_config=log_config,
    )
    logger.info('🌐 Starting cloner host', bind=settings.CLONE_HOST_BIND, port=settings.CLONE_HOST_PORT)
    await uvicorn.Server(config).serve()


if __name__ == '__main__':
    asyncio.run(main())
