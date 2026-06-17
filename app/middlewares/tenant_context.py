"""Tenant-context middleware for the multi-tenant cloner host.

Registered as an UPDATE-level outer middleware (``dp.update.outer_middleware``) so it
runs FIRST for every update — before AuthMiddleware — and tags the update with the clone
bot that received it. Handlers read ``data['clone_bot']`` for white-label branding/scoping.

Resolution is by ``event.bot.id`` against the in-memory registry, so it is O(1) and
needs no DB hit on the hot path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.utils.clone_context import reset_current_clone, set_current_clone


logger = structlog.get_logger(__name__)


class TenantContextMiddleware(BaseMiddleware):
    def __init__(self, registry: Any) -> None:
        self._registry = registry

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        clone = None
        bot = data.get('bot')
        if bot is not None:
            try:
                entry = self._registry.get_by_bot_id(bot.id)
                clone = entry.snapshot if entry is not None else None
            except Exception:  # never let tenant resolution break update handling
                logger.warning('Tenant resolution failed', exc_info=True)
        data['clone_bot'] = clone
        # Also expose it as a task-local contextvar so UI builders deep in the call
        # stack (keyboards) can hide main-brand-only actions without threading the
        # flag through every handler signature. Reset after handling to avoid leaking
        # into the next update reusing this task.
        token = set_current_clone(clone)
        try:
            return await handler(event, data)
        finally:
            reset_current_clone(token)
