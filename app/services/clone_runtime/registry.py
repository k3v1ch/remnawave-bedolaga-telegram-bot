"""In-memory registry of live clone bots for the cloner host.

Holds one aiogram ``Bot`` per active clone, keyed by both ``clone_id`` (webhook route)
and ``bot_id`` (tenant resolution). Supports live ``add_or_reload`` / ``remove`` so the
cloner never restarts to pick up a new/disabled bot (hot-swap). Concurrency-safe via a lock.

We store a plain ``CloneSnapshot`` (not the session-bound ORM row) so handlers/middleware
can read tenant data without touching the DB.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog
from aiogram import Bot

from app.bot_factory import create_bot
from app.database.crud.clone_bot import get_clone_bot, get_decrypted_token, list_active_clone_bots
from app.database.database import AsyncSessionLocal
from app.database.models import CloneBot, CloneBotStatus


logger = structlog.get_logger(__name__)


@dataclass
class CloneSnapshot:
    clone_id: int
    bot_id: int
    bot_username: str | None
    external_squad_uuid: str | None
    profile_title: str | None
    webhook_secret: str

    @classmethod
    def from_model(cls, clone: CloneBot) -> CloneSnapshot:
        return cls(
            clone_id=clone.id,
            bot_id=clone.bot_id,
            bot_username=clone.bot_username,
            external_squad_uuid=clone.external_squad_uuid,
            profile_title=clone.profile_title,
            webhook_secret=clone.webhook_secret,
        )


@dataclass
class CloneEntry:
    bot: Bot
    snapshot: CloneSnapshot


async def _close_bot(bot: Bot) -> None:
    try:
        await bot.session.close()
    except Exception:  # pragma: no cover - best-effort cleanup
        logger.debug('Failed to close clone bot session', exc_info=True)


class CloneBotRegistry:
    def __init__(self) -> None:
        self._by_clone_id: dict[int, CloneEntry] = {}
        self._bot_id_to_clone_id: dict[int, int] = {}
        self._lock = asyncio.Lock()

    # --- lookups (hot path, no lock — dict reads are atomic enough for our use) ---

    def get_by_clone_id(self, clone_id: int) -> CloneEntry | None:
        return self._by_clone_id.get(clone_id)

    def get_by_bot_id(self, bot_id: int) -> CloneEntry | None:
        clone_id = self._bot_id_to_clone_id.get(bot_id)
        return self._by_clone_id.get(clone_id) if clone_id is not None else None

    def active_clone_ids(self) -> set[int]:
        return set(self._by_clone_id.keys())

    # --- mutations (hot-swap) ---

    async def _put(self, snapshot: CloneSnapshot, token: str) -> CloneEntry:
        async with self._lock:
            old = self._by_clone_id.pop(snapshot.clone_id, None)
            if old is not None:
                self._bot_id_to_clone_id.pop(old.snapshot.bot_id, None)
                await _close_bot(old.bot)
            entry = CloneEntry(bot=create_bot(token=token), snapshot=snapshot)
            self._by_clone_id[snapshot.clone_id] = entry
            self._bot_id_to_clone_id[snapshot.bot_id] = snapshot.clone_id
            logger.info('Clone bot added to registry', clone_id=snapshot.clone_id, bot_id=snapshot.bot_id)
            return entry

    async def add_or_reload(self, clone_id: int) -> CloneEntry | None:
        """(Re)load a clone from the DB into the registry. Removes it if no longer ACTIVE."""
        async with AsyncSessionLocal() as session:
            clone = await get_clone_bot(session, clone_id)
            if clone is None or clone.status != CloneBotStatus.ACTIVE.value:
                await self.remove(clone_id)
                return None
            snapshot = CloneSnapshot.from_model(clone)
            token = get_decrypted_token(clone)
        return await self._put(snapshot, token)

    async def remove(self, clone_id: int) -> None:
        async with self._lock:
            entry = self._by_clone_id.pop(clone_id, None)
            if entry is not None:
                self._bot_id_to_clone_id.pop(entry.snapshot.bot_id, None)
                await _close_bot(entry.bot)
                logger.info('Clone bot removed from registry', clone_id=clone_id)

    async def load_all(self) -> None:
        """Cold start: load every ACTIVE clone from the DB."""
        async with AsyncSessionLocal() as session:
            clones = await list_active_clone_bots(session)
            loaded = [(CloneSnapshot.from_model(c), get_decrypted_token(c)) for c in clones]
        for snapshot, token in loaded:
            try:
                await self._put(snapshot, token)
            except Exception:
                logger.warning('Failed to load clone into registry', clone_id=snapshot.clone_id, exc_info=True)
        logger.info('Clone registry cold start complete', count=len(self._by_clone_id))

    async def close_all(self) -> None:
        async with self._lock:
            for entry in self._by_clone_id.values():
                await _close_bot(entry.bot)
            self._by_clone_id.clear()
            self._bot_id_to_clone_id.clear()
