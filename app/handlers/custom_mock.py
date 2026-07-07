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

from pathlib import Path

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.database.models import User
from app.handlers.custom_tiktok import build_tiktok_cta
from app.localization.texts import get_texts
from app.utils.clone_context import is_clone_context
from app.utils.photo_message import edit_or_answer_photo
from app.utils.premium_emoji import build_caption_entities, combine_entities


logger = structlog.get_logger(__name__)

# Корень проекта (в контейнере — /app). Картинки-баннеры лежат в корне репозитория.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Файлы, которые бот отдаёт по кнопке «Скачать изображение» на экранах сторис/пост.
_BONUS_IMAGES = {'stories': 'storis.png', 'post': 'post.png'}

# Профиль поддержки для бонус-акций (захардкожен по требованию владельца).
_BONUS_SUPPORT_URL = 'https://t.me/VernoVPNsupport'


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
    '🔗 Подпись для публикации (реф-ссылка уже внутри): {bot_ref_link}'
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
    '🔗 Подпись для публикации (реф-ссылка уже внутри): {bot_ref_link}'
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


def _bonus_screen_keyboard(texts, image_kind: str) -> InlineKeyboardMarkup:
    """Клавиатура SCR-REF-STORIES / SCR-REF-POST.

    «Получить 7 дней» ведёт прямо в поддержку (URL), «Скачать изображение» отдаёт
    нужный баннер (storis.png / post.png). Кнопка «Написать в поддержку» убрана.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('CUSTOM_REF_GET7_BUTTON', 'Получить 7 дней'),
                    url=_BONUS_SUPPORT_URL,
                    style='success',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('CUSTOM_REF_DOWNLOAD_IMAGE_BUTTON', 'Скачать изображение'),
                    callback_data=f'kmock_img:{image_kind}',
                    style='success',
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


async def _render_bonus_screen(
    callback: types.CallbackQuery, db_user: User, key: str, default_text: str, image_kind: str
):
    # Бренд-акции (ВЕРНО VPN) в клонах не показываем (white-label).
    if is_clone_context():
        await callback.answer()
        return
    texts = get_texts(db_user.language)
    raw = texts.t(key, default_text)

    # Внизу экрана — готовая подпись «Пользуюсь ВЕРНО VPN» со встроенной реф-ссылкой
    # юзера (text_link), а не голая ссылка: её копируют прямо в сторис/пост.
    if db_user.referral_code:
        from app.config import settings as _settings

        bot_username = (await callback.bot.get_me()).username
        ref_link = _settings.get_bot_referral_link(db_user.referral_code, bot_username)
        link_caption = f'«<a href="{ref_link}">Пользуюсь ВЕРНО VPN</a>»'
    else:
        link_caption = CUSTOM_REF_LINK_HINT
    text = raw.replace('{bot_ref_link}', link_caption)

    # HTML (text_link) + премиум-эмодзи вместе: Telegram игнорирует parse_mode при
    # entities, поэтому HTML парсим в entities сами (combine_entities). Если премиум
    # выключен/совпадений нет — шлём как обычный HTML.
    combined = combine_entities(text, 'HTML')
    caption, entities = combined if combined else (text, None)
    try:
        await edit_or_answer_photo(
            callback=callback,
            caption=caption,
            keyboard=_bonus_screen_keyboard(texts, image_kind),
            parse_mode='HTML',
            caption_entities=entities,
        )
        await callback.answer()
    except Exception as error:
        logger.debug('CUSTOM-UI: ошибка рендера mock-экрана', key=key, error=error)
        await callback.answer()


async def show_ref_stories(callback: types.CallbackQuery, db_user: User):
    await _render_bonus_screen(
        callback, db_user, 'CUSTOM_REF_STORIES_SCREEN', CUSTOM_REF_STORIES_SCREEN_DEFAULT, 'stories'
    )


async def show_ref_post(callback: types.CallbackQuery, db_user: User):
    await _render_bonus_screen(
        callback, db_user, 'CUSTOM_REF_POST_SCREEN', CUSTOM_REF_POST_SCREEN_DEFAULT, 'post'
    )


async def send_bonus_image(callback: types.CallbackQuery, db_user: User):
    """«Скачать изображение» → отправляет нужный баннер документом (без сжатия)."""
    if is_clone_context():
        await callback.answer()
        return
    data = callback.data or ''
    kind = data.split(':', 1)[1] if ':' in data else ''
    filename = _BONUS_IMAGES.get(kind)
    if not filename:
        await callback.answer()
        return
    path = _PROJECT_ROOT / filename
    if not path.exists():
        logger.warning('CUSTOM-UI: баннер не найден', path=str(path))
        await callback.answer('Изображение временно недоступно', show_alert=True)
        return
    try:
        await callback.message.answer_document(FSInputFile(str(path)))
        await callback.answer()
    except Exception as error:
        logger.debug('CUSTOM-UI: ошибка отправки баннера', kind=kind, error=error)
        await callback.answer('Не удалось отправить изображение', show_alert=True)


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
    '├ После одобрения заявки результаты роликов присылайте в поддержку\n'
    '└ Сетка может пересматриваться раз в полгода'
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


def _tiktok_keyboard(texts, db_user: User) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('CUSTOM_TIKTOK_RULES_BUTTON', '📋 Условия и правила'), callback_data='kmock_tiktok_rules')],
            [build_tiktok_cta(db_user, texts)],
            [InlineKeyboardButton(text=texts.t('CUSTOM_BACK_BUTTON', '‹ Назад'), callback_data='menu_referrals')],
        ]
    )


def _tiktok_rules_keyboard(texts, db_user: User) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [build_tiktok_cta(db_user, texts)],
            [InlineKeyboardButton(text=texts.t('CUSTOM_BACK_BUTTON', '‹ Назад'), callback_data='kmock_tiktok')],
        ]
    )


def _create_vpn_keyboard(texts, owned: int = 0, max_bots: int = 0) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # «Создать бота» исчезает, когда достигнут лимит по ботам.
    if owned < max_bots:
        rows.append(
            [InlineKeyboardButton(text=texts.t('CUSTOM_CREATE_VPN_BUTTON', '➕ Создать своего VPN-бота'), callback_data='myb:create', style='primary')]
        )
    # «Мои боты» — отдельной кнопкой, если уже есть хотя бы один бот.
    if owned > 0:
        rows.append(
            [InlineKeyboardButton(text=texts.t('CUSTOM_MY_BOTS_BUTTON', '🤖 Мои боты'), callback_data='myb:list:0')]
        )
    rows.append([InlineKeyboardButton(text=texts.t('CUSTOM_BACK_BUTTON', '‹ Назад'), callback_data='menu_referrals')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_static(callback: types.CallbackQuery, db_user: User, key: str, default_text: str, keyboard_fn):
    # SCR-TIKTOK / правила — наш бренд; в клонах скрыто (white-label).
    if is_clone_context():
        await callback.answer()
        return
    texts = get_texts(db_user.language)
    caption = texts.t(key, default_text)
    try:
        await edit_or_answer_photo(
            callback=callback,
            caption=caption,
            keyboard=keyboard_fn(texts, db_user),
            parse_mode='HTML',
            caption_entities=build_caption_entities(caption),
        )
        await callback.answer()
    except Exception as error:
        logger.debug('CUSTOM-UI: ошибка рендера mock-экрана', key=key, error=error)
        await callback.answer()


async def show_tiktok(callback: types.CallbackQuery, db_user: User):
    await _render_static(callback, db_user, 'CUSTOM_TIKTOK_SCREEN', CUSTOM_TIKTOK_SCREEN_DEFAULT, _tiktok_keyboard)


async def show_tiktok_rules(callback: types.CallbackQuery, db_user: User):
    await _render_static(callback, db_user, 'CUSTOM_TIKTOK_RULES_SCREEN', CUSTOM_TIKTOK_RULES_SCREEN_DEFAULT, _tiktok_rules_keyboard)


async def show_create_vpn(callback: types.CallbackQuery, db_user: User, db):
    from app.config import settings
    from app.database.crud.clone_bot import count_for_owner

    owned = await count_for_owner(db, db_user.id)
    max_bots = settings.CLONE_MAX_PER_USER
    texts = get_texts(db_user.language)
    caption = texts.t('CUSTOM_CREATE_VPN_SCREEN', CUSTOM_CREATE_VPN_SCREEN_DEFAULT)
    try:
        await edit_or_answer_photo(
            callback=callback,
            caption=caption,
            keyboard=_create_vpn_keyboard(texts, owned, max_bots),
            parse_mode='HTML',
            caption_entities=build_caption_entities(caption),
        )
        await callback.answer()
    except Exception as error:
        logger.debug('CUSTOM-UI: ошибка рендера экрана create_vpn', error=error)
        await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_ref_stories, F.data == 'kmock_ref_stories')
    dp.callback_query.register(show_ref_post, F.data == 'kmock_ref_post')
    dp.callback_query.register(send_bonus_image, F.data.startswith('kmock_img:'))
    dp.callback_query.register(show_tiktok, F.data == 'kmock_tiktok')
    dp.callback_query.register(show_tiktok_rules, F.data == 'kmock_tiktok_rules')
    dp.callback_query.register(show_create_vpn, F.data == 'kmock_create_vpn')
    dp.callback_query.register(show_demo_alert, F.data.startswith('kmock_alert:'))
