"""CRUD for white-label clone bots (``clone_bots`` table).

Encapsulates token encryption (via :mod:`app.utils.crypto`) and the CRM stat queries
(users brought / revenue) so callers never touch the raw token or hand-roll aggregates.
"""

from __future__ import annotations

import secrets

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.models import (
    CloneBot,
    CloneBotStatus,
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    User,
)
from app.utils.crypto import decrypt_secret, encrypt_secret


logger = structlog.get_logger(__name__)


async def create_clone_bot(
    db: AsyncSession,
    *,
    owner_user_id: int,
    bot_id: int,
    token: str,
    bot_username: str | None = None,
    bot_title: str | None = None,
    status: CloneBotStatus = CloneBotStatus.PENDING,
) -> CloneBot:
    """Create a clone-bot row. ``token`` is the plaintext BotFather token; it is
    encrypted at rest. A per-clone webhook secret is generated automatically."""
    clone = CloneBot(
        owner_user_id=owner_user_id,
        bot_id=bot_id,
        bot_username=bot_username,
        bot_title=bot_title,
        token_encrypted=encrypt_secret(token),
        webhook_secret=secrets.token_urlsafe(32),
        status=status.value,
    )
    db.add(clone)
    await db.commit()
    await db.refresh(clone)
    return clone


def get_decrypted_token(clone: CloneBot) -> str:
    """Return the plaintext bot token for a clone (decrypts at use site only)."""
    return decrypt_secret(clone.token_encrypted)


async def get_clone_bot(db: AsyncSession, clone_id: int) -> CloneBot | None:
    return await db.get(CloneBot, clone_id)


async def get_clone_bot_by_bot_id(db: AsyncSession, bot_id: int) -> CloneBot | None:
    result = await db.execute(select(CloneBot).where(CloneBot.bot_id == bot_id))
    return result.scalar_one_or_none()


async def list_clone_bots(
    db: AsyncSession,
    *,
    owner_user_id: int | None = None,
    status: CloneBotStatus | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> list[CloneBot]:
    query = select(CloneBot)
    if owner_user_id is not None:
        query = query.where(CloneBot.owner_user_id == owner_user_id)
    if status is not None:
        query = query.where(CloneBot.status == status.value)
    query = query.order_by(CloneBot.created_at.desc(), CloneBot.id.desc())
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())


async def list_active_clone_bots(db: AsyncSession) -> list[CloneBot]:
    """Active clones — used by the cloner registry cold-start and reconcile."""
    return await list_clone_bots(db, status=CloneBotStatus.ACTIVE)


async def count_active(db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count(CloneBot.id)).where(CloneBot.status == CloneBotStatus.ACTIVE.value)
    )
    return int(result.scalar() or 0)


async def count_for_owner(db: AsyncSession, owner_user_id: int, *, exclude_disabled: bool = True) -> int:
    query = select(func.count(CloneBot.id)).where(CloneBot.owner_user_id == owner_user_id)
    if exclude_disabled:
        query = query.where(CloneBot.status != CloneBotStatus.DISABLED.value)
    result = await db.execute(query)
    return int(result.scalar() or 0)


async def set_status(
    db: AsyncSession, clone_id: int, status: CloneBotStatus, *, last_error: str | None = None
) -> CloneBot | None:
    clone = await db.get(CloneBot, clone_id)
    if clone is None:
        return None
    clone.status = status.value
    # Keep last_error sticky only for ERROR; clear it on a successful transition.
    clone.last_error = last_error if status == CloneBotStatus.ERROR else None
    await db.commit()
    await db.refresh(clone)
    return clone


async def set_squad(
    db: AsyncSession,
    clone_id: int,
    *,
    external_squad_uuid: str,
    external_squad_name: str,
    profile_title: str | None = None,
    subpage_config_uuid: str | None = None,
) -> CloneBot | None:
    clone = await db.get(CloneBot, clone_id)
    if clone is None:
        return None
    clone.external_squad_uuid = external_squad_uuid
    clone.external_squad_name = external_squad_name
    clone.profile_title = profile_title
    clone.subpage_config_uuid = subpage_config_uuid
    await db.commit()
    await db.refresh(clone)
    return clone


async def update_profile_title(db: AsyncSession, clone_id: int, profile_title: str) -> CloneBot | None:
    """Change the client-facing display name of a clone (the squad's profile title).

    The external squad's *name* is immutable in the panel, but its ``profileTitle`` —
    what the rebrand logic and VPN clients show — can be changed freely. Callers should
    also update the panel-side title (``update_squad_profile_title``) and publish a
    ``reload`` so the cloner's in-memory snapshot picks up the new title.
    """
    clone = await db.get(CloneBot, clone_id)
    if clone is None:
        return None
    clone.profile_title = profile_title
    await db.commit()
    await db.refresh(clone)
    return clone


async def update_token(
    db: AsyncSession,
    clone_id: int,
    *,
    token: str,
    bot_username: str | None = None,
    bot_title: str | None = None,
) -> CloneBot | None:
    """Replace a clone's BotFather token (re-encrypted at rest) without recreating it.

    Used when a reseller re-issues/revokes the token of the *same* bot. ``bot_id`` is
    intentionally left untouched — callers must verify via getMe that the new token
    belongs to the same bot before calling. Publish a ``reload`` afterwards so the cloner
    rebuilds the ``Bot`` and re-asserts the webhook with the new token.
    """
    clone = await db.get(CloneBot, clone_id)
    if clone is None:
        return None
    clone.token_encrypted = encrypt_secret(token)
    if bot_username is not None:
        clone.bot_username = bot_username
    if bot_title is not None:
        clone.bot_title = bot_title
    await db.commit()
    await db.refresh(clone)
    return clone


async def set_pricing_markup(db: AsyncSession, clone_id: int, pct: int) -> CloneBot | None:
    """Наценка клона на тарифы, % (валидация 0–500 — на вызывающей стороне).

    После вызова нужен ``publish_clone_event('reload', …)`` — цены в клонере
    считаются по in-memory snapshot."""
    clone = await db.get(CloneBot, clone_id)
    if clone is None:
        return None
    clone.pricing_markup_pct = pct
    await db.commit()
    await db.refresh(clone)
    return clone


async def sum_purchases_kopeks(db: AsyncSession, clone_id: int, since=None) -> int:
    """Сумма покупок/продлений подписок юзерами клона за период (для оценки
    «сколько принесла наценка»)."""
    where = [
        Transaction.clone_bot_id == clone_id,
        Transaction.is_completed.is_(True),
        Transaction.amount_kopeks > 0,
        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
    ]
    if since is not None:
        where.append(Transaction.created_at >= since)
    result = await db.execute(select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(*where))
    return int(result.scalar() or 0)


async def set_channel_sub_channel(
    db: AsyncSession,
    clone_id: int,
    *,
    chat_id: int,
    link: str,
    title: str | None,
) -> CloneBot | None:
    """Привязать канал обязательной подписки (уже проверенный: клон-бот — админ канала).

    После вызова нужен ``publish_clone_event('reload', …)`` — enforcement в клонере
    читает эти поля из in-memory snapshot, а не из БД.
    """
    clone = await db.get(CloneBot, clone_id)
    if clone is None:
        return None
    clone.channel_sub_chat_id = chat_id
    clone.channel_sub_link = link
    clone.channel_sub_title = title
    await db.commit()
    await db.refresh(clone)
    return clone


async def set_channel_sub_enabled(db: AsyncSession, clone_id: int, enabled: bool) -> CloneBot | None:
    clone = await db.get(CloneBot, clone_id)
    if clone is None:
        return None
    clone.channel_sub_enabled = enabled
    await db.commit()
    await db.refresh(clone)
    return clone


async def set_channel_sub_text(db: AsyncSession, clone_id: int, text: str | None) -> CloneBot | None:
    """Кастомный текст заглушки «подпишитесь на канал». ``None`` — вернуть дефолтный."""
    clone = await db.get(CloneBot, clone_id)
    if clone is None:
        return None
    clone.channel_sub_text = text
    await db.commit()
    await db.refresh(clone)
    return clone


async def delete_clone_bot(db: AsyncSession, clone_id: int) -> bool:
    clone = await db.get(CloneBot, clone_id)
    if clone is None:
        return False
    await db.delete(clone)
    await db.commit()
    return True


# --- CRM stats ---


async def count_brought_users(db: AsyncSession, clone_id: int) -> int:
    result = await db.execute(select(func.count(User.id)).where(User.clone_bot_id == clone_id))
    return int(result.scalar() or 0)


async def sum_revenue_kopeks(db: AsyncSession, clone_id: int) -> int:
    result = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
            Transaction.clone_bot_id == clone_id,
            Transaction.is_completed.is_(True),
            Transaction.amount_kopeks > 0,
        )
    )
    return int(result.scalar() or 0)


async def get_stats_bulk(db: AsyncSession, clone_ids: list[int]) -> dict[int, dict[str, int]]:
    """Users-brought + revenue + real top-ups for many clones in 3 grouped queries.

    - ``revenue_kopeks``: ALL positive completed transactions (includes internal
      welcome/referral bonus credits — a gross turnover figure).
    - ``real_topup_kopeks``: only DEPOSITs settled through a real payment gateway
      (``REAL_PAYMENT_METHODS`` — excludes BALANCE, admin MANUAL credits and bonuses),
      i.e. actual money the clone's users brought in.
    """
    stats: dict[int, dict[str, int]] = {
        cid: {'users': 0, 'revenue_kopeks': 0, 'real_topup_kopeks': 0} for cid in clone_ids
    }
    if not clone_ids:
        return stats

    users_rows = await db.execute(
        select(User.clone_bot_id, func.count(User.id))
        .where(User.clone_bot_id.in_(clone_ids))
        .group_by(User.clone_bot_id)
    )
    for clone_id, cnt in users_rows.all():
        stats[clone_id]['users'] = int(cnt or 0)

    revenue_rows = await db.execute(
        select(Transaction.clone_bot_id, func.coalesce(func.sum(Transaction.amount_kopeks), 0))
        .where(
            Transaction.clone_bot_id.in_(clone_ids),
            Transaction.is_completed.is_(True),
            Transaction.amount_kopeks > 0,
        )
        .group_by(Transaction.clone_bot_id)
    )
    for clone_id, total in revenue_rows.all():
        stats[clone_id]['revenue_kopeks'] = int(total or 0)

    topup_rows = await db.execute(
        select(Transaction.clone_bot_id, func.coalesce(func.sum(Transaction.amount_kopeks), 0))
        .where(
            Transaction.clone_bot_id.in_(clone_ids),
            Transaction.is_completed.is_(True),
            Transaction.amount_kopeks > 0,
            Transaction.type == TransactionType.DEPOSIT.value,
            Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
        )
        .group_by(Transaction.clone_bot_id)
    )
    for clone_id, total in topup_rows.all():
        stats[clone_id]['real_topup_kopeks'] = int(total or 0)

    return stats


async def get_period_stats(db: AsyncSession, clone_id: int, since=None) -> dict[str, int]:
    """Статистика клона за период (``since=None`` — за всё время) для экрана 📊.

    - ``new_users`` — новые юзеры клона;
    - ``purchases`` — покупки/продления подписок (число транзакций);
    - ``real_topup_kopeks`` — реальные пополнения через платёжные шлюзы;
    - ``owner_reward_kopeks`` — начислено владельцу-партнёру (клон-комиссия);
    - ``owner_reward_days_awards`` — сколько раз владельцу-непартнёру капнули бонусные
      дни (дней за раз — ``settings.REFERRAL_INVITER_TOPUP_BONUS_DAYS``, множит хендлер).
    """
    from app.database.models import ReferralEarning

    def _period(col, *conds):
        where = list(conds)
        if since is not None:
            where.append(col >= since)
        return where

    new_users = await db.execute(
        select(func.count(User.id)).where(*_period(User.created_at, User.clone_bot_id == clone_id))
    )
    purchases = await db.execute(
        select(func.count(Transaction.id)).where(
            *_period(
                Transaction.created_at,
                Transaction.clone_bot_id == clone_id,
                Transaction.is_completed.is_(True),
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            )
        )
    )
    topup = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
            *_period(
                Transaction.created_at,
                Transaction.clone_bot_id == clone_id,
                Transaction.is_completed.is_(True),
                Transaction.amount_kopeks > 0,
                Transaction.type == TransactionType.DEPOSIT.value,
                Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
            )
        )
    )
    payer = ReferralEarning.referral_id == User.id
    reward_money = await db.execute(
        select(func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0))
        .select_from(ReferralEarning)
        .join(User, payer)
        .where(
            *_period(
                ReferralEarning.created_at,
                User.clone_bot_id == clone_id,
                ReferralEarning.reason == 'clone_owner_commission_topup',
            )
        )
    )
    reward_days = await db.execute(
        select(func.count(ReferralEarning.id))
        .select_from(ReferralEarning)
        .join(User, payer)
        .where(
            *_period(
                ReferralEarning.created_at,
                User.clone_bot_id == clone_id,
                ReferralEarning.reason == 'clone_owner_topup_days',
            )
        )
    )
    return {
        'new_users': int(new_users.scalar() or 0),
        'purchases': int(purchases.scalar() or 0),
        'real_topup_kopeks': int(topup.scalar() or 0),
        'owner_reward_kopeks': int(reward_money.scalar() or 0),
        'owner_reward_days_awards': int(reward_days.scalar() or 0),
    }


# --- detail view (one clone) ---


_ACTIVE_SUB_STATUSES = (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)


async def get_brought_users(
    db: AsyncSession, clone_id: int, *, offset: int = 0, limit: int = 50
) -> list[User]:
    """Recent users brought by a clone, newest first. Subscriptions are eager-loaded
    (single extra query, not N+1) so callers can flag who has an active sub."""
    result = await db.execute(
        select(User)
        .where(User.clone_bot_id == clone_id)
        .options(selectinload(User.subscriptions))
        .order_by(User.created_at.desc(), User.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_active_subscribers(db: AsyncSession, clone_id: int) -> int:
    """How many of a clone's users currently hold an active/trial subscription."""
    result = await db.execute(
        select(func.count(func.distinct(User.id)))
        .select_from(User)
        .join(Subscription, Subscription.user_id == User.id)
        .where(User.clone_bot_id == clone_id, Subscription.status.in_(_ACTIVE_SUB_STATUSES))
    )
    return int(result.scalar() or 0)
