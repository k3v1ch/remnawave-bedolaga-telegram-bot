"""CUSTOM-UI: реальная заявка на партнёрство («Платим за TikTok»).

Входные экраны (SCR-TIKTOK / SCR-TIKTOK-RULES) живут в :mod:`custom_mock` и
воспроизводят макет ВЕРНО VPN. Кнопка «📝 Подать заявку» ведёт сюда — это уже не
заглушка, а 6-шаговая анкета, которая создаёт настоящую заявку через
:data:`partner_application_service` (та же партнёрка, что и в кабинете) и шлёт
уведомление админам. Поля и валидация 1:1 с кабинетной схемой
``PartnerApplicationRequest``:

    company_name              ≤ 255
    telegram_channel          ≤ 255
    website_url               ≤ 500
    description               ≤ 2000
    expected_monthly_referrals  0 … 2_000_000_000
    desired_commission_percent  1 … 100

Все поля, кроме площадки, необязательные — их можно пропустить, отправив «-».
"""

from __future__ import annotations

import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PartnerStatus, User
from app.services.admin_notification_service import AdminNotificationService
from app.services.partner_application_service import partner_application_service
from app.states import PartnerApplicationStates
from app.utils.decorators import error_handler
from app.utils.photo_message import edit_or_answer_photo


logger = structlog.get_logger(__name__)

# Лимиты — копия кабинетной схемы (app/cabinet/schemas/partners.py).
_MAX_COMPANY = 255
_MAX_CHANNEL = 255
_MAX_WEBSITE = 500
_MAX_DESCRIPTION = 2000
_MAX_REFERRALS = 2_000_000_000
_DEFAULT_PERCENT = 15  # озвучен на экране SCR-TIKTOK (15% от оплат клиентов)

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


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='‹ Назад', callback_data='menu_referrals')]]
    )


# ── вход ──────────────────────────────────────────────────────────────────────


@error_handler
async def show_partner_info(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, clone_bot=None
):
    """Экран «Стать партнёром» (как в кабинете) — статус-зависимый, доступен везде."""
    status = db_user.partner_status

    if status == PartnerStatus.APPROVED.value:
        pct = db_user.referral_commission_percent or _DEFAULT_PERCENT
        text = (
            '🤝 <b>Партнёрство</b>\n\n'
            '✅ Вы партнёр.\n'
            f'Ваша комиссия: <b>{pct}%</b>\n\n'
            'Запросить вывод заработка можно в разделе «ЗАРАБОТАТЬ».'
        )
        rows = [[InlineKeyboardButton(text='‹ Назад', callback_data='menu_referrals')]]
    elif status == PartnerStatus.PENDING.value:
        text = (
            '⏳ <b>Заявка на рассмотрении</b>\n\n'
            'Ваша заявка на партнёрство рассматривается. Мы уведомим вас, когда будет принято решение.'
        )
        rows = [[InlineKeyboardButton(text='‹ Назад', callback_data='menu_referrals')]]
    elif status == PartnerStatus.REJECTED.value:
        text = (
            '🤝 <b>Партнёрство</b>\n\n'
            'Предыдущая заявка отклонена. Вы можете подать её заново.'
        )
        rows = [
            [InlineKeyboardButton(text='📝 Подать заново', callback_data='partner_apply', style='primary')],
            [InlineKeyboardButton(text='‹ Назад', callback_data='menu_referrals')],
        ]
    else:
        text = (
            '🤝 <b>Стать партнёром</b>\n\n'
            'Подайте заявку на партнёрскую программу, чтобы получить повышенную комиссию '
            'и возможность вывода заработка.'
        )
        rows = [
            [InlineKeyboardButton(text='📝 Подать заявку', callback_data='partner_apply', style='primary')],
            [InlineKeyboardButton(text='‹ Назад', callback_data='menu_referrals')],
        ]

    await edit_or_answer_photo(
        callback=callback,
        caption=text,
        keyboard=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode='HTML',
    )
    await callback.answer()


@error_handler
async def start_application(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    """«📝 Подать заявку» → старт анкеты (с защитой от повторной/одобренной заявки).

    Партнёрка бренд-нейтральна (как в кабинете), поэтому доступна и в клонах."""
    if db_user.partner_status == PartnerStatus.APPROVED.value:
        await callback.answer('Вы уже являетесь партнёром ✅', show_alert=True)
        return
    if db_user.partner_status == PartnerStatus.PENDING.value:
        await callback.answer('Ваша заявка уже на рассмотрении ⏳', show_alert=True)
        return

    await state.set_state(PartnerApplicationStates.waiting_for_company_name)
    await state.update_data(partner_app={})
    await callback.message.answer(
        '📝 <b>Заявка на участие — шаг 1 из 6</b>\n\n'
        'Как к вам обращаться? Укажите ваше имя, ник или название проекта.\n\n'
        'Если не хотите указывать — отправьте «-».\n'
        'Отмена — /cancel'
    )
    await callback.answer()


# ── шаги анкеты ───────────────────────────────────────────────────────────────


@error_handler
async def step_company_name(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None):
    if await _maybe_cancel(message, state):
        return
    text = (message.text or '').strip()
    value = None if _is_skip(text) else text
    if value is not None and len(value) > _MAX_COMPANY:
        await message.answer(f'❌ Слишком длинно (до {_MAX_COMPANY} символов). Попробуйте короче.')
        return
    data = await state.get_data()
    app = data.get('partner_app', {})
    app['company_name'] = value
    await state.update_data(partner_app=app)
    await state.set_state(PartnerApplicationStates.waiting_for_channel)
    await message.answer(
        '📢 <b>Шаг 2 из 6</b>\n\n'
        'Дайте ссылку на вашу основную площадку — TikTok, Instagram, YouTube или Telegram-канал, '
        'где вы будете продвигать VPN.\n\n'
        'Отмена — /cancel'
    )


@error_handler
async def step_channel(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None):
    if await _maybe_cancel(message, state):
        return
    value = (message.text or '').strip()
    if _is_skip(value) or not value:
        await message.answer('❗️ Площадку нужно указать — это главное для заявки. Пришлите ссылку.')
        return
    if len(value) > _MAX_CHANNEL:
        await message.answer(f'❌ Слишком длинно (до {_MAX_CHANNEL} символов). Пришлите ссылку короче.')
        return
    data = await state.get_data()
    app = data.get('partner_app', {})
    app['telegram_channel'] = value
    await state.update_data(partner_app=app)
    await state.set_state(PartnerApplicationStates.waiting_for_website)
    await message.answer(
        '🌐 <b>Шаг 3 из 6</b>\n\n'
        'Есть ещё ссылки — сайт, второй канал, портфолио? Пришлите их.\n\n'
        'Если нет — отправьте «-». Отмена — /cancel'
    )


@error_handler
async def step_website(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None):
    if await _maybe_cancel(message, state):
        return
    text = (message.text or '').strip()
    value = None if _is_skip(text) else text
    if value is not None and len(value) > _MAX_WEBSITE:
        await message.answer(f'❌ Слишком длинно (до {_MAX_WEBSITE} символов).')
        return
    data = await state.get_data()
    app = data.get('partner_app', {})
    app['website_url'] = value
    await state.update_data(partner_app=app)
    await state.set_state(PartnerApplicationStates.waiting_for_expected_referrals)
    await message.answer(
        '👥 <b>Шаг 4 из 6</b>\n\n'
        'Сколько клиентов в месяц вы рассчитываете приводить? Укажите число.\n\n'
        'Если не знаете — отправьте «-». Отмена — /cancel'
    )


@error_handler
async def step_expected_referrals(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    if await _maybe_cancel(message, state):
        return
    text = (message.text or '').strip()
    value: int | None = None
    if not _is_skip(text):
        cleaned = text.replace(' ', '').replace('_', '')
        if not cleaned.isdigit():
            await message.answer('❌ Нужно целое число (например, 500). Или отправьте «-», чтобы пропустить.')
            return
        value = int(cleaned)
        if not 0 <= value <= _MAX_REFERRALS:
            await message.answer(f'❌ Число должно быть от 0 до {_MAX_REFERRALS:,}.'.replace(',', ' '))
            return
    data = await state.get_data()
    app = data.get('partner_app', {})
    app['expected_monthly_referrals'] = value
    await state.update_data(partner_app=app)
    await state.set_state(PartnerApplicationStates.waiting_for_desired_percent)
    await message.answer(
        '💰 <b>Шаг 5 из 6</b>\n\n'
        f'Какой процент комиссии вы бы хотели? Укажите число от 1 до 100 '
        f'(по умолчанию — {_DEFAULT_PERCENT}%).\n\n'
        'Чтобы оставить стандартный — отправьте «-». Отмена — /cancel'
    )


@error_handler
async def step_desired_percent(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    if await _maybe_cancel(message, state):
        return
    text = (message.text or '').strip()
    value: int | None = None
    if not _is_skip(text):
        cleaned = text.replace('%', '').replace(' ', '')
        if not cleaned.isdigit():
            await message.answer('❌ Нужно целое число от 1 до 100. Или «-», чтобы оставить стандартный процент.')
            return
        value = int(cleaned)
        if not 1 <= value <= 100:
            await message.answer('❌ Процент должен быть от 1 до 100.')
            return
    data = await state.get_data()
    app = data.get('partner_app', {})
    app['desired_commission_percent'] = value
    await state.update_data(partner_app=app)
    await state.set_state(PartnerApplicationStates.waiting_for_description)
    await message.answer(
        '✍️ <b>Шаг 6 из 6</b>\n\n'
        'Пара слов о вас: какой контент снимаете, какая аудитория, опыт продвижения. '
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
    app = data.get('partner_app', {})
    app['description'] = value
    await state.update_data(partner_app=app)
    await state.set_state(PartnerApplicationStates.confirming)
    await message.answer(_summary_text(app), reply_markup=_confirm_kb())


# ── подтверждение / отправка ──────────────────────────────────────────────────


def _row(label: str, value) -> str:
    shown = html.escape(str(value)) if value not in (None, '') else '—'
    return f'{label}: <b>{shown}</b>'


def _summary_text(app: dict) -> str:
    pct = app.get('desired_commission_percent')
    return (
        '📋 <b>Проверьте заявку</b>\n\n'
        f'{_row("👤 Имя/проект", app.get("company_name"))}\n'
        f'{_row("📢 Площадка", app.get("telegram_channel"))}\n'
        f'{_row("🌐 Доп. ссылки", app.get("website_url"))}\n'
        f'{_row("👥 Клиентов в месяц", app.get("expected_monthly_referrals"))}\n'
        f'{_row("💰 Желаемая комиссия", f"{pct}%" if pct else f"{_DEFAULT_PERCENT}% (стандарт)")}\n'
        f'{_row("✍️ О себе", app.get("description"))}\n\n'
        'Отправляем?'
    )


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='✅ Отправить заявку', callback_data='partner_apply_confirm', style='success')],
            [InlineKeyboardButton(text='❌ Отмена', callback_data='partner_apply_cancel')],
        ]
    )


@error_handler
async def confirm_application(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    data = await state.get_data()
    app = data.get('partner_app') or {}
    await state.clear()

    application, error = await partner_application_service.submit_application(
        db,
        user_id=db_user.id,
        company_name=app.get('company_name'),
        website_url=app.get('website_url'),
        telegram_channel=app.get('telegram_channel'),
        description=app.get('description'),
        expected_monthly_referrals=app.get('expected_monthly_referrals'),
        desired_commission_percent=app.get('desired_commission_percent'),
    )

    if not application:
        await callback.message.edit_text(f'⚠️ {error or "Не удалось отправить заявку."}', reply_markup=_back_kb())
        await callback.answer()
        return

    try:
        await AdminNotificationService(callback.bot).send_partner_application_notification(
            user=db_user, application_data=app
        )
    except Exception as e:  # уведомление не критично для пользователя
        logger.error('Ошибка уведомления админов о заявке на партнёрку', error=e)

    await callback.message.edit_text(
        '✅ <b>Заявка отправлена!</b>\n\n'
        'Мы рассмотрим её и сообщим о решении. После одобрения вам станет доступен '
        'повышенный процент и вывод средств в разделе «ЗАРАБОТАТЬ».',
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
    dp.callback_query.register(show_partner_info, F.data == 'partner_info')
    dp.callback_query.register(start_application, F.data == 'partner_apply')
    dp.callback_query.register(confirm_application, F.data == 'partner_apply_confirm')
    dp.callback_query.register(cancel_application, F.data == 'partner_apply_cancel')
    dp.message.register(step_company_name, PartnerApplicationStates.waiting_for_company_name)
    dp.message.register(step_channel, PartnerApplicationStates.waiting_for_channel)
    dp.message.register(step_website, PartnerApplicationStates.waiting_for_website)
    dp.message.register(step_expected_referrals, PartnerApplicationStates.waiting_for_expected_referrals)
    dp.message.register(step_desired_percent, PartnerApplicationStates.waiting_for_desired_percent)
    dp.message.register(step_description, PartnerApplicationStates.waiting_for_description)
