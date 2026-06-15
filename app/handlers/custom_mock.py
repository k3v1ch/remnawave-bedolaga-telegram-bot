"""CUSTOM-UI: навигируемые экраны-реплики макета ВЕРНО VPN для фич без бэкенда.

Принцип (решение владельца): экраны воспроизводят макет визуально и КЛИКАЮТСЯ
(реальные переходы между экранами), но НЕ выполняют операций в бэкенде —
никаких начислений/записей. Это демо-витрина, которую позже наполняем реальной
логикой. Инертные действия → явный демо-алерт ``kmock_alert:<key>`` («ничего не
начислено»), чтобы не путать заглушку с рабочей функцией.

Реализованные экраны:
- ``kmock_ref_stories`` — SCR-REF-STORIES (7 дней за сторис);
- ``kmock_ref_post``    — SCR-REF-POST (7 дней за пост).
"""

from __future__ import annotations

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.database.models import User
from app.localization.texts import get_texts
from app.utils.photo_message import edit_or_answer_photo


logger = structlog.get_logger(__name__)


CUSTOM_REF_STORIES_SCREEN_DEFAULT = (
    '🥴 Целая неделя бесплатного VPN за сторис в Telegram\n'
    '\n'
    'Условия:\n'
    '1. Выложите у себя в сторис Telegram изображение с реферальной ссылкой в подписи на нашего бота.\n'
    '2. После 40 часов с момента публикации, отправьте скрин сторис нашей поддержке.\n'
    '3. Получите неделю премиальной подписки абсолютно бесплатно.\n'
    '\n'
    '⭐️ Акцией могут воспользоваться только пользователи с Telegram Premium, не более одного раза.\n'
    '\n'
    '🔗 Ссылка для подписи — {bot_ref_link}'
)

CUSTOM_REF_POST_SCREEN_DEFAULT = (
    '🥴 Целая неделя бесплатного VPN за пост в Telegram\n'
    '\n'
    'Условия:\n'
    '1. Выложите у себя на канале Telegram изображение с реферальной ссылкой в подписи на нашего бота.\n'
    '2. После 24 часов с момента публикации, перешлите пост нашей поддержке.\n'
    '3. Получите неделю премиальной подписки абсолютно бесплатно.\n'
    '\n'
    '⭐️ Акцией могут воспользоваться только пользователи с активной аудиторией от 100 подписчиков, не более одного раза.\n'
    '\n'
    '🔗 Ссылка для подписи — {bot_ref_link}'
)

# Инертные действия — демо-алерты (ничего не шлют в бэкенд)
CUSTOM_DEMO_ALERTS = {
    'bonus': '🔧 Демо-режим: бонус не начисляется (функция в разработке).',
    'image': '🔧 Демо-режим: изображение для публикации пока недоступно.',
    'apply': '🔧 Демо-режим: подача заявки в разработке (ничего не отправлено).',
    'create_bot': '🔧 Демо-режим: создание бота в разработке (ничего не создано).',
}
CUSTOM_DEMO_ALERT_DEFAULT = '🔧 Демо-режим: действие не выполняется (функция в разработке).'

# Подсказка вместо реальной реф-ссылки (без обращения к бэкенду)
CUSTOM_REF_LINK_HINT = '(ваша ссылка — в разделе «Реферальная программа»)'


def _bonus_screen_keyboard(texts) -> InlineKeyboardMarkup:
    """Клавиатура SCR-REF-STORIES / SCR-REF-POST (kb_ref_stories эталона)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('CUSTOM_REF_GET7_BUTTON', 'Получить 7 дней'),
                    callback_data='kmock_alert:bonus',
                    style='success',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('CUSTOM_REF_DOWNLOAD_IMAGE_BUTTON', 'Скачать изображение'),
                    callback_data='kmock_alert:image',
                    style='success',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('CUSTOM_REF_SUPPORT_BUTTON', 'Написать в поддержку'),
                    callback_data='menu_support',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('CUSTOM_BACK_BUTTON', '‹ Назад'),
                    callback_data='menu_referrals',
                )
            ],
        ]
    )


async def _render_bonus_screen(callback: types.CallbackQuery, db_user: User, key: str, default_text: str):
    texts = get_texts(db_user.language)
    raw = texts.t(key, default_text)
    text = raw.replace('{bot_ref_link}', CUSTOM_REF_LINK_HINT)
    try:
        await edit_or_answer_photo(
            callback=callback,
            caption=text,
            keyboard=_bonus_screen_keyboard(texts),
            parse_mode='HTML',
        )
        await callback.answer()
    except Exception as error:
        logger.debug('CUSTOM-UI: ошибка рендера mock-экрана', key=key, error=error)
        await callback.answer()


async def show_ref_stories(callback: types.CallbackQuery, db_user: User):
    await _render_bonus_screen(callback, db_user, 'CUSTOM_REF_STORIES_SCREEN', CUSTOM_REF_STORIES_SCREEN_DEFAULT)


async def show_ref_post(callback: types.CallbackQuery, db_user: User):
    await _render_bonus_screen(callback, db_user, 'CUSTOM_REF_POST_SCREEN', CUSTOM_REF_POST_SCREEN_DEFAULT)


async def show_demo_alert(callback: types.CallbackQuery, db_user: User):
    """Инертное действие демо-экрана: алерт без обращения к бэкенду."""
    try:
        data = callback.data or ''
        key = data.split(':', 1)[1] if ':' in data else ''
        texts = get_texts(db_user.language)
        default = CUSTOM_DEMO_ALERTS.get(key, CUSTOM_DEMO_ALERT_DEFAULT)
        loc_key = f'CUSTOM_DEMO_{key.upper()}' if key else 'CUSTOM_DEMO_DEFAULT'
        await callback.answer(texts.t(loc_key, default), show_alert=True)
    except Exception as error:
        logger.debug('CUSTOM-UI: ошибка demo-алерта', error=error)
        try:
            await callback.answer(CUSTOM_DEMO_ALERT_DEFAULT, show_alert=True)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# SCR-TIKTOK / SCR-TIKTOK-RULES / SCR-CREATE-VPN
# Входные экраны воспроизведены навигируемо; глубокие флоу (6-шаговая заявка,
# создание бота, white-label-управление) — демо-алерты (kmock_alert:*).
# ─────────────────────────────────────────────────────────────

CUSTOM_TIKTOK_SCREEN_DEFAULT = (
    '🔥 Зарабатывайте на коротких видео!\n'
    '\n'
    'Снимайте ролики, продвигайте ВЕРНО VPN и получайте реальные деньги за просмотры.\n'
    '\n'
    '💰 Сколько можно заработать:\n'
    '\n'
    '🎯 Целевой контент:\n'
    '├ от 125 000 просмотров — 725₽\n'
    '├ от 250 000 просмотров — 1 450₽\n'
    '├ от 500 000 просмотров — 2 900₽\n'
    '└ от 1 000 000 просмотров — 5 800₽\n'
    '\n'
    '📹 Нецелевой контент:\n'
    '├ от 125 000 просмотров — 375₽\n'
    '├ от 250 000 просмотров — 750₽\n'
    '├ от 500 000 просмотров — 1 500₽\n'
    '└ от 1 000 000 просмотров — 3 000₽\n'
    '\n'
    '➕ Бонус: 15% от оплат привлечённых клиентов первые 12 месяцев\n'
    '\n'
    '📌 Площадки: TikTok · Instagram Reels · YouTube Shorts'
)

CUSTOM_TIKTOK_RULES_SCREEN_DEFAULT = (
    '📋 Условия участия\n'
    '\n'
    '✦ Баннер ВЕРНО VPN должен быть виден на протяжении всего видео\n'
    '✦ Баннер не должен перекрываться описанием или интерфейсом\n'
    '✦ В профиле — ссылка на сайт: vernovpn.ru\n'
    '✦ В описании ролика — тег #ВЕРНОVPN\n'
    '✦ Учитываются ролики от 125 000 просмотров\n'
    '\n'
    '🎯 Целевой контент — ролики про VPN, интернет-безопасность, обходы блокировок\n'
    '📹 Нецелевой — любые другие темы с баннером\n'
    '\n'
    '💸 Выплаты:\n'
    '├ Вознаграждение за просмотры — по сетке выше\n'
    '├ 15% от оплат клиентов по Вашей ссылке\n'
    '└ Процент может пересматриваться раз в полгода'
)

CUSTOM_CREATE_VPN_SCREEN_DEFAULT = (
    '💸 Создайте своего VPN-бота, монетезируйте свою аудиторию и зарабатывайте с каждой продажи!\n'
    '\n'
    'У Вас есть трафик, канал, чат или комьюнити?\n'
    'Превратите это в доход и получайте до 90% с оплат в вашем боте!\n'
    '\n'
    '⚡ Как это работает:\n'
    '1. Создаете или подключаете уже существующего бота\n'
    '2. Подключаетесь к нашей инфраструктуре с помощью API\n'
    '3. Привлекаете аудиторию, получаете прибыль'
)


def _tiktok_keyboard(texts) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('CUSTOM_TIKTOK_RULES_BUTTON', '📋 Условия и правила'), callback_data='kmock_tiktok_rules')],
            [InlineKeyboardButton(text=texts.t('CUSTOM_TIKTOK_APPLY_BUTTON', '📝 Подать заявку'), callback_data='kmock_alert:apply', style='primary')],
            [InlineKeyboardButton(text=texts.t('CUSTOM_BACK_BUTTON', '‹ Назад'), callback_data='menu_referrals')],
        ]
    )


def _tiktok_rules_keyboard(texts) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('CUSTOM_TIKTOK_APPLY_BUTTON', '📝 Подать заявку'), callback_data='kmock_alert:apply', style='primary')],
            [InlineKeyboardButton(text=texts.t('CUSTOM_BACK_BUTTON', '‹ Назад'), callback_data='kmock_tiktok')],
        ]
    )


def _create_vpn_keyboard(texts) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('CUSTOM_CREATE_VPN_BUTTON', 'Создать своего VPN-бота'), callback_data='kmock_alert:create_bot', style='primary')],
            [InlineKeyboardButton(text=texts.t('CUSTOM_BACK_BUTTON', '‹ Назад'), callback_data='menu_referrals')],
        ]
    )


async def _render_static(callback: types.CallbackQuery, db_user: User, key: str, default_text: str, keyboard_fn):
    texts = get_texts(db_user.language)
    try:
        await edit_or_answer_photo(
            callback=callback,
            caption=texts.t(key, default_text),
            keyboard=keyboard_fn(texts),
            parse_mode='HTML',
        )
        await callback.answer()
    except Exception as error:
        logger.debug('CUSTOM-UI: ошибка рендера mock-экрана', key=key, error=error)
        await callback.answer()


async def show_tiktok(callback: types.CallbackQuery, db_user: User):
    await _render_static(callback, db_user, 'CUSTOM_TIKTOK_SCREEN', CUSTOM_TIKTOK_SCREEN_DEFAULT, _tiktok_keyboard)


async def show_tiktok_rules(callback: types.CallbackQuery, db_user: User):
    await _render_static(callback, db_user, 'CUSTOM_TIKTOK_RULES_SCREEN', CUSTOM_TIKTOK_RULES_SCREEN_DEFAULT, _tiktok_rules_keyboard)


async def show_create_vpn(callback: types.CallbackQuery, db_user: User):
    await _render_static(callback, db_user, 'CUSTOM_CREATE_VPN_SCREEN', CUSTOM_CREATE_VPN_SCREEN_DEFAULT, _create_vpn_keyboard)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_ref_stories, F.data == 'kmock_ref_stories')
    dp.callback_query.register(show_ref_post, F.data == 'kmock_ref_post')
    dp.callback_query.register(show_tiktok, F.data == 'kmock_tiktok')
    dp.callback_query.register(show_tiktok_rules, F.data == 'kmock_tiktok_rules')
    dp.callback_query.register(show_create_vpn, F.data == 'kmock_create_vpn')
    dp.callback_query.register(show_demo_alert, F.data.startswith('kmock_alert:'))
