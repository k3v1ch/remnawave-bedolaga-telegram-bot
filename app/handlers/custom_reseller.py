"""In-bot reseller self-service panel "Мои боты" (CUSTOM-UI).

Lets the person who created a clone bot manage it from INSIDE the main bot (no cabinet):
list their bots with live stats, pause/resume, delete, and create a new one. Admins see
ALL bots. Real feature on the clone-bots backend — no mock.

Mothership-only (a clone bot must not manage clones). Callbacks namespaced ``myb:``.
Reuses the same CRUD + hot-swap publisher as the admin ``/clones`` panel.
"""

from __future__ import annotations

import html
import re

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.config import settings
from app.database.crud.clone_bot import (
    count_active_subscribers,
    count_for_owner,
    delete_clone_bot,
    get_clone_bot,
    get_stats_bulk,
    list_clone_bots,
    set_status,
    update_profile_title,
    update_token,
)
from app.database.models import CloneBot, CloneBotStatus, User
from app.handlers.clone_bot import start_clone_onboarding
from app.services.clone_bot_service import update_squad_profile_title
from app.services.clone_runtime.coordinator import publish_clone_event
from app.states import CloneBotStates
from app.utils.clone_context import is_clone_context
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)

_PAGE = 8
_STATUS_ICON = {'active': '🟢', 'disabled': '⚪️', 'pending': '🟡', 'error': '🔴'}
# Человекочитаемые подписи статуса для владельца/админа (вместо сырых active/disabled).
_STATUS_LABEL = {'active': 'включён', 'disabled': 'выключен', 'pending': 'создаётся', 'error': 'ошибка'}
# Совпадает с валидацией онбординга (app/handlers/clone_bot.py): панель принимает
# имена сквадов только из латиницы/цифр/пробела/дефиса/подчёркивания.
_TOKEN_RE = re.compile(r'^\d{5,}:[\w-]{30,}$')
_NAME_RE = re.compile(r'^[A-Za-z0-9 _-]+$')
_MAX_NAME_LEN = 40


def _rub(kopeks: int) -> str:
    return f'{(kopeks or 0) / 100:.0f}₽'


def _owns(clone: CloneBot, db_user: User, is_admin: bool) -> bool:
    return is_admin or clone.owner_user_id == db_user.id


async def _render_list(
    db: AsyncSession, db_user: User, is_admin: bool, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    owner_filter = None if is_admin else db_user.id
    clones = await list_clone_bots(db, owner_user_id=owner_filter, offset=page * _PAGE, limit=_PAGE + 1)
    has_next = len(clones) > _PAGE
    clones = clones[:_PAGE]
    stats = await get_stats_bulk(db, [c.id for c in clones])

    title = '🤖 <b>Боты (админ)</b>' if is_admin else '🤖 <b>Мои боты</b>'
    rows: list[list[InlineKeyboardButton]] = []
    for c in clones:
        st = stats.get(c.id, {})
        icon = _STATUS_ICON.get(c.status, '❔')
        label = f'{icon} @{c.bot_username or c.bot_id} · 👥{st.get("users", 0)} · {_rub(st.get("real_topup_kopeks", 0))}'
        rows.append([InlineKeyboardButton(text=label, callback_data=f'myb:view:{c.id}')])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text='◀️', callback_data=f'myb:list:{page - 1}'))
    if has_next:
        nav.append(InlineKeyboardButton(text='▶️', callback_data=f'myb:list:{page + 1}'))
    if nav:
        rows.append(nav)
    # «Создать бота» исчезает при достижении лимита (как в макете). Считаем только
    # для владельца (для админа — его собственные боты); сам онбординг тоже гейтит cap.
    owned = await count_for_owner(db, db_user.id)
    if owned < settings.CLONE_MAX_PER_USER:
        rows.append([InlineKeyboardButton(text='➕ Создать бота', callback_data='myb:create')])
    rows.append([InlineKeyboardButton(text='‹ Назад', callback_data='back_to_menu')])

    if not clones and page == 0:
        text = (
            f'{title}\n\n'
            'У вас пока нет подключённых ботов.\n'
            'Нажмите «➕ Создать бота» — поднимем ваш VPN-бот на наших серверах, '
            'а все его клиенты попадут в ваш отдельный профиль.'
        )
    else:
        text = f'{title} (стр. {page + 1})\n\nВыберите бота для управления:'
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_detail(
    db: AsyncSession, clone: CloneBot, is_admin: bool
) -> tuple[str, InlineKeyboardMarkup]:
    st = (await get_stats_bulk(db, [clone.id])).get(clone.id, {})
    active = await count_active_subscribers(db, clone.id)
    icon = _STATUS_ICON.get(clone.status, '❔')

    lines = [
        f'{icon} <b>@{html.escape(clone.bot_username or str(clone.bot_id))}</b>',
        f'Статус: <b>{_STATUS_LABEL.get(clone.status, clone.status)}</b>',
        f'Сквад: <b>{html.escape(clone.external_squad_name or "—")}</b>',
        f'Заголовок профиля: <b>{html.escape(clone.profile_title or "—")}</b>',
        '',
        f'👥 Привёл пользователей: <b>{st.get("users", 0)}</b>',
        f'✅ С активной подпиской: <b>{active}</b>',
        f'💳 Пополнения: <b>{_rub(st.get("real_topup_kopeks", 0))}</b>',
        f'📊 Оборот: <b>{_rub(st.get("revenue_kopeks", 0))}</b>',
    ]
    if is_admin:
        lines.append(f'Владелец (user_id): <code>{clone.owner_user_id}</code>')
    if clone.last_error:
        lines.append(f'\n⚠️ Ошибка: <code>{html.escape(clone.last_error[:300])}</code>')

    if clone.status == CloneBotStatus.ACTIVE.value:
        toggle = InlineKeyboardButton(text='⏸ Выключить', callback_data=f'myb:tg:{clone.id}')
    else:
        toggle = InlineKeyboardButton(text='▶️ Включить', callback_data=f'myb:tg:{clone.id}')

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='✏️ Название', callback_data=f'myb:rn:{clone.id}'),
                InlineKeyboardButton(text='🔑 Токен', callback_data=f'myb:tok:{clone.id}'),
            ],
            [toggle, InlineKeyboardButton(text='🗑 Удалить', callback_data=f'myb:del:{clone.id}')],
            [InlineKeyboardButton(text='◀️ К списку', callback_data='myb:list:0')],
        ]
    )
    return '\n'.join(lines), kb


async def _guarded_clone(
    callback: types.CallbackQuery, db: AsyncSession, db_user: User, is_admin: bool
) -> CloneBot | None:
    """Resolve the clone from ``myb:<action>:<id>`` and enforce owner/admin access."""
    clone_id = int(callback.data.split(':')[2])
    clone = await get_clone_bot(db, clone_id)
    if clone is None:
        await callback.answer('Бот не найден', show_alert=True)
        return None
    if not _owns(clone, db_user, is_admin):
        await callback.answer('Это не ваш бот', show_alert=True)
        return None
    return clone


# -- handlers -----------------------------------------------------------------


@error_handler
async def open_panel(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    parts = callback.data.split(':')
    page = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
    text, kb = await _render_list(db, db_user, is_admin, page)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@error_handler
async def view_bot(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    text, kb = await _render_detail(db, clone, is_admin)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@error_handler
async def toggle_bot(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    if clone.status == CloneBotStatus.ACTIVE.value:
        await set_status(db, clone.id, CloneBotStatus.DISABLED)
        await publish_clone_event('remove', clone.id)
        await callback.answer('Бот выключен')
    else:
        await set_status(db, clone.id, CloneBotStatus.ACTIVE)
        await publish_clone_event('add', clone.id)
        await callback.answer('Бот включён')
    clone = await get_clone_bot(db, clone.id)
    text, kb = await _render_detail(db, clone, is_admin)
    await callback.message.edit_text(text, reply_markup=kb)


@error_handler
async def confirm_delete(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='✅ Да, удалить', callback_data=f'myb:delok:{clone.id}')],
            [InlineKeyboardButton(text='◀️ Отмена', callback_data=f'myb:view:{clone.id}')],
        ]
    )
    await callback.message.edit_text(
        'Удалить бота? Он перестанет работать. Профиль в панели и активные подписки клиентов сохранятся.',
        reply_markup=kb,
    )
    await callback.answer()


@error_handler
async def do_delete(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    # Stop it on the cloner first (drops webhook + registry), then delete the row.
    await publish_clone_event('remove', clone.id)
    await delete_clone_bot(db, clone.id)
    await callback.answer('Бот удалён')
    text, kb = await _render_list(db, db_user, is_admin, 0)
    await callback.message.edit_text(text, reply_markup=kb)


@error_handler
async def create_bot_cb(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    await callback.answer()
    await start_clone_onboarding(callback.message, db_user, state, db, clone_bot)


# -- editing an existing bot (название / токен) -------------------------------


async def _cancelled(message: types.Message, state: FSMContext) -> bool:
    if (message.text or '').strip().lower() == '/cancel':
        await state.clear()
        await message.answer('Отменено.')
        return True
    return False


async def _resolve_edit_clone(
    message: types.Message, state: FSMContext, db: AsyncSession
) -> CloneBot | None:
    """Re-fetch the clone stored in FSM data (ownership was checked when editing began)."""
    data = await state.get_data()
    clone_id = data.get('edit_clone_id')
    clone = await get_clone_bot(db, int(clone_id)) if clone_id else None
    if clone is None:
        await state.clear()
        await message.answer('⚠️ Бот не найден. Откройте «Мои боты» заново.')
        return None
    return clone


@error_handler
async def rename_start(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    await state.set_state(CloneBotStates.waiting_for_rename)
    await state.update_data(edit_clone_id=clone.id)
    await callback.message.answer(
        '✏️ Пришлите новое <b>название</b> бота (его видят клиенты в приложении).\n\n'
        '❗️ Только <b>английские</b> буквы (A–Z), цифры, пробел, дефис «-» и подчёркивание «_».\n'
        'Для отмены — /cancel'
    )
    await callback.answer()


@error_handler
async def process_rename(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    if await _cancelled(message, state):
        return
    name = (message.text or '').strip()
    if not 1 <= len(name) <= _MAX_NAME_LEN:
        await message.answer(f'❌ Название должно быть от 1 до {_MAX_NAME_LEN} символов.')
        return
    if not _NAME_RE.match(name):
        await message.answer(
            '❌ Можно только <b>английские</b> буквы, цифры, пробел, «-» и «_». '
            'Пришлите другое название.'
        )
        return
    clone = await _resolve_edit_clone(message, state, db)
    if clone is None:
        return

    await update_profile_title(db, clone.id, name)
    if clone.external_squad_uuid:
        try:
            await update_squad_profile_title(clone.external_squad_uuid, name)
        except Exception:
            logger.warning('Failed to update squad profile title', clone_id=clone.id, exc_info=True)
    await state.clear()
    # Сначала отвечаем по живой сессии, потом перезагружаем клон. Если редактируем тот же
    # бот, из которого пишем, reload закрывает его сессию в клонере (тот же процесс) — отправь
    # мы ответ после publish, он упал бы с ServerDisconnectedError.
    await message.answer(f'✅ Название обновлено: <b>{html.escape(name)}</b>')
    await publish_clone_event('reload', clone.id)  # обновить snapshot.profile_title в клонере


@error_handler
async def token_start(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    await state.set_state(CloneBotStates.waiting_for_new_token)
    await state.update_data(edit_clone_id=clone.id)
    await callback.message.answer(
        '🔑 Пришлите новый <b>токен</b> бота от @BotFather.\n\n'
        'Это должен быть токен <b>того же самого</b> бота (например, после смены или отзыва токена). '
        'Бот сразу перезапустится с новым токеном — пересоздавать его не нужно.\n\n'
        'Для отмены — /cancel'
    )
    await callback.answer()


@error_handler
async def process_new_token(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    if await _cancelled(message, state):
        return
    token = (message.text or '').strip()
    if not _TOKEN_RE.match(token):
        await message.answer('❌ Это не похоже на токен бота. Пришлите токен от @BotFather или /cancel.')
        return
    clone = await _resolve_edit_clone(message, state, db)
    if clone is None:
        return

    try:
        probe = create_bot(token=token)
    except Exception:
        await message.answer('❌ Некорректный формат токена. Проверьте и пришлите ещё раз.')
        return
    try:
        me = await probe.get_me()
    except Exception:
        await message.answer('❌ Telegram отклонил токен (возможно, отозван). Пришлите другой или /cancel.')
        return
    finally:
        try:
            await probe.session.close()
        except Exception:
            pass

    if me.id != clone.bot_id:
        await message.answer(
            '❌ Это токен другого бота. Нужен новый токен <b>этого же</b> бота. '
            'Чтобы подключить другой бот — создайте новый в «Мои боты».'
        )
        return

    await update_token(db, clone.id, token=token, bot_username=me.username, bot_title=me.full_name)
    await state.clear()
    # Отвечаем ДО reload: при смене токена того же бота, из которого пишем, reload закрывает
    # его старую сессию в клонере (тот же процесс) — ответ после publish упал бы с
    # ServerDisconnectedError. Сам reload пересоздаст Bot и переустановит webhook.
    await message.answer(f'✅ Токен обновлён. Бот @{me.username} перезапущен с новым токеном.')
    await publish_clone_event('reload', clone.id)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(open_panel, F.data.startswith('myb:list'))
    dp.callback_query.register(view_bot, F.data.startswith('myb:view:'))
    dp.callback_query.register(toggle_bot, F.data.startswith('myb:tg:'))
    dp.callback_query.register(rename_start, F.data.startswith('myb:rn:'))
    dp.callback_query.register(token_start, F.data.startswith('myb:tok:'))
    dp.callback_query.register(confirm_delete, F.data.startswith('myb:del:'))
    dp.callback_query.register(do_delete, F.data.startswith('myb:delok:'))
    dp.callback_query.register(create_bot_cb, F.data == 'myb:create')
    dp.message.register(process_rename, CloneBotStates.waiting_for_rename)
    dp.message.register(process_new_token, CloneBotStates.waiting_for_new_token)
