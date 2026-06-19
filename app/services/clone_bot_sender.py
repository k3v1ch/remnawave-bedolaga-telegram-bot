"""Подбор исходящего Bot для пользователя: основной бот или его клон-бот.

Плановые/периодические уведомления (мониторинг, daily, реферальные и т.п.) крутятся в
ОСНОВНОМ процессе и исторически слали всё через основной бот. Но клон-подписчик основной
бот никогда не запускал — такие сообщения до него не доходили. Этот резолвер возвращает
бота, который РЕАЛЬНО достанет пользователя: основной — для обычных юзеров, либо
закешированный ``Bot`` из токена клона — для клон-подписчиков (``user.clone_bot_id``).

Bot API стейтлес (обычный HTTP), поэтому отправлять по токену из основного процесса
нормально — отдельный вебхук-процесс (cloner) нужен только чтобы ПРИНИМАТЬ апдейты.

Свежесть кэша:
* мгновенная инвалидация при изменении клона в этом же процессе —
  :func:`invalidate_clone_bot` дёргается из ``publish_clone_event`` (смена токена/статуса);
* TTL подстраховывает изменения, опубликованные из другого процесса (кабинет-бэкенд).
"""

from __future__ import annotations

import asyncio
import time

import structlog
from aiogram import Bot

from app.bot_factory import create_bot
from app.database.crud.clone_bot import get_clone_bot, get_decrypted_token
from app.database.database import AsyncSessionLocal
from app.database.models import CloneBotStatus


logger = structlog.get_logger(__name__)

# Как долго доверять закешированному боту без перепроверки в БД (сек).
_TTL = 300.0


class _Entry:
    __slots__ = ('bot', 'token', 'ts')

    def __init__(self, bot: Bot, token: str, ts: float) -> None:
        self.bot = bot
        self.token = token
        self.ts = ts


_cache: dict[int, _Entry] = {}
_lock = asyncio.Lock()


async def get_bot_for_user(user, *, default_bot: Bot | None) -> Bot | None:
    """Вернуть бота, которым можно достать ``user``.

    * обычный пользователь (``clone_bot_id`` пуст) → ``default_bot`` (основной);
    * клон-подписчик → закешированный ``Bot`` его клона;
    * клон удалён/выключен → ``None`` (уведомление пропускаем — бот недоступен).
    """
    clone_id = getattr(user, 'clone_bot_id', None)
    if not clone_id:
        return default_bot

    entry = _cache.get(clone_id)
    if entry is not None and (time.monotonic() - entry.ts) < _TTL:
        return entry.bot

    async with _lock:
        entry = _cache.get(clone_id)
        if entry is not None and (time.monotonic() - entry.ts) < _TTL:
            return entry.bot

        try:
            async with AsyncSessionLocal() as db:
                clone = await get_clone_bot(db, clone_id)
                if clone is None or clone.status != CloneBotStatus.ACTIVE.value:
                    await _drop(clone_id)
                    return None
                token = get_decrypted_token(clone)
        except Exception as error:  # noqa: BLE001 — доставка важнее, не валим рассылку
            logger.warning('clone_bot_sender: не удалось загрузить клон-бота', clone_id=clone_id, error=str(error))
            # Если есть прежний бот в кэше — лучше отдать его, чем потерять уведомление.
            return entry.bot if entry is not None else None

        # Токен не изменился — продлеваем TTL и переиспользуем существующий Bot.
        if entry is not None and entry.token == token:
            entry.ts = time.monotonic()
            return entry.bot

        # Первая загрузка или ротация токена — пересоздаём Bot (старую сессию закрываем).
        if entry is not None:
            await _close(entry.bot)
        bot = create_bot(token=token)
        _cache[clone_id] = _Entry(bot, token, time.monotonic())
        logger.info('clone_bot_sender: создан Bot для клона', clone_id=clone_id)
        return bot


async def _drop(clone_id: int) -> None:
    entry = _cache.pop(clone_id, None)
    if entry is not None:
        await _close(entry.bot)


async def _close(bot: Bot) -> None:
    try:
        await bot.session.close()
    except Exception:  # noqa: BLE001
        pass


def invalidate_clone_bot(clone_id: int) -> None:
    """Сбросить кэш бота клона (вызывается из publish_clone_event при изменении клона).

    Синхронная: закрытие aiohttp-сессии планируем в фоне, чтобы не блокировать вызов.
    """
    entry = _cache.pop(clone_id, None)
    if entry is None:
        return
    try:
        asyncio.get_running_loop().create_task(_close(entry.bot))
    except RuntimeError:
        # Нет активного loop — сессия закроется при завершении процесса, не критично.
        pass
