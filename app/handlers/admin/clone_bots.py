"""Admin CRM for white-label clone bots (in the main bot).

Lists clones with per-clone stats (users brought + revenue), shows detail, and lets an
admin enable/disable (kill-switch) or delete a clone. Enable/disable publishes a hot-swap
event so the cloner host applies it live (sets/deletes the Telegram webhook, no restart).

Entry: /clones (admin only). Callbacks namespaced ``acl:``.
"""

from __future__ import annotations

import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.clone_bot import (
    delete_clone_bot,
    get_clone_bot,
    get_stats_bulk,
    list_clone_bots,
    set_status,
)
from app.database.models import CloneBotStatus, User
from app.services.clone_runtime.coordinator import publish_clone_event
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)

_PAGE = 8
_STATUS_ICON = {'active': '🟢', 'disabled': '⚪️', 'pending': '🟡', 'error': '🔴'}
# Человекочитаемые подписи статуса (вместо сырых active/disabled).
_STATUS_LABEL = {'active': 'включён', 'disabled': 'выключен', 'pending': 'создаётся', 'error': 'ошибка'}


def _rub(kopeks: int) -> str:
    return f'{(kopeks or 0) / 100:.0f}₽'


async def _render_list(db: AsyncSession, page: int) -> tuple[str, InlineKeyboardMarkup]:
    clones = await list_clone_bots(db, offset=page * _PAGE, limit=_PAGE + 1)
    has_next = len(clones) > _PAGE
    clones = clones[:_PAGE]
    stats = await get_stats_bulk(db, [c.id for c in clones])

    if not clones and page == 0:
        text = '🤖 <b>Клон-боты</b>\n\nПока нет ни одного подключённого бота.'
        return text, InlineKeyboardMarkup(inline_keyboard=[])

    rows: list[list[InlineKeyboardButton]] = []
    for c in clones:
        st = stats.get(c.id, {})
        icon = _STATUS_ICON.get(c.status, '❔')
        label = f'{icon} @{c.bot_username or c.bot_id} · 👥{st.get("users", 0)} · {_rub(st.get("revenue_kopeks", 0))}'
        rows.append([InlineKeyboardButton(text=label, callback_data=f'acl:view:{c.id}')])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text='◀️', callback_data=f'acl:list:{page - 1}'))
    if has_next:
        nav.append(InlineKeyboardButton(text='▶️', callback_data=f'acl:list:{page + 1}'))
    if nav:
        rows.append(nav)

    text = f'🤖 <b>Клон-боты</b> (стр. {page + 1})\n\nВыберите бота для управления:'
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_detail(db: AsyncSession, clone_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    clone = await get_clone_bot(db, clone_id)
    if clone is None:
        return None
    stats = await get_stats_bulk(db, [clone.id])
    st = stats.get(clone.id, {})
    icon = _STATUS_ICON.get(clone.status, '❔')

    lines = [
        f'{icon} <b>@{html.escape(clone.bot_username or str(clone.bot_id))}</b>',
        f'Статус: <b>{_STATUS_LABEL.get(clone.status, clone.status)}</b>',
        f'Сквад: <b>{html.escape(clone.external_squad_name or "—")}</b>',
        f'Заголовок профиля: <b>{html.escape(clone.profile_title or "—")}</b>',
        f'Привёл пользователей: <b>{st.get("users", 0)}</b>',
        f'Выручка: <b>{_rub(st.get("revenue_kopeks", 0))}</b>',
        f'Владелец (user_id): <code>{clone.owner_user_id}</code>',
    ]
    if clone.last_error:
        lines.append(f'\n⚠️ Ошибка: <code>{html.escape(clone.last_error[:300])}</code>')

    if clone.status == CloneBotStatus.ACTIVE.value:
        toggle = InlineKeyboardButton(text='⏸ Выключить', callback_data=f'acl:tg:{clone.id}')
    else:
        toggle = InlineKeyboardButton(text='▶️ Включить', callback_data=f'acl:tg:{clone.id}')

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [toggle, InlineKeyboardButton(text='🗑 Удалить', callback_data=f'acl:del:{clone.id}')],
            [InlineKeyboardButton(text='◀️ К списку', callback_data='acl:list:0')],
        ]
    )
    return '\n'.join(lines), kb


@error_handler
async def cmd_clones(message: types.Message, db_user: User, db: AsyncSession, is_admin: bool = False):
    if not is_admin:
        return
    text, kb = await _render_list(db, 0)
    await message.answer(text, reply_markup=kb)


@error_handler
async def show_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False):
    if not is_admin:
        await callback.answer('Нет доступа', show_alert=True)
        return
    page = int(callback.data.split(':')[2]) if callback.data.count(':') >= 2 else 0
    text, kb = await _render_list(db, page)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@error_handler
async def show_detail(callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False):
    if not is_admin:
        await callback.answer('Нет доступа', show_alert=True)
        return
    clone_id = int(callback.data.split(':')[2])
    rendered = await _render_detail(db, clone_id)
    if rendered is None:
        await callback.answer('Бот не найден', show_alert=True)
        return
    text, kb = rendered
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@error_handler
async def toggle_clone(callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False):
    if not is_admin:
        await callback.answer('Нет доступа', show_alert=True)
        return
    clone_id = int(callback.data.split(':')[2])
    clone = await get_clone_bot(db, clone_id)
    if clone is None:
        await callback.answer('Бот не найден', show_alert=True)
        return
    if clone.status == CloneBotStatus.ACTIVE.value:
        await set_status(db, clone_id, CloneBotStatus.DISABLED)
        await publish_clone_event('remove', clone_id)
        await callback.answer('Выключен')
    else:
        await set_status(db, clone_id, CloneBotStatus.ACTIVE)
        await publish_clone_event('add', clone_id)
        await callback.answer('Включён')
    rendered = await _render_detail(db, clone_id)
    if rendered is not None:
        text, kb = rendered
        await callback.message.edit_text(text, reply_markup=kb)


@error_handler
async def confirm_delete(callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False):
    if not is_admin:
        await callback.answer('Нет доступа', show_alert=True)
        return
    clone_id = int(callback.data.split(':')[2])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='✅ Да, удалить', callback_data=f'acl:delok:{clone_id}')],
            [InlineKeyboardButton(text='◀️ Отмена', callback_data=f'acl:view:{clone_id}')],
        ]
    )
    await callback.message.edit_text(
        'Удалить клон-бота? Он перестанет работать. Сквад в панели и подписки клиентов сохранятся.',
        reply_markup=kb,
    )
    await callback.answer()


@error_handler
async def do_delete(callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False):
    if not is_admin:
        await callback.answer('Нет доступа', show_alert=True)
        return
    clone_id = int(callback.data.split(':')[2])
    # Stop it on the cloner first (deletes webhook + drops from registry), then delete the row.
    await publish_clone_event('remove', clone_id)
    await delete_clone_bot(db, clone_id)
    await callback.answer('Удалён')
    text, kb = await _render_list(db, 0)
    await callback.message.edit_text(text, reply_markup=kb)


def register_handlers(dp: Dispatcher):
    dp.message.register(cmd_clones, Command('clones'))
    dp.callback_query.register(show_list, F.data.startswith('acl:list'))
    dp.callback_query.register(show_detail, F.data.startswith('acl:view:'))
    dp.callback_query.register(toggle_clone, F.data.startswith('acl:tg:'))
    dp.callback_query.register(confirm_delete, F.data.startswith('acl:del:'))
    dp.callback_query.register(do_delete, F.data.startswith('acl:delok:'))
