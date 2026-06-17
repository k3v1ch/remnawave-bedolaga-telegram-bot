"""Admin cabinet routes for white-label clone bots (CRM).

Lists clones with per-clone stats (users brought + revenue), and lets an admin
enable/disable (kill-switch) or delete a clone. Enable/disable/delete publish a hot-swap
event so the cloner host applies it live (no restart).
"""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.clone_bot import (
    count_active_subscribers,
    delete_clone_bot,
    get_brought_users,
    get_clone_bot,
    get_stats_bulk,
    list_clone_bots,
    set_status,
)
from app.database.models import CloneBotStatus, SubscriptionStatus, User
from app.services.clone_runtime.coordinator import publish_clone_event

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/clone-bots', tags=['Cabinet Admin Clone Bots'])


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


class CloneBotDetail(CloneBotItem):
    owner: CloneOwner | None = None
    active_subscribers: int = 0
    users: list[CloneBroughtUser] = []


class ToggleResponse(BaseModel):
    id: int
    status: str


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
        last_error=clone.last_error,
        created_at=clone.created_at,
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
    # Stop it on the cloner first (deletes webhook + drops from registry), then delete the row.
    await publish_clone_event('remove', clone_id)
    await delete_clone_bot(db, clone_id)
    logger.info('Admin deleted clone bot', clone_id=clone_id, admin_id=admin.id)
    return {'ok': True}
