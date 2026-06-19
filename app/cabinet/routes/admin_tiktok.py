"""Admin routes for the TikTok program in cabinet."""

from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import TikTokApplication, TikTokApplicationStatus, User
from app.services.tiktok_application_service import tiktok_application_service

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.tiktok import (
    AdminTikTokApplicationItem,
    AdminTikTokApplicationsResponse,
    AdminTikTokCreatorItem,
    AdminTikTokCreatorsResponse,
    TikTokAddEarningRequest,
    TikTokApproveRequest,
    TikTokEarningItem,
    TikTokEarningsResponse,
    TikTokRejectRequest,
    TikTokStatsResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/tiktok', tags=['Cabinet Admin TikTok'])


async def _notify_user(telegram_id: int | None, text: str) -> None:
    """Шлёт пользователю уведомление в Telegram (best-effort)."""
    if not telegram_id or not settings.BOT_TOKEN:
        return
    try:
        from app.bot_factory import create_bot

        bot = create_bot()
        try:
            await bot.send_message(chat_id=telegram_id, text=text, parse_mode='HTML')
        finally:
            await bot.session.close()
    except Exception as e:
        logger.error('Failed to send tiktok user notification', error=e)


# ==================== Stats ====================


@router.get('/stats', response_model=TikTokStatsResponse)
async def get_tiktok_stats(
    admin: User = Depends(require_permission('tiktok:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Overall TikTok program statistics."""
    from app.database.models import TikTokEarning

    total_creators = await db.execute(
        select(func.count()).select_from(User).where(User.tiktok_status == TikTokApplicationStatus.APPROVED.value)
    )
    pending_apps = await db.execute(
        select(func.count())
        .select_from(TikTokApplication)
        .where(TikTokApplication.status == TikTokApplicationStatus.PENDING.value)
    )
    total_earnings = await db.execute(select(func.coalesce(func.sum(TikTokEarning.amount_kopeks), 0)))

    return TikTokStatsResponse(
        total_creators=total_creators.scalar() or 0,
        pending_applications=pending_apps.scalar() or 0,
        total_earnings_kopeks=total_earnings.scalar() or 0,
    )


# ==================== Applications ====================


@router.get('/applications', response_model=AdminTikTokApplicationsResponse)
async def list_applications(
    application_status: Literal['pending', 'approved', 'rejected', 'none'] | None = Query(None, alias='status'),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    admin: User = Depends(require_permission('tiktok:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List TikTok applications."""
    applications, total = await tiktok_application_service.get_all_applications(
        db, status=application_status, limit=limit, offset=offset
    )

    user_ids = list({app.user_id for app in applications})
    if user_ids:
        users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        users_map = {u.id: u for u in users_result.scalars().all()}
    else:
        users_map = {}

    items = []
    for app in applications:
        user = users_map.get(app.user_id)
        items.append(
            AdminTikTokApplicationItem(
                id=app.id,
                user_id=app.user_id,
                username=user.username if user else None,
                first_name=user.first_name if user else None,
                telegram_id=user.telegram_id if user else None,
                display_name=app.display_name,
                tiktok_url=app.tiktok_url,
                other_platforms=app.other_platforms,
                audience_size=app.audience_size,
                content_topic=app.content_topic,
                description=app.description,
                status=app.status,
                admin_comment=app.admin_comment,
                created_at=app.created_at,
                processed_at=app.processed_at,
            )
        )

    return AdminTikTokApplicationsResponse(items=items, total=total)


@router.post('/applications/{application_id}/approve')
async def approve_application(
    application_id: int,
    request: TikTokApproveRequest,
    admin: User = Depends(require_permission('tiktok:approve')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Approve a TikTok application."""
    success, error = await tiktok_application_service.approve_application(
        db, application_id=application_id, admin_id=admin.id, comment=request.comment
    )
    if not success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)

    application = await db.get(TikTokApplication, application_id)
    user = await db.get(User, application.user_id) if application else None
    if user:
        support = settings.TIKTOK_SUPPORT_USERNAME
        comment_text = f'\n{request.comment}' if request.comment else ''
        await _notify_user(
            user.telegram_id,
            '✅ <b>Ваша заявка в TikTok-программу одобрена!</b>\n\n'
            f'Снимайте ролики по условиям и присылайте результаты в поддержку: {support}'
            f'{comment_text}',
        )

    return {'success': True}


@router.post('/applications/{application_id}/reject')
async def reject_application(
    application_id: int,
    request: TikTokRejectRequest,
    admin: User = Depends(require_permission('tiktok:approve')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Reject a TikTok application."""
    application = await db.get(TikTokApplication, application_id)
    user = await db.get(User, application.user_id) if application else None

    success, error = await tiktok_application_service.reject_application(
        db, application_id=application_id, admin_id=admin.id, comment=request.comment
    )
    if not success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)

    if user:
        comment_text = f'\nПричина: {request.comment}' if request.comment else ''
        await _notify_user(
            user.telegram_id,
            f'❌ <b>Ваша заявка в TikTok-программу отклонена.</b>{comment_text}',
        )

    return {'success': True}


# ==================== Creators ====================


@router.get('/creators', response_model=AdminTikTokCreatorsResponse)
async def list_creators(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    admin: User = Depends(require_permission('tiktok:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List approved TikTok creators with their total earnings."""
    count_result = await db.execute(
        select(func.count()).select_from(User).where(User.tiktok_status == TikTokApplicationStatus.APPROVED.value)
    )
    total = count_result.scalar() or 0

    result = await db.execute(
        select(User)
        .where(User.tiktok_status == TikTokApplicationStatus.APPROVED.value)
        .order_by(desc(User.created_at))
        .offset(offset)
        .limit(limit)
    )
    creators = result.scalars().all()
    creator_ids = [u.id for u in creators]

    earnings_map = await tiktok_application_service.get_creator_earnings_map(db, creator_ids)

    # Подтягиваем последнюю заявку каждого автора для display_name / ссылки
    app_map: dict[int, TikTokApplication] = {}
    if creator_ids:
        apps_result = await db.execute(
            select(TikTokApplication)
            .where(TikTokApplication.user_id.in_(creator_ids))
            .order_by(TikTokApplication.user_id, desc(TikTokApplication.created_at))
        )
        for app in apps_result.scalars().all():
            app_map.setdefault(app.user_id, app)

    items = []
    for user in creators:
        app = app_map.get(user.id)
        items.append(
            AdminTikTokCreatorItem(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                telegram_id=user.telegram_id,
                display_name=app.display_name if app else None,
                tiktok_url=app.tiktok_url if app else None,
                total_earned_kopeks=earnings_map.get(user.id, 0),
                tiktok_status=user.tiktok_status,
                created_at=user.created_at,
            )
        )

    return AdminTikTokCreatorsResponse(items=items, total=total)


@router.post('/{user_id}/revoke')
async def revoke_creator(
    user_id: int,
    admin: User = Depends(require_permission('tiktok:revoke')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Remove a creator from the TikTok program."""
    success, error = await tiktok_application_service.revoke(db, user_id=user_id, admin_id=admin.id)
    if not success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)
    return {'success': True}


# ==================== Earnings journal ====================


@router.get('/{user_id}/earnings', response_model=TikTokEarningsResponse)
async def list_earnings(
    user_id: int,
    admin: User = Depends(require_permission('tiktok:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List the earnings journal for a creator."""
    earnings = await tiktok_application_service.list_earnings(db, user_id)
    items = [TikTokEarningItem.model_validate(e) for e in earnings]
    total = sum(e.amount_kopeks for e in earnings)
    return TikTokEarningsResponse(items=items, total_kopeks=total)


@router.post('/{user_id}/earnings')
async def add_earning(
    user_id: int,
    request: TikTokAddEarningRequest,
    admin: User = Depends(require_permission('tiktok:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Add an earnings entry for a creator (manual)."""
    earning, error = await tiktok_application_service.add_earning(
        db, user_id=user_id, amount_kopeks=request.amount_kopeks, admin_id=admin.id, comment=request.comment
    )
    if not earning:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)
    return {'success': True, 'id': earning.id}


@router.delete('/{user_id}/earnings/{earning_id}')
async def delete_earning(
    user_id: int,
    earning_id: int,
    admin: User = Depends(require_permission('tiktok:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete an earnings entry."""
    success, error = await tiktok_application_service.delete_earning(db, earning_id)
    if not success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)
    return {'success': True}
