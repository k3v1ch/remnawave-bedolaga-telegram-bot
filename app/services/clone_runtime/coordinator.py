"""Cross-process coordination for clone hot-swap.

The onboarding/CRM flows run in the MAIN bot process; the registry lives in the CLONER
process. They sync via Redis pub/sub on ``settings.CLONE_EVENTS_CHANNEL``:

  * main bot  → :func:`publish_clone_event` after committing a DB change
  * cloner    → :class:`CloneCoordinator` listens and applies add/remove/reload live

A periodic reconcile loop heals any missed event by diffing the DB's ACTIVE clones
against the in-memory registry. The cloner also owns Telegram webhook registration
(``set_webhook``/``delete_webhook``) since it holds the per-clone ``Bot``.
"""

from __future__ import annotations

import asyncio
import json

import redis.asyncio as redis
import structlog

from app.config import settings
from app.database.crud.clone_bot import list_active_clone_bots
from app.database.database import AsyncSessionLocal

from .registry import CloneBotRegistry, CloneEntry


logger = structlog.get_logger(__name__)


def clone_webhook_url(clone_id: int) -> str:
    base = (settings.CLONE_PUBLIC_BASE_URL or settings.WEBHOOK_URL or '').rstrip('/')
    prefix = settings.CLONE_WEBHOOK_PATH_PREFIX.rstrip('/')
    return f'{base}{prefix}/{clone_id}'


async def set_clone_webhook(entry: CloneEntry) -> None:
    url = clone_webhook_url(entry.snapshot.clone_id)
    await entry.bot.set_webhook(
        url=url,
        secret_token=entry.snapshot.webhook_secret,
        drop_pending_updates=False,
    )
    logger.info('Clone webhook set', clone_id=entry.snapshot.clone_id, url=url)


async def delete_clone_webhook(entry: CloneEntry) -> None:
    try:
        await entry.bot.delete_webhook(drop_pending_updates=False)
        logger.info('Clone webhook deleted', clone_id=entry.snapshot.clone_id)
    except Exception:  # pragma: no cover - best effort
        logger.warning('Failed to delete clone webhook', clone_id=entry.snapshot.clone_id, exc_info=True)


async def publish_clone_event(action: str, clone_id: int) -> None:
    """Publish a hot-swap event (called from the main bot after a DB commit).

    ``action`` ∈ {"add", "remove", "reload"}. Best-effort: the reconcile loop is the
    safety net if the cloner misses the message.
    """
    client = redis.from_url(settings.REDIS_URL)
    try:
        await client.publish(settings.CLONE_EVENTS_CHANNEL, json.dumps({'action': action, 'clone_id': clone_id}))
        logger.info('Published clone event', action=action, clone_id=clone_id)
    except Exception:
        logger.warning('Failed to publish clone event', action=action, clone_id=clone_id, exc_info=True)
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


class CloneCoordinator:
    def __init__(self, registry: CloneBotRegistry) -> None:
        self._registry = registry
        self._redis = redis.from_url(settings.REDIS_URL)
        self._pubsub = None
        self._listen_task: asyncio.Task[None] | None = None
        self._reconcile_task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        # Cold start: load all active clones, then (re)assert their webhooks.
        await self._registry.load_all()
        for clone_id in self._registry.active_clone_ids():
            entry = self._registry.get_by_clone_id(clone_id)
            if entry is not None:
                try:
                    await set_clone_webhook(entry)
                except Exception:
                    logger.warning('Failed to set webhook on cold start', clone_id=clone_id, exc_info=True)

        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(settings.CLONE_EVENTS_CHANNEL)
        self._listen_task = asyncio.create_task(self._listen(), name='clone-events-listener')
        self._reconcile_task = asyncio.create_task(self._reconcile_loop(), name='clone-reconcile')
        logger.info('🔌 Clone coordinator started', channel=settings.CLONE_EVENTS_CHANNEL)

    async def stop(self) -> None:
        self._running = False
        for task in (self._listen_task, self._reconcile_task):
            if task is not None:
                task.cancel()
        for task in (self._listen_task, self._reconcile_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self._pubsub is not None:
            try:
                await self._pubsub.unsubscribe(settings.CLONE_EVENTS_CHANNEL)
                await self._pubsub.aclose()
            except Exception:
                pass
        try:
            await self._redis.aclose()
        except Exception:
            pass
        await self._registry.close_all()
        logger.info('🛑 Clone coordinator stopped')

    async def _apply(self, action: str, clone_id: int) -> None:
        if action in ('add', 'reload'):
            entry = await self._registry.add_or_reload(clone_id)
            if entry is not None:
                await set_clone_webhook(entry)
        elif action == 'remove':
            entry = self._registry.get_by_clone_id(clone_id)
            if entry is not None:
                await delete_clone_webhook(entry)
            await self._registry.remove(clone_id)

    async def _listen(self) -> None:
        assert self._pubsub is not None
        async for message in self._pubsub.listen():
            if message.get('type') != 'message':
                continue
            try:
                raw = message['data']
                data = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
                await self._apply(str(data.get('action')), int(data.get('clone_id')))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('Failed to apply clone event', message=message, exc_info=True)

    async def _reconcile_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(max(15, settings.CLONE_RECONCILE_INTERVAL_SECONDS))
                await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('Clone reconcile failed', exc_info=True)

    async def _reconcile(self) -> None:
        async with AsyncSessionLocal() as session:
            desired = {c.id for c in await list_active_clone_bots(session)}
        current = self._registry.active_clone_ids()
        for clone_id in desired - current:
            await self._apply('reload', clone_id)
        for clone_id in current - desired:
            await self._apply('remove', clone_id)
        if desired != current:
            logger.info('Clone registry reconciled', added=len(desired - current), removed=len(current - desired))
