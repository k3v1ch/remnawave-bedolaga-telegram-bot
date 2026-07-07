"""Admin cabinet routes for white-label clone bots (CRM).

Mirrors the in-bot reseller panel «Мои боты» (app/handlers/custom_reseller.py) for
admins: list/detail with stats, period stats, pricing markup, profile title rename,
token rotation, ad links, enable/disable (kill-switch) and delete. Mutations publish
a hot-swap event so the cloner host applies changes live (no restart).
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.config import settings
from app.database.crud.clone_bot import (
    count_active_subscribers,
    delete_clone_bot,
    get_brought_users,
    get_clone_bot,
    get_decrypted_token,
    get_period_stats,
    get_stats_bulk,
    list_clone_bots,
    set_channel_sub_channel,
    set_channel_sub_enabled,
    set_channel_sub_text,
    set_pricing_markup,
    set_status,
    update_profile_title,
    update_token,
)
from app.database.crud.clone_bot_link import (
    MAX_LINKS_PER_CLONE,
    count_links,
    create_link,
    delete_link,
    get_link,
    get_link_stats,
    list_links,
)
from app.database.crud.clone_broadcast import (
    CLONE_BROADCASTS_PER_DAY,
    count_today,
    create_broadcast,
    get_recipient_telegram_ids,
    list_broadcasts,
)
from app.database.models import CloneBot, CloneBotStatus, SubscriptionStatus, User
from app.services.clone_bot_service import (
    CloneChannelVerifyError,
    cleanup_squad_on_delete,
    parse_channel_ref,
    update_squad_profile_title,
    verify_clone_channel,
)
from app.services.clone_broadcast_service import run_clone_broadcast
from app.services.clone_pricing import MAX_MARKUP_PCT
from app.services.clone_runtime.coordinator import publish_clone_event

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/clone-bots', tags=['Cabinet Admin Clone Bots'])

# Совпадает с валидацией онбординга и панели «Мои боты» (app/handlers/clone_bot.py,
# app/handlers/custom_reseller.py): панель принимает имена только из латиницы/цифр/
# пробела/дефиса/подчёркивания.
_TOKEN_RE = re.compile(r'^\d{5,}:[\w-]{30,}$')
_NAME_RE = re.compile(r'^[A-Za-z0-9 _-]+$')
_MAX_NAME_LEN = 40


class CloneBotItem(BaseModel):
    id: int
    bot_id: int
    bot_username: str | None = None
    bot_title: str | None = None
    status: str
    external_squad_name: str | None = None
    profile_title: str | None = None
    owner_user_id: int
    users_brought: int = 0
    revenue_kopeks: int = 0
    real_topup_kopeks: int = 0
    pricing_markup_pct: int = 0
    last_error: str | None = None
    created_at: datetime | None = None


class CloneBotListResponse(BaseModel):
    items: list[CloneBotItem]
    total: int


class CloneOwner(BaseModel):
    user_id: int
    telegram_id: int | None = None
    username: str | None = None
    full_name: str | None = None


class CloneBroughtUser(BaseModel):
    id: int
    telegram_id: int | None = None
    username: str | None = None
    full_name: str | None = None
    status: str
    balance_kopeks: int = 0
    has_active_subscription: bool = False
    created_at: datetime | None = None


class ChannelSubState(BaseModel):
    enabled: bool
    has_channel: bool
    channel_link: str | None = None
    channel_title: str | None = None
    text: str | None = None  # None = стандартный текст заглушки


class CloneBotDetail(CloneBotItem):
    owner: CloneOwner | None = None
    active_subscribers: int = 0
    users: list[CloneBroughtUser] = []
    channel_sub: ChannelSubState | None = None


class ToggleResponse(BaseModel):
    id: int
    status: str


class CloneStatsResponse(BaseModel):
    period: str
    new_users: int = 0
    purchases: int = 0
    real_topup_kopeks: int = 0
    owner_reward_kopeks: int = 0
    owner_reward_days: int = 0


class MarkupUpdateRequest(BaseModel):
    pct: int = Field(ge=0, le=MAX_MARKUP_PCT)


class MarkupResponse(BaseModel):
    id: int
    pricing_markup_pct: int


class TitleUpdateRequest(BaseModel):
    title: str


class TitleResponse(BaseModel):
    id: int
    profile_title: str


class TokenUpdateRequest(BaseModel):
    token: str


class TokenResponse(BaseModel):
    id: int
    bot_username: str | None = None
    bot_title: str | None = None


class CloneLinkItem(BaseModel):
    id: int
    name: str
    url: str
    clicks_count: int = 0
    registrations_count: int = 0
    real_topup_kopeks: int = 0
    created_at: datetime | None = None


class CloneLinksResponse(BaseModel):
    items: list[CloneLinkItem]
    max_links: int = MAX_LINKS_PER_CLONE


class LinkCreateRequest(BaseModel):
    name: str


class ChannelSubChannelRequest(BaseModel):
    channel: str


class ChannelSubTextRequest(BaseModel):
    text: str | None = None


class CloneBroadcastItem(BaseModel):
    id: int
    status: str
    message_text: str | None = None
    media_type: str | None = None
    button_text: str | None = None
    button_url: str | None = None
    show_tariffs_button: bool = False
    sent_count: int = 0
    failed_count: int = 0
    total_count: int = 0
    created_at: datetime | None = None


class CloneBroadcastsResponse(BaseModel):
    items: list[CloneBroadcastItem]
    used_today: int
    per_day_limit: int = CLONE_BROADCASTS_PER_DAY
    recipients: int = 0


def _full_name(user: User) -> str | None:
    parts = [p for p in (user.first_name, user.last_name) if p]
    return ' '.join(parts) if parts else None


def _to_item(clone, stats: dict[int, dict[str, int]]) -> CloneBotItem:
    st = stats.get(clone.id, {})
    return CloneBotItem(
        id=clone.id,
        bot_id=clone.bot_id,
        bot_username=clone.bot_username,
        bot_title=clone.bot_title,
        status=clone.status,
        external_squad_name=clone.external_squad_name,
        profile_title=clone.profile_title,
        owner_user_id=clone.owner_user_id,
        users_brought=st.get('users', 0),
        revenue_kopeks=st.get('revenue_kopeks', 0),
        real_topup_kopeks=st.get('real_topup_kopeks', 0),
        pricing_markup_pct=int(clone.pricing_markup_pct or 0),
        last_error=clone.last_error,
        created_at=clone.created_at,
    )


async def _clone_or_404(db: AsyncSession, clone_id: int) -> CloneBot:
    clone = await get_clone_bot(db, clone_id)
    if clone is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='clone_not_found')
    return clone


def _channel_sub_state(clone: CloneBot) -> ChannelSubState:
    return ChannelSubState(
        enabled=bool(clone.channel_sub_enabled),
        has_channel=bool(clone.channel_sub_chat_id),
        channel_link=clone.channel_sub_link,
        channel_title=clone.channel_sub_title,
        text=clone.channel_sub_text,
    )


def _link_to_item(link, clone: CloneBot, real_topup_kopeks: int) -> CloneLinkItem:
    return CloneLinkItem(
        id=link.id,
        name=link.name,
        url=f'https://t.me/{clone.bot_username}?start={link.start_parameter}',
        clicks_count=link.clicks_count or 0,
        registrations_count=link.registrations_count or 0,
        real_topup_kopeks=real_topup_kopeks,
        created_at=link.created_at,
    )


@router.get('', response_model=CloneBotListResponse)
async def list_clones(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:read')),
):
    clones = await list_clone_bots(db, offset=offset, limit=limit)
    stats = await get_stats_bulk(db, [c.id for c in clones])
    return CloneBotListResponse(items=[_to_item(c, stats) for c in clones], total=len(clones))


@router.get('/{clone_id}', response_model=CloneBotItem)
async def get_clone(
    clone_id: int,
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:read')),
):
    clone = await get_clone_bot(db, clone_id)
    if clone is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='clone_not_found')
    stats = await get_stats_bulk(db, [clone.id])
    return _to_item(clone, stats)


@router.get('/{clone_id}/detail', response_model=CloneBotDetail)
async def get_clone_detail(
    clone_id: int,
    users_limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:read')),
):
    """Full card for one clone: owner, aggregate stats, and the users it brought."""
    clone = await get_clone_bot(db, clone_id)
    if clone is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='clone_not_found')

    stats = await get_stats_bulk(db, [clone.id])
    base = _to_item(clone, stats)

    owner = await db.get(User, clone.owner_user_id)
    owner_dto = (
        CloneOwner(
            user_id=owner.id,
            telegram_id=owner.telegram_id,
            username=owner.username,
            full_name=_full_name(owner),
        )
        if owner is not None
        else None
    )

    active_subscribers = await count_active_subscribers(db, clone.id)
    brought = await get_brought_users(db, clone.id, limit=users_limit)
    users = [
        CloneBroughtUser(
            id=u.id,
            telegram_id=u.telegram_id,
            username=u.username,
            full_name=_full_name(u),
            status=u.status,
            balance_kopeks=u.balance_kopeks or 0,
            has_active_subscription=any(
                s.status in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)
                for s in (u.subscriptions or [])
            ),
            created_at=u.created_at,
        )
        for u in brought
    ]

    return CloneBotDetail(
        **base.model_dump(),
        owner=owner_dto,
        active_subscribers=active_subscribers,
        users=users,
        channel_sub=_channel_sub_state(clone),
    )


@router.post('/{clone_id}/toggle', response_model=ToggleResponse)
async def toggle_clone(
    clone_id: int,
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:manage')),
):
    clone = await get_clone_bot(db, clone_id)
    if clone is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='clone_not_found')
    if clone.status == CloneBotStatus.ACTIVE.value:
        await set_status(db, clone_id, CloneBotStatus.DISABLED)
        await publish_clone_event('remove', clone_id)
        new_status = CloneBotStatus.DISABLED.value
    else:
        await set_status(db, clone_id, CloneBotStatus.ACTIVE)
        await publish_clone_event('add', clone_id)
        new_status = CloneBotStatus.ACTIVE.value
    logger.info('Admin toggled clone bot', clone_id=clone_id, status=new_status, admin_id=admin.id)
    return ToggleResponse(id=clone_id, status=new_status)


@router.delete('/{clone_id}')
async def delete_clone(
    clone_id: int,
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:manage')),
):
    clone = await get_clone_bot(db, clone_id)
    if clone is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='clone_not_found')
    # Stop it on the cloner first (deletes webhook + drops from registry), clean up its empty
    # squad in the panel, then delete the row.
    await publish_clone_event('remove', clone_id)
    await cleanup_squad_on_delete(db, clone)
    await delete_clone_bot(db, clone_id)
    logger.info('Admin deleted clone bot', clone_id=clone_id, admin_id=admin.id)
    return {'ok': True}


# -- статистика по периодам (паритет с экраном 📊 в «Мои боты») -----------------

_STATS_PERIOD_DAYS: dict[str, int | None] = {'day': 1, 'week': 7, 'month': 30, 'all': None}


@router.get('/{clone_id}/stats', response_model=CloneStatsResponse)
async def clone_period_stats(
    clone_id: int,
    period: str = Query('all', pattern='^(day|week|month|all)$'),
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:read')),
):
    await _clone_or_404(db, clone_id)
    days = _STATS_PERIOD_DAYS[period]
    since = datetime.now(UTC) - timedelta(days=days) if days else None
    st = await get_period_stats(db, clone_id, since)
    return CloneStatsResponse(
        period=period,
        new_users=st['new_users'],
        purchases=st['purchases'],
        real_topup_kopeks=st['real_topup_kopeks'],
        owner_reward_kopeks=st['owner_reward_kopeks'],
        owner_reward_days=st['owner_reward_days_awards'] * settings.REFERRAL_INVITER_TOPUP_BONUS_DAYS,
    )


# -- наценка --------------------------------------------------------------------


@router.patch('/{clone_id}/markup', response_model=MarkupResponse)
async def set_markup(
    clone_id: int,
    payload: MarkupUpdateRequest,
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:manage')),
):
    await _clone_or_404(db, clone_id)
    clone = await set_pricing_markup(db, clone_id, payload.pct)
    # Цены в клонере считаются по in-memory snapshot — обновляем его на лету.
    await publish_clone_event('reload', clone_id)
    logger.info('Admin set clone markup', clone_id=clone_id, pct=payload.pct, admin_id=admin.id)
    return MarkupResponse(id=clone_id, pricing_markup_pct=int(clone.pricing_markup_pct or 0))


# -- название профиля (то, что видят клиенты в VPN-приложении) --------------------


@router.patch('/{clone_id}/title', response_model=TitleResponse)
async def rename_profile_title(
    clone_id: int,
    payload: TitleUpdateRequest,
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:manage')),
):
    title = payload.title.strip()
    if not 1 <= len(title) <= _MAX_NAME_LEN or not _NAME_RE.match(title):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='invalid_title')
    clone = await _clone_or_404(db, clone_id)

    await update_profile_title(db, clone_id, title)
    if clone.external_squad_uuid:
        try:
            await update_squad_profile_title(clone.external_squad_uuid, title)
        except Exception:
            logger.warning('Failed to update squad profile title', clone_id=clone_id, exc_info=True)
    await publish_clone_event('reload', clone_id)
    logger.info('Admin renamed clone profile', clone_id=clone_id, title=title, admin_id=admin.id)
    return TitleResponse(id=clone_id, profile_title=title)


# -- смена токена (тот же bot_id, например после отзыва в BotFather) ---------------


@router.put('/{clone_id}/token', response_model=TokenResponse)
async def rotate_token(
    clone_id: int,
    payload: TokenUpdateRequest,
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:manage')),
):
    token = payload.token.strip()
    if not _TOKEN_RE.match(token):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='invalid_token_format')
    clone = await _clone_or_404(db, clone_id)

    try:
        probe = create_bot(token=token)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='invalid_token_format')
    try:
        me = await probe.get_me()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='token_rejected')
    finally:
        try:
            await probe.session.close()
        except Exception:
            pass

    if me.id != clone.bot_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='token_wrong_bot')

    clone = await update_token(db, clone_id, token=token, bot_username=me.username, bot_title=me.full_name)
    # reload пересоздаёт Bot в клонере и переустанавливает webhook с новым токеном.
    await publish_clone_event('reload', clone_id)
    logger.info('Admin rotated clone token', clone_id=clone_id, admin_id=admin.id)
    return TokenResponse(id=clone_id, bot_username=clone.bot_username, bot_title=clone.bot_title)


# -- рекламные ссылки --------------------------------------------------------------


@router.get('/{clone_id}/links', response_model=CloneLinksResponse)
async def list_clone_links(
    clone_id: int,
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:read')),
):
    clone = await _clone_or_404(db, clone_id)
    links = await list_links(db, clone_id)
    items = []
    for link in links:
        stats = await get_link_stats(db, link.id)
        items.append(_link_to_item(link, clone, stats.get('real_topup_kopeks', 0)))
    return CloneLinksResponse(items=items)


@router.post('/{clone_id}/links', response_model=CloneLinkItem)
async def create_clone_link(
    clone_id: int,
    payload: LinkCreateRequest,
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:manage')),
):
    name = payload.name.strip()
    if not 1 <= len(name) <= 50:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='invalid_name')
    clone = await _clone_or_404(db, clone_id)
    if await count_links(db, clone_id) >= MAX_LINKS_PER_CLONE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='links_limit_reached')
    link = await create_link(db, clone_id, name)
    logger.info('Admin created clone ad link', clone_id=clone_id, link_id=link.id, admin_id=admin.id)
    return _link_to_item(link, clone, 0)


@router.delete('/{clone_id}/links/{link_id}')
async def delete_clone_link(
    clone_id: int,
    link_id: int,
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:manage')),
):
    link = await get_link(db, link_id)
    if link is None or link.clone_bot_id != clone_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='link_not_found')
    await delete_link(db, link_id)
    logger.info('Admin deleted clone ad link', clone_id=clone_id, link_id=link_id, admin_id=admin.id)
    return {'ok': True}


# -- обязательная подписка на канал владельца ---------------------------------------


@router.put('/{clone_id}/channel-sub/channel', response_model=ChannelSubState)
async def set_channel_sub(
    clone_id: int,
    payload: ChannelSubChannelRequest,
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:manage')),
):
    ref = parse_channel_ref(payload.channel)
    if ref is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='invalid_channel_ref')
    clone = await _clone_or_404(db, clone_id)
    try:
        chat_id, link, title = await verify_clone_channel(clone, ref)
    except CloneChannelVerifyError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.code)
    clone = await set_channel_sub_channel(db, clone_id, chat_id=chat_id, link=link, title=title)
    await publish_clone_event('reload', clone_id)
    logger.info('Admin set clone sub channel', clone_id=clone_id, chat_id=chat_id, admin_id=admin.id)
    return _channel_sub_state(clone)


@router.post('/{clone_id}/channel-sub/toggle', response_model=ChannelSubState)
async def toggle_channel_sub(
    clone_id: int,
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:manage')),
):
    clone = await _clone_or_404(db, clone_id)
    if not clone.channel_sub_chat_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='channel_not_set')
    enable = not clone.channel_sub_enabled
    if enable:
        # Перед включением перепроверяем, что бот всё ещё админ канала — права могли снять.
        try:
            await verify_clone_channel(clone, clone.channel_sub_chat_id)
        except CloneChannelVerifyError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.code)
    clone = await set_channel_sub_enabled(db, clone_id, enable)
    await publish_clone_event('reload', clone_id)
    logger.info('Admin toggled clone channel sub', clone_id=clone_id, enabled=enable, admin_id=admin.id)
    return _channel_sub_state(clone)


@router.patch('/{clone_id}/channel-sub/text', response_model=ChannelSubState)
async def set_channel_sub_stub_text(
    clone_id: int,
    payload: ChannelSubTextRequest,
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:manage')),
):
    text = (payload.text or '').strip() or None  # пусто = вернуть стандартный текст
    if text is not None and len(text) > 1000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='text_too_long')
    await _clone_or_404(db, clone_id)
    clone = await set_channel_sub_text(db, clone_id, text)
    await publish_clone_event('reload', clone_id)
    logger.info('Admin set clone sub text', clone_id=clone_id, custom=text is not None, admin_id=admin.id)
    return _channel_sub_state(clone)


# -- рассылки -----------------------------------------------------------------------

_MAX_BROADCAST_PHOTO_BYTES = 10 * 1024 * 1024  # лимит Telegram на фото


def _broadcast_to_item(b) -> CloneBroadcastItem:
    return CloneBroadcastItem(
        id=b.id,
        status=b.status,
        message_text=b.message_text,
        media_type=b.media_type,
        button_text=b.button_text,
        button_url=b.button_url,
        show_tariffs_button=bool(b.show_tariffs_button),
        sent_count=b.sent_count or 0,
        failed_count=b.failed_count or 0,
        total_count=b.total_count or 0,
        created_at=b.created_at,
    )


@router.get('/{clone_id}/broadcasts', response_model=CloneBroadcastsResponse)
async def list_clone_broadcasts(
    clone_id: int,
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:read')),
):
    await _clone_or_404(db, clone_id)
    items = await list_broadcasts(db, clone_id, limit=10)
    used_today = await count_today(db, clone_id)
    recipients = len(await get_recipient_telegram_ids(db, clone_id))
    return CloneBroadcastsResponse(
        items=[_broadcast_to_item(b) for b in items],
        used_today=used_today,
        recipients=recipients,
    )


@router.post('/{clone_id}/broadcasts', response_model=CloneBroadcastItem)
async def create_clone_broadcast(
    clone_id: int,
    text: str | None = Form(None),
    button_text: str | None = Form(None),
    button_url: str | None = Form(None),
    show_tariffs: bool = Form(False),
    photo: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_cabinet_db),
    admin: User = Depends(require_permission('clone_bots:manage')),
):
    """Запустить рассылку от имени клон-бота по его юзерам (лимит в сутки — как в боте).

    Фото уходит байтами напрямую в ``run_clone_broadcast`` (в кабинете нет file_id
    основного бота), текст — подписью. Отправка фоновая; статус — в списке рассылок.
    """
    clone = await _clone_or_404(db, clone_id)

    text = (text or '').strip() or None
    button_text = (button_text or '').strip() or None
    button_url = (button_url or '').strip() or None

    photo_bytes: bytes | None = None
    if photo is not None:
        if not (photo.content_type or '').startswith('image/'):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='invalid_photo')
        photo_bytes = await photo.read()
        if not photo_bytes:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='invalid_photo')
        if len(photo_bytes) > _MAX_BROADCAST_PHOTO_BYTES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='photo_too_large')

    if photo_bytes is not None:
        if text is not None and len(text) > 1024:  # лимит Telegram на caption
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='text_too_long')
    else:
        if text is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='invalid_text')
        if len(text) > 4000:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='text_too_long')

    if bool(button_text) != bool(button_url) or (
        button_url and not button_url.startswith(('http://', 'https://'))
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='invalid_button')

    if await count_today(db, clone_id) >= CLONE_BROADCASTS_PER_DAY:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='broadcasts_limit_reached')
    recipients = await get_recipient_telegram_ids(db, clone_id)
    if not recipients:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='no_recipients')

    broadcast = await create_broadcast(
        db,
        clone_id,
        message_text=text,
        media_type='photo' if photo_bytes is not None else None,
        media_file_id=None,
        button_text=button_text[:64] if button_text else None,
        button_url=button_url,
        show_tariffs_button=show_tariffs,
        total_count=len(recipients),
    )
    token = get_decrypted_token(clone)
    asyncio.create_task(run_clone_broadcast(broadcast, clone_token=token, media_bytes=photo_bytes))
    logger.info(
        'Admin started clone broadcast',
        clone_id=clone_id,
        broadcast_id=broadcast.id,
        recipients=len(recipients),
        admin_id=admin.id,
    )
    return _broadcast_to_item(broadcast)
