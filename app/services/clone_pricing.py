"""Наценка white-label клона на цены тарифов.

Единственный источник правды — contextvar текущего клона (``app/utils/clone_context``):

- интерактив в клон-боте: contextvar ставит ``TenantContextMiddleware`` (cloner);
- фоновые продления/списания (авто-покупка, суточный крон и т.п.): contextvar ставится
  явно через :func:`markup_context_for_user` по ``user.clone_bot_id``;
- основной бот, miniapp, кабинет: contextvar пуст → наценки НЕТ, цены не меняются.

Наценка применяется к БАЗОВОЙ цене до скидок, целочисленно:
``price * (100 + pct) // 100``.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

import structlog

from app.utils.clone_context import get_current_clone, reset_current_clone, set_current_clone


logger = structlog.get_logger(__name__)

MAX_MARKUP_PCT = 500

# Кэш наценок для фоновых сервисов: clone_id -> (pct, expires_at). Фоновые циклы
# обрабатывают сотни юзеров — не ходим в БД за одним и тем же клоном каждый раз.
_markup_cache: dict[int, tuple[int, float]] = {}
_MARKUP_CACHE_TTL = 60.0


def current_markup_pct() -> int:
    """Наценка активного клона (0 — не клон / наценки нет)."""
    clone = get_current_clone()
    if clone is None:
        return 0
    try:
        pct = int(getattr(clone, 'pricing_markup_pct', 0) or 0)
    except (TypeError, ValueError):
        return 0
    return min(max(pct, 0), MAX_MARKUP_PCT)


def apply_clone_markup(price_kopeks: int, pct: int | None = None) -> int:
    """Цена с наценкой текущего клона. Вне клона — цена без изменений."""
    if pct is None:
        pct = current_markup_pct()
    if pct <= 0 or price_kopeks <= 0:
        return price_kopeks
    return price_kopeks * (100 + pct) // 100


async def _resolve_user_markup(db, user) -> tuple[int, int] | None:
    """(clone_id, pct>0) для юзера клона, иначе None. С коротким кэшем."""
    clone_id = getattr(user, 'clone_bot_id', None)
    if not clone_id:
        return None
    now = time.monotonic()
    cached = _markup_cache.get(clone_id)
    if cached is not None and cached[1] > now:
        pct = cached[0]
        return (clone_id, pct) if pct > 0 else None
    try:
        from app.database.models import CloneBot

        clone = await db.get(CloneBot, clone_id)
        pct = min(max(int(getattr(clone, 'pricing_markup_pct', 0) or 0), 0), MAX_MARKUP_PCT) if clone else 0
    except Exception:
        logger.warning('Failed to resolve clone markup', clone_id=clone_id, exc_info=True)
        return None
    _markup_cache[clone_id] = (pct, now + _MARKUP_CACHE_TTL)
    return (clone_id, pct) if pct > 0 else None


async def get_user_markup_pct(db, user) -> int:
    """Наценка клона юзера для СИНХРОННЫХ расчётов в фоне (суточные списания и т.п.).

    В интерактиве клона уже стоит contextvar — тогда возвращаем его значение,
    чтобы не наценить дважды."""
    if get_current_clone() is not None:
        return current_markup_pct()
    resolved = await _resolve_user_markup(db, user)
    return resolved[1] if resolved else 0


class _MarkupOnly:
    """Лёгкий объект для contextvar в фоновых задачах: несёт только наценку.

    Ключевое: НЕ является клон-контекстом для UI (``is_clone_context`` в фоне
    никто не зовёт), а PricingEngine читает лишь ``pricing_markup_pct``.
    """

    __slots__ = ('clone_id', 'pricing_markup_pct')

    def __init__(self, clone_id: int, pct: int) -> None:
        self.clone_id = clone_id
        self.pricing_markup_pct = pct


@asynccontextmanager
async def markup_context_for_user(db, user):
    """Выставить наценку клона юзера на время фонового расчёта цены.

    Использовать ТОЛЬКО в фоновых сервисах (авто-продление, суточный крон):
    в интерактиве клона contextvar уже стоит, а в основном боте наценки быть не должно.
    Если contextvar уже установлен (вдруг вызвали из интерактива) — не трогаем его.
    """
    if get_current_clone() is not None:
        yield
        return
    resolved = await _resolve_user_markup(db, user)
    if resolved is None:
        yield
        return
    token = set_current_clone(_MarkupOnly(*resolved))
    try:
        yield
    finally:
        reset_current_clone(token)
