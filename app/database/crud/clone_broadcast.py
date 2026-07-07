"""CRUD для рассылок клон-ботов (``clone_broadcasts``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CloneBroadcast, User


logger = structlog.get_logger(__name__)

CLONE_BROADCASTS_PER_DAY = 10


async def create_broadcast(
    db: AsyncSession,
    clone_bot_id: int,
    *,
    message_text: str | None,
    media_type: str | None = None,
    media_file_id: str | None = None,
    button_text: str | None = None,
    button_url: str | None = None,
    show_tariffs_button: bool = False,
    total_count: int = 0,
) -> CloneBroadcast:
    broadcast = CloneBroadcast(
        clone_bot_id=clone_bot_id,
        message_text=message_text,
        media_type=media_type,
        media_file_id=media_file_id,
        button_text=button_text,
        button_url=button_url,
        show_tariffs_button=show_tariffs_button,
        total_count=total_count,
        status='in_progress',
    )
    db.add(broadcast)
    await db.commit()
    await db.refresh(broadcast)
    return broadcast


async def get_broadcast(db: AsyncSession, broadcast_id: int) -> CloneBroadcast | None:
    return await db.get(CloneBroadcast, broadcast_id)


async def list_broadcasts(db: AsyncSession, clone_bot_id: int, *, limit: int = 10) -> list[CloneBroadcast]:
    result = await db.execute(
        select(CloneBroadcast)
        .where(CloneBroadcast.clone_bot_id == clone_bot_id)
        .order_by(CloneBroadcast.created_at.desc(), CloneBroadcast.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_today(db: AsyncSession, clone_bot_id: int) -> int:
    """Сколько рассылок клон создал за последние сутки (лимит — CLONE_BROADCASTS_PER_DAY)."""
    since = datetime.now(UTC) - timedelta(days=1)
    result = await db.execute(
        select(func.count(CloneBroadcast.id)).where(
            CloneBroadcast.clone_bot_id == clone_bot_id,
            CloneBroadcast.created_at >= since,
        )
    )
    return int(result.scalar() or 0)


async def get_recipient_telegram_ids(db: AsyncSession, clone_bot_id: int) -> list[int]:
    """Получатели рассылки: ТОЛЬКО юзеры этого клона с telegram_id (никаких соседей)."""
    result = await db.execute(
        select(User.telegram_id).where(
            User.clone_bot_id == clone_bot_id,
            User.telegram_id.isnot(None),
        )
    )
    return [int(tid) for (tid,) in result.all()]


async def finish_broadcast(
    db: AsyncSession, broadcast_id: int, *, sent: int, failed: int, status: str = 'completed'
) -> None:
    broadcast = await db.get(CloneBroadcast, broadcast_id)
    if broadcast is None:
        return
    broadcast.sent_count = sent
    broadcast.failed_count = failed
    broadcast.status = status
    broadcast.completed_at = datetime.now(UTC)
    await db.commit()
