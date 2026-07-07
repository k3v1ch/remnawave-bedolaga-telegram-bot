"""CRUD для рекламных ссылок клон-ботов (``clone_bot_links``).

Ссылка — чистый счётчик (клики/регистрации), без бонусов. Клик инкрементится в
/start клона; регистрация — в AuthMiddleware в момент атрибуции юзера к клону
(pending-запись в Redis переживает FSM-очистки регистрационного флоу).
"""

from __future__ import annotations

import secrets

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.models import CloneBotLink, Transaction, TransactionType, User
from app.utils.cache import cache


logger = structlog.get_logger(__name__)

MAX_LINKS_PER_CLONE = 20

_PENDING_KEY = 'pending_clone_link:{telegram_id}'
_PENDING_TTL = 86400  # сутки: клик → регистрация обычно занимает минуты


async def create_link(db: AsyncSession, clone_bot_id: int, name: str) -> CloneBotLink:
    """Создать ссылку с уникальным slug (глобально — lookup в /start идёт по slug)."""
    for _ in range(5):
        slug = secrets.token_urlsafe(6)[:8].replace('-', 'x').replace('_', 'y')
        exists = await db.execute(select(CloneBotLink.id).where(CloneBotLink.slug == slug))
        if exists.scalar_one_or_none() is None:
            break
    link = CloneBotLink(clone_bot_id=clone_bot_id, name=name, slug=slug)
    db.add(link)
    await db.commit()
    await db.refresh(link)
    return link


async def get_link(db: AsyncSession, link_id: int) -> CloneBotLink | None:
    return await db.get(CloneBotLink, link_id)


async def get_link_by_slug(db: AsyncSession, slug: str) -> CloneBotLink | None:
    result = await db.execute(select(CloneBotLink).where(CloneBotLink.slug == slug))
    return result.scalar_one_or_none()


async def list_links(db: AsyncSession, clone_bot_id: int) -> list[CloneBotLink]:
    result = await db.execute(
        select(CloneBotLink)
        .where(CloneBotLink.clone_bot_id == clone_bot_id)
        .order_by(CloneBotLink.created_at.desc(), CloneBotLink.id.desc())
    )
    return list(result.scalars().all())


async def count_links(db: AsyncSession, clone_bot_id: int) -> int:
    result = await db.execute(
        select(func.count(CloneBotLink.id)).where(CloneBotLink.clone_bot_id == clone_bot_id)
    )
    return int(result.scalar() or 0)


async def delete_link(db: AsyncSession, link_id: int) -> bool:
    link = await db.get(CloneBotLink, link_id)
    if link is None:
        return False
    await db.delete(link)
    await db.commit()
    return True


async def increment_clicks(db: AsyncSession, link_id: int) -> None:
    """Атомарный клик++. Ошибки глотаем — метрика мягкая, /start важнее."""
    try:
        await db.execute(
            update(CloneBotLink)
            .where(CloneBotLink.id == link_id)
            .values(clicks_count=CloneBotLink.clicks_count + 1)
        )
        await db.commit()
    except Exception as exc:
        logger.warning('Failed to increment clone link clicks', link_id=link_id, error=exc)


async def get_link_stats(db: AsyncSession, link_id: int) -> dict[str, int]:
    """Живая статистика ссылки: юзеры и реальные пополнения приведённых по ней."""
    users_cnt = await db.execute(select(func.count(User.id)).where(User.clone_link_id == link_id))
    topup = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount_kopeks), 0))
        .select_from(Transaction)
        .join(User, User.id == Transaction.user_id)
        .where(
            User.clone_link_id == link_id,
            Transaction.is_completed.is_(True),
            Transaction.amount_kopeks > 0,
            Transaction.type == TransactionType.DEPOSIT.value,
            Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
        )
    )
    return {
        'users': int(users_cnt.scalar() or 0),
        'real_topup_kopeks': int(topup.scalar() or 0),
    }


# --- pending-атрибуция «клик → регистрация» (Redis через общий cache) ---------


async def save_pending_link(telegram_id: int, link_id: int) -> None:
    try:
        await cache.set(_PENDING_KEY.format(telegram_id=telegram_id), link_id, expire=_PENDING_TTL)
    except Exception as exc:
        logger.warning('Failed to save pending clone link', telegram_id=telegram_id, error=exc)


async def pop_pending_link(telegram_id: int) -> int | None:
    try:
        value = await cache.getdel(_PENDING_KEY.format(telegram_id=telegram_id))
        return int(value) if value is not None else None
    except Exception as exc:
        logger.warning('Failed to pop pending clone link', telegram_id=telegram_id, error=exc)
        return None
