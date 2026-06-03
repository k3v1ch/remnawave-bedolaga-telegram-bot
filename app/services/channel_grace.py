"""Grace-period helpers for channel-leave subscription disabling.

When a user leaves a required channel we no longer disable their VPN
immediately. Instead we open a grace window (``CHANNEL_LEAVE_GRACE_HOURS``)
and warn the user. If they resubscribe before the deadline the VPN is never
interrupted; otherwise the next reconciliation pass disables it.

All three code paths funnel through these helpers so the behaviour stays
consistent:
  * real-time ChatMemberUpdated handler (``handlers/channel_member.py``)
  * channel-checker middleware (``middlewares/channel_checker.py``)
  * monitoring reconciliation loop (``services/monitoring_service.py``)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot

from app.config import settings
from app.localization.loader import DEFAULT_LANGUAGE
from app.localization.texts import get_texts


logger = structlog.get_logger(__name__)

# Outcomes of evaluate_grace()
GRACE_STARTED = 'started'  # window just opened -> warn, do NOT disable
GRACE_PENDING = 'pending'  # still inside the window -> do nothing
GRACE_EXPIRED = 'expired'  # deadline passed / grace disabled -> disable now


def grace_hours() -> int:
    """Configured grace window in hours (0 = disable immediately)."""
    try:
        return max(0, int(getattr(settings, 'CHANNEL_LEAVE_GRACE_HOURS', 0)))
    except (TypeError, ValueError):
        return 0


def is_grace_enabled() -> bool:
    return grace_hours() > 0


def evaluate_grace(user, now: datetime | None = None) -> str:
    """Decide what to do for a user unsubscribed from required channel(s).

    Opens a new grace window by setting ``user.channel_grace_until`` when none
    is pending. The caller is responsible for committing the session.
    """
    if not is_grace_enabled():
        return GRACE_EXPIRED  # legacy: disable immediately

    now = now or datetime.now(UTC)
    deadline = getattr(user, 'channel_grace_until', None)

    if deadline is None:
        user.channel_grace_until = now + timedelta(hours=grace_hours())
        return GRACE_STARTED

    # Defensive: column is timezone-aware, but normalise just in case.
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)

    if now >= deadline:
        return GRACE_EXPIRED
    return GRACE_PENDING


def clear_grace(user) -> bool:
    """Cancel any pending grace window. Returns True if one was set."""
    if getattr(user, 'channel_grace_until', None) is not None:
        user.channel_grace_until = None
        return True
    return False


async def send_grace_warning(bot: Bot, user, channels: list[dict] | None = None) -> bool:
    """Warn the user that they left a required channel and a countdown started."""
    # Local import avoids a module-load circular import via app.keyboards.inline.
    from app.keyboards.inline import get_channel_sub_keyboard

    language = getattr(user, 'language', None) or DEFAULT_LANGUAGE
    try:
        texts = get_texts(language)
        notification_text = texts.t(
            'SUBSCRIPTION_CHANNEL_LEAVE_GRACE_WARNING',
            '⚠️ Вы отписались от обязательного канала.\n\n'
            'У вас есть {hours} ч, чтобы вернуться в канал — иначе доступ к VPN '
            'будет приостановлен. Подпишитесь снова, чтобы сохранить доступ.',
        ).format(hours=grace_hours())
        keyboard = get_channel_sub_keyboard(channels or None, language=language)
        await bot.send_message(user.telegram_id, notification_text, reply_markup=keyboard)
        return True
    except Exception as error:
        logger.warning(
            'Failed to send channel grace warning',
            telegram_id=getattr(user, 'telegram_id', None),
            error=error,
        )
        return False


async def send_grace_cancelled(bot: Bot, user) -> bool:
    """Confirm the user's access was preserved after resubscribing in time."""
    language = getattr(user, 'language', None) or DEFAULT_LANGUAGE
    try:
        texts = get_texts(language)
        notification_text = texts.t(
            'SUBSCRIPTION_CHANNEL_GRACE_CANCELLED',
            '✅ Спасибо, что вернулись в канал! Доступ к VPN сохранён.',
        )
        await bot.send_message(user.telegram_id, notification_text)
        return True
    except Exception as error:
        logger.warning(
            'Failed to send channel grace cancelled notice',
            telegram_id=getattr(user, 'telegram_id', None),
            error=error,
        )
        return False
