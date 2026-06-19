"""CUSTOM-UI: заявка в TikTok-программу (отдельный трек от партнёрки).

Экраны SCR-TIKTOK / SCR-TIKTOK-RULES живут в :mod:`custom_mock`. Кнопка действия
на них зависит от статуса автора (:attr:`User.tiktok_status`):

    none / rejected → «📝 Подать заявку» → анкета здесь (tiktok_apply)
    pending         → «⏳ Заявка на рассмотрении» (инфо-алерт)
    approved        → «📨 Отправить результаты» → URL на профиль поддержки

TikTok-программа НЕ выдаёт реф-код, комиссию и вывод — после одобрения автор
просто шлёт результаты в поддержку, а заработок проставляется вручную админом.
Поля и валидация анкеты 1:1 с кабинетной схемой ``TikTokApplicationRequest``.
"""

from __future__ import annotations

import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import TikTokApplicationStatus, User
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService
from app.services.tiktok_application_service import tiktok_application_service
from app.states import TikTokApplicationStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)

# Лимиты — копия кабинетной схемы (app/cabinet/schemas/tiktok.py).
_MAX_NAME = 255
_MAX_URL = 500
_MAX_PLATFORMS = 500
_MAX_TOPIC = 255
_MAX_DESCRIPTION = 2000
_MAX_AUDIENCE = 2_000_000_000

_SKIP_TOKENS = {'-', '—', 'skip', '/skip', 'пропустить', 'пропуск', 'нет'}
_CANCEL_TOKENS = {'/cancel', 'отмена'}


def _is_skip(text: str) -> bool:
    return text.strip().lower() in _SKIP_TOKENS


def _is_cancel(text: str) -> bool:
    return text.strip().lower() in _CANCEL_TOKENS


async def _maybe_cancel(message: types.Message, state: FSMContext) -> bool:
    if _is_cancel(message.text or ''):
        await state.clear()
        await message.answer('❌ Подача заявки отменена.')
        return True
    return False


def tiktok_support_url() -> str:
    """URL профиля поддержки для отправки результатов TikTok-авторами."""
    username = (settings.TIKTOK_SUPPORT_USERNAME or '@VernoVPNsupport').lstrip('@')
    return f'https://t.me/{username}'


def build_tiktok_cta(db_user: User, texts) -> InlineKeyboardButton:
    """Кнопка-действие на TikTok-экранах, зависящая от статуса автора."""
    status = db_user.tiktok_status

    if status == TikTokApplicationStatus.APPROVED.value:
        return InlineKeyboardButton(
            text=texts.t('CUSTOM_TIKTOK_SEND_RESULTS_BUTTON', '📨 Отправить результаты'),
            url=tiktok_support_url(),
        )
    if status == TikTokApplicationStatus.PENDING.value:
        return InlineKeyboardButton(
            text=texts.t('CUSTOM_TIKTOK_PENDING_BUTTON', '⏳ Заявка на рассмотрении'),
            callback_data='tiktok_pending',
        )
    return InlineKeyboardButton(
        text=texts.t('CUSTOM_TIKTOK_APPLY_BUTTON', '📝 Подать заявку'),
        callback_data='tiktok_apply',
        style='primary',
    )


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='‹ Назад', callback_data='kmock_tiktok')]]
    )


# ── вход ────────────────────────────────────────────────────────────────────


@error_handler
async def show_pending(callback: types.CallbackQuery, db_user: User, clone_bot=None):
    await callback.answer(
        'Ваша заявка уже на рассмотрении ⏳ Мы сообщим о решении.', show_alert=True
    )


@error_handler
async def start_application(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    """«📝 Подать заявку» → старт анкеты TikTok-программы."""
    if db_user.tiktok_status == TikTokApplicationStatus.APPROVED.value:
        await callback.answer('Вы уже в TikTok-программе ✅', show_alert=True)
        return
    if db_user.tiktok_status == TikTokApplicationStatus.PENDING.value:
        await callback.answer('Ваша заявка уже на рассмотрении ⏳', show_alert=True)
        return

    await state.set_state(TikTokApplicationStates.waiting_for_display_name)
    await state.update_data(tiktok_app={})
    await callback.message.answer(
        '📝 <b>Заявка в TikTok-программу — шаг 1 из 6</b>\n\n'
        'Как к вам обращаться? Укажите имя или ник.\n\n'
        'Если не хотите указывать — отправьте «-».\n'
        'Отмена — /cancel'
    )
    await callback.answer()


# ── шаги анкеты ───────────────────────────────────────────────────────────────


@error_handler
async def step_display_name(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None):
    if await _maybe_cancel(message, state):
        return
    text = (message.text or '').strip()
    value = None if _is_skip(text) else text
    if value is not None and len(value) > _MAX_NAME:
        await message.answer(f'❌ Слишком длинно (до {_MAX_NAME} символов). Попробуйте короче.')
        return
    data = await state.get_data()
    app = data.get('tiktok_app', {})
    app['display_name'] = value
    await state.update_data(tiktok_app=app)
    await state.set_state(TikTokApplicationStates.waiting_for_tiktok_url)
    await message.answer(
        '🎬 <b>Шаг 2 из 6</b>\n\n'
        'Пришлите ссылку на ваш TikTok-профиль (или основную площадку коротких видео).\n\n'
        'Отмена — /cancel'
    )


@error_handler
async def step_tiktok_url(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None):
    if await _maybe_cancel(message, state):
        return
    value = (message.text or '').strip()
    if _is_skip(value) or not value:
        await message.answer('❗️ Ссылку на профиль нужно указать — это главное для заявки.')
        return
    if len(value) > _MAX_URL:
        await message.answer(f'❌ Слишком длинно (до {_MAX_URL} символов). Пришлите ссылку короче.')
        return
    data = await state.get_data()
    app = data.get('tiktok_app', {})
    app['tiktok_url'] = value
    await state.update_data(tiktok_app=app)
    await state.set_state(TikTokApplicationStates.waiting_for_other_platforms)
    await message.answer(
        '🔗 <b>Шаг 3 из 6</b>\n\n'
        'Есть другие площадки — Instagram Reels, YouTube Shorts, второй аккаунт? Пришлите ссылки.\n\n'
        'Если нет — отправьте «-». Отмена — /cancel'
    )


@error_handler
async def step_other_platforms(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    if await _maybe_cancel(message, state):
        return
    text = (message.text or '').strip()
    value = None if _is_skip(text) else text
    if value is not None and len(value) > _MAX_PLATFORMS:
        await message.answer(f'❌ Слишком длинно (до {_MAX_PLATFORMS} символов).')
        return
    data = await state.get_data()
    app = data.get('tiktok_app', {})
    app['other_platforms'] = value
    await state.update_data(tiktok_app=app)
    await state.set_state(TikTokApplicationStates.waiting_for_audience_size)
    await message.answer(
        '👥 <b>Шаг 4 из 6</b>\n\n'
        'Какая у вас аудитория? Укажите примерное число подписчиков.\n\n'
        'Если не знаете — отправьте «-». Отмена — /cancel'
    )


@error_handler
async def step_audience_size(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    if await _maybe_cancel(message, state):
        return
    text = (message.text or '').strip()
    value: int | None = None
    if not _is_skip(text):
        cleaned = text.replace(' ', '').replace('_', '')
        if not cleaned.isdigit():
            await message.answer('❌ Нужно целое число (например, 50000). Или отправьте «-», чтобы пропустить.')
            return
        value = int(cleaned)
        if not 0 <= value <= _MAX_AUDIENCE:
            await message.answer(f'❌ Число должно быть от 0 до {_MAX_AUDIENCE:,}.'.replace(',', ' '))
            return
    data = await state.get_data()
    app = data.get('tiktok_app', {})
    app['audience_size'] = value
    await state.update_data(tiktok_app=app)
    await state.set_state(TikTokApplicationStates.waiting_for_content_topic)
    await message.answer(
        '🎯 <b>Шаг 5 из 6</b>\n\n'
        'Какая тематика вашего контента? (например: технологии, лайфхаки, развлечения)\n\n'
        'Если разная — отправьте «-». Отмена — /cancel'
    )


@error_handler
async def step_content_topic(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    if await _maybe_cancel(message, state):
        return
    text = (message.text or '').strip()
    value = None if _is_skip(text) else text
    if value is not None and len(value) > _MAX_TOPIC:
        await message.answer(f'❌ Слишком длинно (до {_MAX_TOPIC} символов).')
        return
    data = await state.get_data()
    app = data.get('tiktok_app', {})
    app['content_topic'] = value
    await state.update_data(tiktok_app=app)
    await state.set_state(TikTokApplicationStates.waiting_for_description)
    await message.answer(
        '✍️ <b>Шаг 6 из 6</b>\n\n'
        'Пара слов о вас: какой контент снимаете, опыт, средние просмотры. '
        'Это поможет нам быстрее одобрить заявку.\n\n'
        'Если нечего добавить — отправьте «-». Отмена — /cancel'
    )


@error_handler
async def step_description(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None):
    if await _maybe_cancel(message, state):
        return
    text = (message.text or '').strip()
    value = None if _is_skip(text) else text
    if value is not None and len(value) > _MAX_DESCRIPTION:
        await message.answer(f'❌ Слишком длинно (до {_MAX_DESCRIPTION} символов). Сократите, пожалуйста.')
        return
    data = await state.get_data()
    app = data.get('tiktok_app', {})
    app['description'] = value
    await state.update_data(tiktok_app=app)
    await state.set_state(TikTokApplicationStates.confirming)
    await message.answer(_summary_text(app), reply_markup=_confirm_kb())


# ── подтверждение / отправка ──────────────────────────────────────────────────


def _row(label: str, value) -> str:
    shown = html.escape(str(value)) if value not in (None, '') else '—'
    return f'{label}: <b>{shown}</b>'


def _summary_text(app: dict) -> str:
    return (
        '📋 <b>Проверьте заявку</b>\n\n'
        f'{_row("👤 Имя/ник", app.get("display_name"))}\n'
        f'{_row("🎬 TikTok", app.get("tiktok_url"))}\n'
        f'{_row("🔗 Доп. площадки", app.get("other_platforms"))}\n'
        f'{_row("👥 Аудитория", app.get("audience_size"))}\n'
        f'{_row("🎯 Тематика", app.get("content_topic"))}\n'
        f'{_row("✍️ О себе", app.get("description"))}\n\n'
        'Отправляем?'
    )


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='✅ Отправить заявку', callback_data='tiktok_apply_confirm', style='success')],
            [InlineKeyboardButton(text='❌ Отмена', callback_data='tiktok_apply_cancel')],
        ]
    )


@error_handler
async def confirm_application(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    data = await state.get_data()
    app = data.get('tiktok_app') or {}
    await state.clear()

    application, error = await tiktok_application_service.submit_application(
        db,
        user_id=db_user.id,
        display_name=app.get('display_name'),
        tiktok_url=app.get('tiktok_url'),
        other_platforms=app.get('other_platforms'),
        audience_size=app.get('audience_size'),
        content_topic=app.get('content_topic'),
        description=app.get('description'),
    )

    if not application:
        await callback.message.edit_text(f'⚠️ {error or "Не удалось отправить заявку."}', reply_markup=_back_kb())
        await callback.answer()
        return

    try:
        await AdminNotificationService(callback.bot).send_tiktok_application_notification(
            user=db_user, application_data=app
        )
    except Exception as e:  # уведомление не критично для пользователя
        logger.error('Ошибка уведомления админов о заявке в TikTok-программу', error=e)

    await callback.message.edit_text(
        '✅ <b>Заявка отправлена!</b>\n\n'
        'Мы рассмотрим её и сообщим о решении. После одобрения вы сможете отправлять '
        'результаты роликов в поддержку прямо с экрана TikTok-программы.',
        reply_markup=_back_kb(),
    )
    await callback.answer('Заявка отправлена')


@error_handler
async def cancel_application(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    await state.clear()
    await callback.message.edit_text('❌ Подача заявки отменена.', reply_markup=_back_kb())
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_pending, F.data == 'tiktok_pending')
    dp.callback_query.register(start_application, F.data == 'tiktok_apply')
    dp.callback_query.register(confirm_application, F.data == 'tiktok_apply_confirm')
    dp.callback_query.register(cancel_application, F.data == 'tiktok_apply_cancel')
    dp.message.register(step_display_name, TikTokApplicationStates.waiting_for_display_name)
    dp.message.register(step_tiktok_url, TikTokApplicationStates.waiting_for_tiktok_url)
    dp.message.register(step_other_platforms, TikTokApplicationStates.waiting_for_other_platforms)
    dp.message.register(step_audience_size, TikTokApplicationStates.waiting_for_audience_size)
    dp.message.register(step_content_topic, TikTokApplicationStates.waiting_for_content_topic)
    dp.message.register(step_description, TikTokApplicationStates.waiting_for_description)
