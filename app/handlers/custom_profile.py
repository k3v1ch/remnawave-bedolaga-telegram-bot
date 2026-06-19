"""CUSTOM-UI: профиль В БОТЕ — привязка/смена email и пароля для входа на сайт.

РЕАЛЬНАЯ фича (не заглушка): переиспользует тот же бэкенд, что и веб-кабинет,
поэтому привязанные email+пароль работают и для входа на сайт.

Переиспользуемые компоненты кабинета:
- ``app.cabinet.auth.password_utils`` — hash_password / verify_password (bcrypt);
- ``app.cabinet.auth.email_verification`` — generate_email_change_code (6 цифр) и сроки;
- ``app.cabinet.services.email_service`` — отправка кода на email (SMTP, sync);
- ``app.database.crud.user`` — set_email_change_pending / verify_and_apply_email_change /
  clear_email_change_pending / is_email_taken.

Флоу:
1. Привязка (только TG → email): email → пароль → код на email → ввод кода → сохранение.
   Любой сбой/отмена/неверный код/истечение → НИЧЕГО не сохраняется (полный сброс).
2. Смена email (2 кода): код на текущую почту → новый email → код на новый email.
3. Смена пароля: текущий пароль (проверка) → новый пароль.

С профиля убраны кнопки «Удалить аккаунт» и «Сбросить ключ» (по требованию).
Логирование на каждом шаге (события + ошибки); код/пароль на info НЕ логируются.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import re
from datetime import datetime, timezone

import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.auth.email_verification import generate_email_change_code, get_email_change_expires_at
from app.cabinet.auth.password_utils import hash_password, verify_password
from app.cabinet.services.email_service import email_service
from app.database.crud.user import (
    clear_email_change_pending,
    is_email_taken,
    set_email_change_pending,
    verify_and_apply_email_change,
)
from app.database.models import User
from app.localization.texts import get_texts
from app.utils.notification_prefs import get_user_notification_pref
from app.utils.photo_message import edit_or_answer_photo


logger = structlog.get_logger(__name__)

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128
MAX_CODE_ATTEMPTS = 3

# Тумблеры уведомлений (SCR-NOTIFICATIONS). Ключи/дефолты канонические
# (app.utils.notification_prefs), поэтому переключение РЕАЛЬНО гейтит отправку
# уведомлений Бедолаги, а не просто сохраняется. Маппинг на 4 пункта макета.
CUSTOM_NOTIF_TOGGLES = (
    ('subscription_expiry_enabled', '📅 Истечение подписки'),
    ('balance_low_enabled', '💸 Низкий баланс'),
    ('promo_offers_enabled', '🎁 Бонусы и акции'),
    ('news_enabled', '🤖 Новости'),
)
CUSTOM_NOTIF_KEYS = frozenset(k for k, _ in CUSTOM_NOTIF_TOGGLES)
CUSTOM_NOTIF_SCREEN_DEFAULT = (
    '🔔 Настройки уведомлений\n'
    '\n'
    'Нажмите на пункт, чтобы включить или выключить:'
)


# ─────────────────────────────────────────────────────────────
# FSM-состояния
# ─────────────────────────────────────────────────────────────

class BindEmailStates(StatesGroup):
    waiting_email = State()
    waiting_password = State()
    waiting_code = State()


class ChangeEmailStates(StatesGroup):
    waiting_current_code = State()
    waiting_new_email = State()
    waiting_new_code = State()


class ChangePasswordStates(StatesGroup):
    waiting_old_password = State()
    waiting_new_password = State()


# ─────────────────────────────────────────────────────────────
# Вспомогательные
# ─────────────────────────────────────────────────────────────

def _mask_email(email: str | None) -> str:
    try:
        name, dom = (email or '').split('@', 1)
        return (name[:2] + '***@' + dom) if len(name) > 2 else ('***@' + dom)
    except Exception:
        return '***'


def _expired(iso: str | None) -> bool:
    try:
        return datetime.now(timezone.utc) >= datetime.fromisoformat(iso)
    except Exception:
        return True


async def _safe_delete(message: types.Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def _send_code(email: str, code: str, username: str | None) -> bool:
    """Отправка 6-значного кода на email (sync-сервис кабинета через executor)."""
    try:
        return await asyncio.to_thread(email_service.send_email_change_code, email, code, username, 'ru')
    except Exception as error:
        logger.error('custom_profile: ошибка отправки кода', email=_mask_email(email), error=str(error))
        return False


def _cancel_keyboard(texts) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.t('CUSTOM_PROFILE_CANCEL_BUTTON', '✖️ Отмена'), callback_data='kprofile_cancel')]]
    )


def _back_to_profile_keyboard(texts) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.t('CUSTOM_PROFILE_BACK_BUTTON', '‹ К профилю'), callback_data='custom_profile')]]
    )


def _profile_keyboard(user: User, texts) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if user.email and user.password_hash:
        rows.append([InlineKeyboardButton(text=texts.t('CUSTOM_PROFILE_CHANGE_EMAIL_BUTTON', 'Сменить email'), callback_data='kprofile_change_email')])
        rows.append([InlineKeyboardButton(text=texts.t('CUSTOM_PROFILE_CHANGE_PASSWORD_BUTTON', 'Сменить пароль'), callback_data='kprofile_change_password')])
    else:
        rows.append([InlineKeyboardButton(text=texts.t('CUSTOM_PROFILE_BIND_BUTTON', 'Добавить email и пароль'), callback_data='kprofile_bind', style='primary')])
    rows.append([InlineKeyboardButton(text=texts.t('CUSTOM_PROFILE_NOTIF_BUTTON', '🔔 Уведомления'), callback_data='kprofile_notifications')])
    rows.append([InlineKeyboardButton(text=texts.t('CUSTOM_BACK_BUTTON', '‹ Назад'), callback_data='menu_subscription')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _profile_text(user: User, texts) -> str:
    lines = ['👤 <b>Профиль</b>', '']
    lines.append('Telegram: подключён ✅')
    if user.email:
        status = 'подтверждён ✅' if user.email_verified else 'не подтверждён ⏳'
        lines.append(f'Email: <code>{html.escape(user.email)}</code> — {status}')
        lines.append(f'Пароль: {"установлен ✅" if user.password_hash else "не задан ❌"}')
        lines.append('')
        lines.append('Эти данные используются для входа в личный кабинет на сайте.')
    else:
        lines.append('Email для входа на сайт: не привязан ❌')
        lines.append('Пароль: не задан ❌')
        lines.append('')
        lines.append('Привяжите email и пароль, чтобы входить в личный кабинет на сайте.')
    return '\n'.join(lines)


async def _show_profile_screen(callback: types.CallbackQuery, user: User) -> None:
    texts = get_texts(user.language)
    await edit_or_answer_photo(
        callback=callback,
        caption=_profile_text(user, texts),
        keyboard=_profile_keyboard(user, texts),
        parse_mode='HTML',
    )


# ─────────────────────────────────────────────────────────────
# Экран профиля / отмена
# ─────────────────────────────────────────────────────────────

async def show_profile(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    await state.clear()
    await _show_profile_screen(callback, db_user)
    await callback.answer()


async def cancel_flow(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    try:
        await clear_email_change_pending(db, db_user)
    except Exception as error:
        logger.debug('custom_profile: ошибка очистки pending при отмене', error=str(error))
    await state.clear()
    logger.info('custom_profile: флоу отменён', user_id=db_user.id)
    await _show_profile_screen(callback, db_user)
    await callback.answer('Отменено. Ничего не сохранено.')


# ─────────────────────────────────────────────────────────────
# Привязка email+пароль
# ─────────────────────────────────────────────────────────────

async def bind_start(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    if db_user.email and db_user.password_hash:
        await callback.answer('Email уже привязан.', show_alert=True)
        return
    await state.set_state(BindEmailStates.waiting_email)
    logger.info('custom_profile: привязка начата', user_id=db_user.id)
    await callback.message.answer('✍️ Введите email для входа на сайт:', reply_markup=_cancel_keyboard(texts))
    await callback.answer()


async def bind_email(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    email = (message.text or '').strip().lower()
    await _safe_delete(message)
    if not EMAIL_RE.match(email):
        await message.answer('❌ Некорректный email. Введите ещё раз:', reply_markup=_cancel_keyboard(texts))
        return
    if await is_email_taken(db, email):
        logger.info('custom_profile: email занят', user_id=db_user.id, email=_mask_email(email))
        await message.answer('❌ Этот email уже используется. Введите другой:', reply_markup=_cancel_keyboard(texts))
        return
    await state.update_data(email=email)
    await state.set_state(BindEmailStates.waiting_password)
    logger.info('custom_profile: email принят', user_id=db_user.id, email=_mask_email(email))
    await message.answer(f'🔐 Придумайте пароль (минимум {MIN_PASSWORD_LEN} символов):', reply_markup=_cancel_keyboard(texts))


async def bind_password(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    password = message.text or ''
    await _safe_delete(message)
    if not (MIN_PASSWORD_LEN <= len(password) <= MAX_PASSWORD_LEN):
        await message.answer(f'❌ Пароль должен быть от {MIN_PASSWORD_LEN} до {MAX_PASSWORD_LEN} символов. Введите ещё раз:', reply_markup=_cancel_keyboard(texts))
        return
    data = await state.get_data()
    email = data.get('email')
    if not email:
        await state.clear()
        await message.answer('Сессия сброшена, начните заново.', reply_markup=_back_to_profile_keyboard(texts))
        return
    code = generate_email_change_code()
    expires = get_email_change_expires_at()
    if not await _send_code(email, code, db_user.full_name):
        logger.warning('custom_profile: не удалось отправить код привязки', user_id=db_user.id, email=_mask_email(email))
        await state.clear()
        await message.answer('❌ Не удалось отправить код на почту. Привязка отменена, данные не сохранены.', reply_markup=_back_to_profile_keyboard(texts))
        return
    await state.update_data(password_hash=hash_password(password), code=code, code_expires=expires.isoformat(), attempts=0)
    await state.set_state(BindEmailStates.waiting_code)
    logger.info('custom_profile: код привязки отправлен', user_id=db_user.id, email=_mask_email(email))
    logger.debug('custom_profile: код привязки (dev)', user_id=db_user.id, code=code)
    await message.answer(f'📨 Код подтверждения отправлен на <code>{html.escape(email)}</code>.\nВведите код из письма:', parse_mode='HTML', reply_markup=_cancel_keyboard(texts))


async def bind_code(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    entered = (message.text or '').strip()
    await _safe_delete(message)
    data = await state.get_data()
    code = data.get('code')
    email = data.get('email')
    pwd_hash = data.get('password_hash')
    if not (code and email and pwd_hash and data.get('code_expires')):
        await state.clear()
        await message.answer('Сессия сброшена, начните заново.', reply_markup=_back_to_profile_keyboard(texts))
        return
    if _expired(data.get('code_expires')):
        await state.clear()
        logger.info('custom_profile: код привязки истёк', user_id=db_user.id)
        await message.answer('⌛ Код истёк. Привязка отменена, данные не сохранены.', reply_markup=_back_to_profile_keyboard(texts))
        return
    if not hmac.compare_digest(entered, str(code)):
        attempts = int(data.get('attempts', 0)) + 1
        if attempts >= MAX_CODE_ATTEMPTS:
            await state.clear()
            logger.info('custom_profile: превышены попытки кода привязки', user_id=db_user.id)
            await message.answer('❌ Слишком много неверных попыток. Привязка отменена, данные не сохранены.', reply_markup=_back_to_profile_keyboard(texts))
            return
        await state.update_data(attempts=attempts)
        await message.answer(f'❌ Неверный код. Осталось попыток: {MAX_CODE_ATTEMPTS - attempts}. Введите код:', reply_markup=_cancel_keyboard(texts))
        return
    if await is_email_taken(db, email, exclude_user_id=db_user.id):
        await state.clear()
        await message.answer('❌ Этот email уже занят. Привязка отменена.', reply_markup=_back_to_profile_keyboard(texts))
        return
    try:
        db_user.email = email
        db_user.password_hash = pwd_hash
        db_user.email_verified = True
        db_user.email_verified_at = datetime.now(timezone.utc)
        db_user.email_verification_source = 'cabinet'
        await db.commit()
        await db.refresh(db_user)
    except Exception as error:
        await db.rollback()
        logger.error('custom_profile: ошибка сохранения привязки', user_id=db_user.id, error=str(error))
        await state.clear()
        await message.answer('❌ Ошибка сохранения. Данные не сохранены.', reply_markup=_back_to_profile_keyboard(texts))
        return
    await state.clear()
    logger.info('custom_profile: привязка завершена', user_id=db_user.id, email=_mask_email(email))
    await message.answer(f'✅ Email привязан: <code>{html.escape(email)}</code>\nТеперь вы можете входить в личный кабинет на сайте по email и паролю.', parse_mode='HTML', reply_markup=_back_to_profile_keyboard(texts))


# ─────────────────────────────────────────────────────────────
# Смена email (2 кода: текущая почта → новая почта)
# ─────────────────────────────────────────────────────────────

async def change_email_start(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    if not (db_user.email and db_user.password_hash):
        await callback.answer('Сначала привяжите email.', show_alert=True)
        return
    code = generate_email_change_code()
    expires = get_email_change_expires_at()
    if not await _send_code(db_user.email, code, db_user.full_name):
        logger.warning('custom_profile: не удалось отправить код на текущую почту', user_id=db_user.id)
        await callback.answer('Не удалось отправить код на текущую почту.', show_alert=True)
        return
    await state.set_state(ChangeEmailStates.waiting_current_code)
    await state.update_data(cur_code=code, cur_expires=expires.isoformat(), attempts=0)
    logger.info('custom_profile: смена email начата, код на текущую', user_id=db_user.id, email=_mask_email(db_user.email))
    logger.debug('custom_profile: код на текущую (dev)', user_id=db_user.id, code=code)
    await callback.message.answer(f'📨 Код отправлен на текущую почту <code>{html.escape(db_user.email)}</code>.\nВведите код для подтверждения:', parse_mode='HTML', reply_markup=_cancel_keyboard(texts))
    await callback.answer()


async def change_email_current_code(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    entered = (message.text or '').strip()
    await _safe_delete(message)
    data = await state.get_data()
    code = data.get('cur_code')
    if not (code and data.get('cur_expires')):
        await state.clear()
        await message.answer('Сессия сброшена, начните заново.', reply_markup=_back_to_profile_keyboard(texts))
        return
    if _expired(data.get('cur_expires')):
        await state.clear()
        await message.answer('⌛ Код истёк. Смена email отменена.', reply_markup=_back_to_profile_keyboard(texts))
        return
    if not hmac.compare_digest(entered, str(code)):
        attempts = int(data.get('attempts', 0)) + 1
        if attempts >= MAX_CODE_ATTEMPTS:
            await state.clear()
            await message.answer('❌ Слишком много неверных попыток. Смена email отменена.', reply_markup=_back_to_profile_keyboard(texts))
            return
        await state.update_data(attempts=attempts)
        await message.answer(f'❌ Неверный код. Осталось попыток: {MAX_CODE_ATTEMPTS - attempts}. Введите код:', reply_markup=_cancel_keyboard(texts))
        return
    await state.set_state(ChangeEmailStates.waiting_new_email)
    logger.info('custom_profile: текущая почта подтверждена', user_id=db_user.id)
    await message.answer('✍️ Введите новый email:', reply_markup=_cancel_keyboard(texts))


async def change_email_new(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    new_email = (message.text or '').strip().lower()
    await _safe_delete(message)
    if not EMAIL_RE.match(new_email):
        await message.answer('❌ Некорректный email. Введите ещё раз:', reply_markup=_cancel_keyboard(texts))
        return
    if db_user.email and new_email == db_user.email.lower():
        await message.answer('❌ Это ваш текущий email. Введите другой:', reply_markup=_cancel_keyboard(texts))
        return
    if await is_email_taken(db, new_email, exclude_user_id=db_user.id):
        await message.answer('❌ Этот email уже используется. Введите другой:', reply_markup=_cancel_keyboard(texts))
        return
    code = generate_email_change_code()
    expires = get_email_change_expires_at()
    if not await _send_code(new_email, code, db_user.full_name):
        await state.clear()
        await message.answer('❌ Не удалось отправить код на новый email. Смена отменена.', reply_markup=_back_to_profile_keyboard(texts))
        return
    try:
        await set_email_change_pending(db, db_user, new_email, code, expires)
    except Exception as error:
        logger.error('custom_profile: ошибка set_email_change_pending', user_id=db_user.id, error=str(error))
        await state.clear()
        await message.answer('❌ Ошибка. Смена email отменена.', reply_markup=_back_to_profile_keyboard(texts))
        return
    await state.set_state(ChangeEmailStates.waiting_new_code)
    await state.update_data(attempts=0)
    logger.info('custom_profile: код на новый email отправлен', user_id=db_user.id, email=_mask_email(new_email))
    logger.debug('custom_profile: код на новый (dev)', user_id=db_user.id, code=code)
    await message.answer(f'📨 Код отправлен на <code>{html.escape(new_email)}</code>.\nВведите код для завершения смены:', parse_mode='HTML', reply_markup=_cancel_keyboard(texts))


async def change_email_new_code(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    entered = (message.text or '').strip()
    await _safe_delete(message)
    ok, reason = await verify_and_apply_email_change(db, db_user, entered)
    if ok:
        await state.clear()
        logger.info('custom_profile: email сменён', user_id=db_user.id, email=_mask_email(db_user.email))
        await message.answer(f'✅ Email изменён на <code>{html.escape(db_user.email or "")}</code>.', parse_mode='HTML', reply_markup=_back_to_profile_keyboard(texts))
        return
    data = await state.get_data()
    attempts = int(data.get('attempts', 0)) + 1
    if attempts >= MAX_CODE_ATTEMPTS:
        try:
            await clear_email_change_pending(db, db_user)
        except Exception:
            pass
        await state.clear()
        logger.info('custom_profile: смена email отменена (попытки/ошибка)', user_id=db_user.id, reason=reason)
        await message.answer('❌ Не удалось подтвердить. Смена email отменена.', reply_markup=_back_to_profile_keyboard(texts))
        return
    await state.update_data(attempts=attempts)
    await message.answer(f'❌ Неверный или истёкший код. Осталось попыток: {MAX_CODE_ATTEMPTS - attempts}. Введите код:', reply_markup=_cancel_keyboard(texts))


# ─────────────────────────────────────────────────────────────
# Смена пароля (старый → новый)
# ─────────────────────────────────────────────────────────────

async def change_password_start(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)
    if not db_user.password_hash:
        await callback.answer('Сначала привяжите email и пароль.', show_alert=True)
        return
    await state.set_state(ChangePasswordStates.waiting_old_password)
    await state.update_data(attempts=0)
    logger.info('custom_profile: смена пароля начата', user_id=db_user.id)
    await callback.message.answer('🔐 Введите текущий пароль:', reply_markup=_cancel_keyboard(texts))
    await callback.answer()


async def change_password_old(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    old = message.text or ''
    await _safe_delete(message)
    if not db_user.password_hash or not verify_password(old, db_user.password_hash):
        data = await state.get_data()
        attempts = int(data.get('attempts', 0)) + 1
        if attempts >= MAX_CODE_ATTEMPTS:
            await state.clear()
            logger.info('custom_profile: смена пароля отменена (неверный старый)', user_id=db_user.id)
            await message.answer('❌ Слишком много неверных попыток. Смена пароля отменена.', reply_markup=_back_to_profile_keyboard(texts))
            return
        await state.update_data(attempts=attempts)
        await message.answer(f'❌ Неверный текущий пароль. Осталось попыток: {MAX_CODE_ATTEMPTS - attempts}. Введите ещё раз:', reply_markup=_cancel_keyboard(texts))
        return
    await state.set_state(ChangePasswordStates.waiting_new_password)
    logger.info('custom_profile: текущий пароль подтверждён', user_id=db_user.id)
    await message.answer(f'🔐 Введите новый пароль (минимум {MIN_PASSWORD_LEN} символов):', reply_markup=_cancel_keyboard(texts))


async def change_password_new(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    texts = get_texts(db_user.language)
    new = message.text or ''
    await _safe_delete(message)
    if not (MIN_PASSWORD_LEN <= len(new) <= MAX_PASSWORD_LEN):
        await message.answer(f'❌ Пароль должен быть от {MIN_PASSWORD_LEN} до {MAX_PASSWORD_LEN} символов. Введите ещё раз:', reply_markup=_cancel_keyboard(texts))
        return
    try:
        db_user.password_hash = hash_password(new)
        await db.commit()
        await db.refresh(db_user)
    except Exception as error:
        await db.rollback()
        logger.error('custom_profile: ошибка сохранения нового пароля', user_id=db_user.id, error=str(error))
        await state.clear()
        await message.answer('❌ Ошибка сохранения. Пароль не изменён.', reply_markup=_back_to_profile_keyboard(texts))
        return
    await state.clear()
    logger.info('custom_profile: пароль изменён', user_id=db_user.id)
    await message.answer('✅ Пароль изменён.', reply_markup=_back_to_profile_keyboard(texts))


# ─────────────────────────────────────────────────────────────
# Уведомления (тумблеры) — SCR-NOTIFICATIONS
# ─────────────────────────────────────────────────────────────

def _notif_keyboard(user: User, texts) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, label in CUSTOM_NOTIF_TOGGLES:
        on = bool(get_user_notification_pref(user, key))
        rows.append([
            InlineKeyboardButton(text=f'{label}: {"✅" if on else "❌"}', callback_data=f'kprofile_notif_toggle:{key}')
        ])
    rows.append([InlineKeyboardButton(text=texts.t('CUSTOM_PROFILE_BACK_BUTTON', '‹ К профилю'), callback_data='custom_profile')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_notifications(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    await state.clear()
    texts = get_texts(db_user.language)
    await edit_or_answer_photo(
        callback=callback,
        caption=texts.t('CUSTOM_NOTIF_SCREEN', CUSTOM_NOTIF_SCREEN_DEFAULT),
        keyboard=_notif_keyboard(db_user, texts),
        parse_mode='HTML',
    )
    await callback.answer()


async def toggle_notification(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    data = callback.data or ''
    key = data.split(':', 1)[1] if ':' in data else ''
    if key not in CUSTOM_NOTIF_KEYS:
        await callback.answer()
        return
    new_value = not bool(get_user_notification_pref(db_user, key))
    try:
        new_settings = dict(db_user.notification_settings or {})
        new_settings[key] = new_value
        db_user.notification_settings = new_settings  # переприсваиваем dict — фиксируем изменение JSONB
        await db.commit()
        await db.refresh(db_user)
        logger.info('custom_profile: тумблер уведомления', user_id=db_user.id, key=key, value=new_value)
    except Exception as error:
        await db.rollback()
        logger.error('custom_profile: ошибка сохранения тумблера', user_id=db_user.id, key=key, error=str(error))
        await callback.answer('Ошибка сохранения', show_alert=True)
        return
    await edit_or_answer_photo(
        callback=callback,
        caption=texts.t('CUSTOM_NOTIF_SCREEN', CUSTOM_NOTIF_SCREEN_DEFAULT),
        keyboard=_notif_keyboard(db_user, texts),
        parse_mode='HTML',
    )
    await callback.answer('🔔 Включено' if new_value else '🔕 Выключено')


async def show_manage(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """CUSTOM-UI: подменю «⚙️ Управление» (Variant B, Фаза 3).

    Оставляет карточку статуса подписки на месте, подменяет только клавиатуру
    на вторичные действия (Сменить тариф/Устройства/Сбросить ключ)."""
    if isinstance(callback.message, types.InaccessibleMessage):
        await callback.answer()
        return
    try:
        from app.keyboards.inline import get_subscription_manage_keyboard

        await db.refresh(db_user)
        subscription = db_user.subscription
        if not subscription:
            # Нет подписки — просто возвращаем на экран аккаунта
            from app.handlers.subscription.purchase import show_subscription_info

            await show_subscription_info(callback, db_user, db)
            return
        await callback.message.edit_reply_markup(
            reply_markup=get_subscription_manage_keyboard(
                db_user.language, is_trial=subscription.is_trial, subscription=subscription
            )
        )
        await callback.answer()
    except Exception as error:
        logger.error('custom_profile: ошибка показа подменю «Управление»', error=str(error))
        await callback.answer()


async def redirect_subscription_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """CUSTOM-UI: старый подраздел «Настройки» Бедолаги (subscription_settings) → наш экран аккаунта.
    Чтобы «Назад» с экранов устройств/сброса ключа не открывал старый подраздел Бедолаги."""
    try:
        from app.handlers.subscription.purchase import show_subscription_info
        await show_subscription_info(callback, db_user, db)
    except Exception as error:
        logger.error('custom_profile: ошибка редиректа subscription_settings', error=str(error))
        await callback.answer()


def register_handlers(dp: Dispatcher):
    # CUSTOM-UI: перехват старого подраздела «Настройки» (регистрируется ДО subscription — выигрывает)
    dp.callback_query.register(redirect_subscription_settings, F.data == 'subscription_settings')
    dp.callback_query.register(show_manage, F.data == 'custom_manage')
    dp.callback_query.register(show_profile, F.data == 'custom_profile')
    dp.callback_query.register(cancel_flow, F.data == 'kprofile_cancel')
    dp.callback_query.register(bind_start, F.data == 'kprofile_bind')
    dp.callback_query.register(change_email_start, F.data == 'kprofile_change_email')
    dp.callback_query.register(change_password_start, F.data == 'kprofile_change_password')
    dp.callback_query.register(show_notifications, F.data == 'kprofile_notifications')
    dp.callback_query.register(toggle_notification, F.data.startswith('kprofile_notif_toggle:'))

    dp.message.register(bind_email, StateFilter(BindEmailStates.waiting_email))
    dp.message.register(bind_password, StateFilter(BindEmailStates.waiting_password))
    dp.message.register(bind_code, StateFilter(BindEmailStates.waiting_code))
    dp.message.register(change_email_current_code, StateFilter(ChangeEmailStates.waiting_current_code))
    dp.message.register(change_email_new, StateFilter(ChangeEmailStates.waiting_new_email))
    dp.message.register(change_email_new_code, StateFilter(ChangeEmailStates.waiting_new_code))
    dp.message.register(change_password_old, StateFilter(ChangePasswordStates.waiting_old_password))
    dp.message.register(change_password_new, StateFilter(ChangePasswordStates.waiting_new_password))
