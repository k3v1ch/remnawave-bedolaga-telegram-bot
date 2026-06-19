"""User-facing TikTok program routes for cabinet."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.services.tiktok_application_service import tiktok_application_service

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.tiktok import (
    TikTokApplicationInfo,
    TikTokApplicationRequest,
    TikTokStatusResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/tiktok', tags=['Cabinet TikTok'])


def _app_info(app) -> TikTokApplicationInfo:
    return TikTokApplicationInfo(
        id=app.id,
        status=app.status,
        display_name=app.display_name,
        tiktok_url=app.tiktok_url,
        other_platforms=app.other_platforms,
        audience_size=app.audience_size,
        content_topic=app.content_topic,
        description=app.description,
        admin_comment=app.admin_comment,
        created_at=app.created_at,
        processed_at=app.processed_at,
    )


@router.get('/status', response_model=TikTokStatusResponse)
async def get_tiktok_status(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """TikTok program status and latest application for the current user."""
    latest = await tiktok_application_service.get_latest_application(db, user.id)
    earnings_map = await tiktok_application_service.get_creator_earnings_map(db, [user.id])

    return TikTokStatusResponse(
        tiktok_status=user.tiktok_status,
        support_username=settings.TIKTOK_SUPPORT_USERNAME,
        total_earned_kopeks=earnings_map.get(user.id, 0),
        latest_application=_app_info(latest) if latest else None,
    )


@router.post('/apply', response_model=TikTokApplicationInfo)
async def apply_for_tiktok(
    request: TikTokApplicationRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Submit a TikTok program application."""
    application, error = await tiktok_application_service.submit_application(
        db,
        user_id=user.id,
        display_name=request.display_name,
        tiktok_url=request.tiktok_url,
        other_platforms=request.other_platforms,
        audience_size=request.audience_size,
        content_topic=request.content_topic,
        description=request.description,
    )

    if not application:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)

    # Уведомляем админов о новой заявке
    try:
        from app.bot_factory import create_bot
        from app.services.admin_notification_service import AdminNotificationService

        if getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False) and settings.BOT_TOKEN:
            bot = create_bot()
            try:
                notification_service = AdminNotificationService(bot)
                await notification_service.send_tiktok_application_notification(
                    user=user,
                    application_data={
                        'display_name': request.display_name,
                        'tiktok_url': request.tiktok_url,
                        'other_platforms': request.other_platforms,
                        'audience_size': request.audience_size,
                        'content_topic': request.content_topic,
                        'description': request.description,
                    },
                )
            finally:
                await bot.session.close()
    except Exception as e:
        logger.error('Failed to send admin notification for tiktok application', error=e)

    return _app_info(application)
