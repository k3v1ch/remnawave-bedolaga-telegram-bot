"""KELDARI-UI: заглушки кнопок фич без бэкенда (стратегия «полный визуальный клон»).

Контракт: мёртвые кнопки используют ``callback_data="keldari_soon:<feature>"``,
где ``<feature>`` ∈ {stories, post, tiktok, create_vpn, worker_links, gifts, profile}.
Хендлер отвечает алертом «скоро» (текст — через locale-ключ ``KELDARI_SOON_<FEATURE>``).
По мере реализации фич соответствующие кнопки перенаправляются на реальные хендлеры.
"""

from __future__ import annotations

import structlog
from aiogram import Dispatcher, F, types

from app.database.models import User
from app.localization.texts import get_texts


logger = structlog.get_logger(__name__)


KELDARI_SOON_DEFAULTS = {
    'stories': '✨ Бонус «7 дней за сторис» скоро будет доступен — раздел в разработке.',
    'post': '✨ Бонус «7 дней за пост» скоро будет доступен — раздел в разработке.',
    'tiktok': '🔥 Раздел «Платим за TikTok» в разработке — скоро запустим.',
    'create_vpn': '🤖 «Создать свой VPN» появится позже — функция в разработке.',
    'worker_links': '👷 Раздел «Рабочие ссылки» в разработке.',
    'gifts': '🎁 Подарки скоро появятся — раздел в разработке.',
    'profile': '👤 Профиль в боте скоро появится. Пока ваши данные доступны в приложении («Открыть приложение»).',
}

KELDARI_SOON_DEFAULT = '🔧 Раздел в разработке — скоро появится.'


async def show_soon_stub(callback: types.CallbackQuery, db_user: User):
    """Алерт-заглушка для кнопок фич без бэкенда (callback ``keldari_soon:<feature>``)."""
    try:
        data = callback.data or ''
        feature = data.split(':', 1)[1] if ':' in data else ''
        texts = get_texts(db_user.language)
        default = KELDARI_SOON_DEFAULTS.get(feature, KELDARI_SOON_DEFAULT)
        key = f'KELDARI_SOON_{feature.upper()}' if feature else 'KELDARI_SOON_DEFAULT'
        await callback.answer(texts.t(key, default), show_alert=True)
    except Exception as error:
        logger.debug('KELDARI-UI: ошибка заглушки keldari_soon', error=error)
        try:
            await callback.answer(KELDARI_SOON_DEFAULT, show_alert=True)
        except Exception:
            pass


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_soon_stub, F.data.startswith('keldari_soon:'))
