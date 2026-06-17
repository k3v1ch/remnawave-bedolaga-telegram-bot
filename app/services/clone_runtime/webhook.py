"""Webhook ingestion for the cloner host.

A FastAPI router exposes ONE generic route ``POST {CLONE_WEBHOOK_PATH_PREFIX}/{clone_id}``
that resolves the target ``Bot`` from the registry at request time — so newly added bots
are served immediately without registering new routes or restarting (hot-swap).

``MultiBotWebhookProcessor`` mirrors the main bot's queue+worker pattern
(``app/webserver/telegram.py``) but carries ``(bot, update)`` tuples so a single shared
Dispatcher serves all clones in parallel via ``dispatcher.feed_update(bot, update)``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.config import settings


logger = structlog.get_logger(__name__)


class MultiBotWebhookProcessor:
    """Async queue that feeds (bot, update) pairs into one shared Dispatcher."""

    def __init__(
        self,
        *,
        dispatcher: Dispatcher,
        queue_maxsize: int,
        worker_count: int,
        enqueue_timeout: float,
        shutdown_timeout: float = 30.0,
    ) -> None:
        self._dispatcher = dispatcher
        self._queue_maxsize = max(1, queue_maxsize)
        self._worker_count = max(1, worker_count)
        self._enqueue_timeout = max(0.0, enqueue_timeout)
        self._shutdown_timeout = max(1.0, shutdown_timeout)
        self._queue: asyncio.Queue[tuple[Bot, Update] | object] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._workers: list[asyncio.Task[None]] = []
        self._running = False
        self._stop_sentinel: object = object()

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._queue = asyncio.Queue(maxsize=self._queue_maxsize)
        self._workers = [
            asyncio.create_task(self._worker_loop(i), name=f'clone-webhook-worker-{i}')
            for i in range(self._worker_count)
        ]
        logger.info('🚀 Clone webhook processor started', workers=self._worker_count, queue=self._queue_maxsize)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            await asyncio.wait_for(self._queue.join(), timeout=self._shutdown_timeout)
        except TimeoutError:
            logger.warning('⏱️ Clone webhook queue drain timed out')
        for _ in self._workers:
            with_sentinel = self._stop_sentinel
            try:
                self._queue.put_nowait(with_sentinel)
            except asyncio.QueueFull:
                await self._queue.put(with_sentinel)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info('🛑 Clone webhook processor stopped')

    async def enqueue(self, bot: Bot, update: Update) -> None:
        if not self._running:
            raise RuntimeError('clone webhook processor not running')
        try:
            if self._enqueue_timeout <= 0:
                self._queue.put_nowait((bot, update))
            else:
                await asyncio.wait_for(self._queue.put((bot, update)), timeout=self._enqueue_timeout)
        except (asyncio.QueueFull, TimeoutError) as error:
            raise OverflowError('clone webhook queue full') from error

    async def _worker_loop(self, worker_id: int) -> None:
        while True:
            item = await self._queue.get()
            if item is self._stop_sentinel:
                self._queue.task_done()
                break
            bot, update = item  # type: ignore[misc]
            try:
                await self._dispatcher.feed_update(bot, update)
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception:
                logger.exception('Error handling clone update', worker_id=worker_id)
            finally:
                self._queue.task_done()


def create_clone_webhook_router(registry: Any, processor: MultiBotWebhookProcessor) -> APIRouter:
    router = APIRouter()
    prefix = settings.CLONE_WEBHOOK_PATH_PREFIX.rstrip('/')

    @router.post(prefix + '/{clone_id}')
    async def clone_webhook(clone_id: int, request: Request) -> JSONResponse:
        entry = registry.get_by_clone_id(clone_id)
        if entry is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='unknown_clone')

        header_token = request.headers.get('X-Telegram-Bot-Api-Secret-Token')
        if header_token != entry.snapshot.webhook_secret:
            logger.warning('Clone webhook with bad secret', clone_id=clone_id)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='invalid_secret_token')

        try:
            payload: Any = await request.json()
            update = Update.model_validate(payload)
        except Exception as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='invalid_update') from error

        try:
            await processor.enqueue(entry.bot, update)
        except OverflowError as error:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='queue_full') from error
        return JSONResponse({'status': 'ok'})

    @router.get('/health/clone-host')
    async def clone_host_health() -> JSONResponse:
        return JSONResponse(
            {
                'status': 'ok',
                'active_clones': len(registry.active_clone_ids()),
                'processor_running': processor.is_running,
            }
        )

    return router
