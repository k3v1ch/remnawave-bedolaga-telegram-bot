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
    get_decrypted_token,
    get_stats_bulk,
    list_clone_bots,
    set_channel_sub_channel,
    set_channel_sub_enabled,
    set_channel_sub_text,
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
    # Здесь ВСЕГДА только свои боты — даже для админа (это пользовательский экран
    # «Создать свой VPN»). Админский обзор всех клонов — /clones (acl:*) в админке.
    clones = await list_clone_bots(db, owner_user_id=db_user.id, offset=page * _PAGE, limit=_PAGE + 1)
    has_next = len(clones) > _PAGE
    clones = clones[:_PAGE]
    stats = await get_stats_bulk(db, [c.id for c in clones])

    title = '🤖 <b>Мои боты</b>'
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
    db: AsyncSession, clone: CloneBot, is_admin: bool, db_user: User | None = None
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

    rows = [
        [
            InlineKeyboardButton(text='✏️ Название', callback_data=f'myb:rn:{clone.id}'),
            InlineKeyboardButton(text='🔑 Токен', callback_data=f'myb:tok:{clone.id}'),
        ],
        [
            InlineKeyboardButton(text='📊 Статистика', callback_data=f'myb:stats:{clone.id}:a'),
            InlineKeyboardButton(text='🔗 Реклама', callback_data=f'myb:links:{clone.id}'),
        ],
        [
            InlineKeyboardButton(text='📢 Рассылка', callback_data=f'myb:bc:{clone.id}'),
            InlineKeyboardButton(text='📌 Обяз. подписка', callback_data=f'myb:sub:{clone.id}'),
        ],
    ]
    # «Наценка» — только партнёру-владельцу (он получает % с пополнений) и админу.
    if db_user is not None and _can_manage_markup(clone, db_user, is_admin):
        rows.append([InlineKeyboardButton(text='💰 Наценка', callback_data=f'myb:mk:{clone.id}')])
    rows += [
        [toggle, InlineKeyboardButton(text='🗑 Удалить', callback_data=f'myb:del:{clone.id}')],
        [InlineKeyboardButton(text='◀️ К списку', callback_data='myb:list:0')],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
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
    text, kb = await _render_detail(db, clone, is_admin, db_user)
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
    text, kb = await _render_detail(db, clone, is_admin, db_user)
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


# -- обязательная подписка на канал владельца ---------------------------------

# Дефолтный текст заглушки (показывается клиентам клона, если владелец не задал свой).
CLONE_SUB_DEFAULT_TEXT = (
    '🔒 Для использования бота подпишитесь на наш канал.\n\n'
    'После подписки нажмите «✅ Я подписался».'
)

_CHANNEL_USERNAME_RE = re.compile(r'^@?([A-Za-z0-9_]{4,32})$')
_CHANNEL_LINK_RE = re.compile(r'^(?:https?://)?t\.me/([A-Za-z0-9_]{4,32})/?$')


def _parse_channel_ref(raw: str) -> str | None:
    """@username / t.me/username / username → '@username' (или None, если не похоже)."""
    raw = raw.strip()
    m = _CHANNEL_LINK_RE.match(raw) or _CHANNEL_USERNAME_RE.match(raw)
    return f'@{m.group(1)}' if m else None


async def _verify_clone_channel(clone: CloneBot, channel_ref: str | int) -> tuple[int, str, str | None] | str:
    """Проверить канал ЧЕРЕЗ САМОГО клон-бота: канал существует и клон-бот в нём админ
    (без админки Telegram не даёт боту звать getChatMember по участникам канала).

    Возвращает ``(chat_id, join_link, title)`` или строку с ошибкой для владельца.
    """
    try:
        probe = create_bot(token=get_decrypted_token(clone))
    except Exception:
        return '❌ Не удалось подключиться к вашему боту. Попробуйте позже.'
    try:
        try:
            chat = await probe.get_chat(channel_ref)
        except Exception:
            return (
                '❌ Канал не найден. Проверьте, что прислали правильный @юзернейм '
                'публичного канала.'
            )
        if chat.type != 'channel':
            return '❌ Это не канал. Пришлите @юзернейм именно канала.'
        try:
            member = await probe.get_chat_member(chat.id, clone.bot_id)
        except Exception:
            member = None
        if member is None or member.status not in ('administrator', 'creator'):
            return (
                f'❌ Бот @{clone.bot_username} не является администратором канала '
                f'{channel_ref}.\n\nДобавьте бота в канал как администратора '
                '(достаточно без особых прав) и пришлите @юзернейм ещё раз.'
            )
        link = f'https://t.me/{chat.username}' if chat.username else (chat.invite_link or '')
        if not link:
            return '❌ Не удалось получить ссылку на канал. Канал должен быть публичным (с @юзернеймом).'
        return chat.id, link, chat.title
    finally:
        try:
            await probe.session.close()
        except Exception:
            pass


async def _render_channel_sub(clone: CloneBot) -> tuple[str, InlineKeyboardMarkup]:
    enabled = bool(clone.channel_sub_enabled)
    has_channel = bool(clone.channel_sub_chat_id)
    status = '🔔 включена' if enabled else '🔕 выключена'
    channel = (
        f'<a href="{clone.channel_sub_link}">{html.escape(clone.channel_sub_title or clone.channel_sub_link)}</a>'
        if has_channel
        else '—'
    )
    text_state = 'свой' if clone.channel_sub_text else 'стандартный'

    lines = [
        '📌 <b>Обязательная подписка</b>',
        '',
        f'Статус: <b>{status}</b>',
        f'Канал: {channel}',
        f'Текст заглушки: <b>{text_state}</b>',
        '',
        'Клиенты вашего бота не смогут пользоваться им, пока не подпишутся на канал.',
    ]
    if not has_channel:
        lines += [
            '',
            '1️⃣ Добавьте вашего бота в канал как <b>администратора</b>.',
            '2️⃣ Нажмите «📡 Указать канал» и пришлите @юзернейм канала.',
        ]

    rows: list[list[InlineKeyboardButton]] = []
    if has_channel:
        toggle_label = '🔕 Выключить' if enabled else '🔔 Включить'
        rows.append([InlineKeyboardButton(text=toggle_label, callback_data=f'myb:subtg:{clone.id}')])
    rows.append([InlineKeyboardButton(text='📡 Указать канал', callback_data=f'myb:subch:{clone.id}')])
    rows.append([InlineKeyboardButton(text='📝 Текст заглушки', callback_data=f'myb:subtxt:{clone.id}')])
    rows.append([InlineKeyboardButton(text='◀️ Назад', callback_data=f'myb:view:{clone.id}')])
    return '\n'.join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@error_handler
async def open_channel_sub(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    text, kb = await _render_channel_sub(clone)
    await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()


@error_handler
async def toggle_channel_sub(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    if not clone.channel_sub_chat_id:
        await callback.answer('Сначала укажите канал', show_alert=True)
        return
    enable = not clone.channel_sub_enabled
    if enable:
        # Перед включением перепроверяем, что бот всё ещё админ канала — права могли снять.
        verified = await _verify_clone_channel(clone, clone.channel_sub_chat_id)
        if isinstance(verified, str):
            await callback.answer(
                'Бот не смог проверить канал (его убрали из админов?). Укажите канал заново.',
                show_alert=True,
            )
            return
    clone = await set_channel_sub_enabled(db, clone.id, enable)
    await publish_clone_event('reload', clone.id)
    text, kb = await _render_channel_sub(clone)
    await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    await callback.answer('Включена ✅' if enable else 'Выключена')


@error_handler
async def sub_channel_start(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    await state.set_state(CloneBotStates.waiting_for_sub_channel)
    await state.update_data(edit_clone_id=clone.id)
    await callback.message.answer(
        '📡 Пришлите <b>@юзернейм</b> вашего канала (например, <code>@my_channel</code>).\n\n'
        f'❗️ Перед этим добавьте бота @{clone.bot_username} в канал как <b>администратора</b> — '
        'иначе он не сможет проверять подписку.\n\n'
        'Для отмены — /cancel'
    )
    await callback.answer()


@error_handler
async def process_sub_channel(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    if await _cancelled(message, state):
        return
    ref = _parse_channel_ref(message.text or '')
    if ref is None:
        await message.answer('❌ Не похоже на @юзернейм канала. Пример: <code>@my_channel</code>. Или /cancel.')
        return
    clone = await _resolve_edit_clone(message, state, db)
    if clone is None:
        return

    await message.answer('⏳ Проверяю канал и права бота…')
    verified = await _verify_clone_channel(clone, ref)
    if isinstance(verified, str):
        await message.answer(verified)
        return

    chat_id, link, title = verified
    clone = await set_channel_sub_channel(db, clone.id, chat_id=chat_id, link=link, title=title)
    await state.clear()
    await publish_clone_event('reload', clone.id)
    await message.answer(
        f'✅ Канал <b>{html.escape(title or link)}</b> привязан, бот имеет нужные права.\n'
        'Теперь включите обязательную подписку кнопкой «🔔 Включить».'
    )
    text, kb = await _render_channel_sub(clone)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@error_handler
async def sub_text_start(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    await state.set_state(CloneBotStates.waiting_for_sub_text)
    await state.update_data(edit_clone_id=clone.id)
    current = clone.channel_sub_text or CLONE_SUB_DEFAULT_TEXT
    await callback.message.answer(
        '📝 Пришлите <b>текст заглушки</b> — его увидят клиенты, пока не подпишутся на канал '
        '(до 1000 символов).\n\n'
        f'Сейчас:\n<blockquote>{html.escape(current)}</blockquote>\n\n'
        'Чтобы вернуть стандартный текст — пришлите <code>-</code>\n'
        'Для отмены — /cancel'
    )
    await callback.answer()


@error_handler
async def process_sub_text(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    if await _cancelled(message, state):
        return
    raw = (message.text or '').strip()
    if not raw:
        await message.answer('❌ Пришлите текст сообщением (или /cancel).')
        return
    if len(raw) > 1000:
        await message.answer(f'❌ Слишком длинно ({len(raw)} симв.), максимум 1000.')
        return
    clone = await _resolve_edit_clone(message, state, db)
    if clone is None:
        return
    new_text = None if raw == '-' else raw
    clone = await set_channel_sub_text(db, clone.id, new_text)
    await state.clear()
    await publish_clone_event('reload', clone.id)
    await message.answer('✅ Текст сброшен на стандартный.' if new_text is None else '✅ Текст заглушки обновлён.')
    text, kb = await _render_channel_sub(clone)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


# -- статистика по периодам ------------------------------------------------------

_STATS_PERIODS: dict[str, tuple[str, int | None]] = {
    'd': ('день', 1),
    'w': ('неделю', 7),
    'm': ('месяц', 30),
    'a': ('всё время', None),
}


async def _render_stats(db: AsyncSession, clone: CloneBot, period: str) -> tuple[str, InlineKeyboardMarkup]:
    from datetime import UTC, datetime, timedelta

    from app.database.crud.clone_bot import get_period_stats

    if period not in _STATS_PERIODS:
        period = 'a'
    label, days = _STATS_PERIODS[period]
    since = datetime.now(UTC) - timedelta(days=days) if days else None
    st = await get_period_stats(db, clone.id, since)

    reward_days = st['owner_reward_days_awards'] * settings.REFERRAL_INVITER_TOPUP_BONUS_DAYS
    lines = [
        f'📊 <b>Статистика @{html.escape(clone.bot_username or str(clone.id))}</b> за <b>{label}</b>',
        '',
        f'👥 Новые пользователи: <b>{st["new_users"]}</b>',
        f'🛒 Покупки подписок: <b>{st["purchases"]}</b>',
        f'💳 Пополнения: <b>{_rub(st["real_topup_kopeks"])}</b>',
    ]
    if st['owner_reward_kopeks']:
        lines.append(f'💰 Ваш доход: <b>{_rub(st["owner_reward_kopeks"])}</b>')
    if reward_days:
        lines.append(f'🎁 Бонусные дни вам: <b>+{reward_days} дн.</b>')

    period_row = [
        InlineKeyboardButton(
            text=('• ' if p == period else '') + name.capitalize(),
            callback_data=f'myb:stats:{clone.id}:{p}',
        )
        for p, (name, _) in _STATS_PERIODS.items()
    ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            period_row[:2],
            period_row[2:],
            [InlineKeyboardButton(text='◀️ Назад', callback_data=f'myb:view:{clone.id}')],
        ]
    )
    return '\n'.join(lines), kb


@error_handler
async def open_stats(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    parts = callback.data.split(':')
    period = parts[3] if len(parts) > 3 else 'a'
    text, kb = await _render_stats(db, clone, period)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass  # переключение на тот же период — message is not modified
    await callback.answer()


# -- рекламные ссылки ----------------------------------------------------------


async def _guarded_link(
    callback: types.CallbackQuery, db: AsyncSession, db_user: User, is_admin: bool
):
    """Resolve ``myb:<action>:<link_id>`` → (link, clone) с проверкой владения."""
    from app.database.crud.clone_bot_link import get_link

    link_id = int(callback.data.split(':')[2])
    link = await get_link(db, link_id)
    if link is None:
        await callback.answer('Ссылка не найдена', show_alert=True)
        return None, None
    clone = await get_clone_bot(db, link.clone_bot_id)
    if clone is None or not _owns(clone, db_user, is_admin):
        await callback.answer('Это не ваш бот', show_alert=True)
        return None, None
    return link, clone


async def _render_links_list(db: AsyncSession, clone: CloneBot) -> tuple[str, InlineKeyboardMarkup]:
    from app.database.crud.clone_bot_link import MAX_LINKS_PER_CLONE, list_links

    links = await list_links(db, clone.id)
    lines = [
        '🔗 <b>Рекламные ссылки</b>',
        '',
        'Создавайте отдельную ссылку под каждое размещение рекламы — '
        'увидите, сколько людей пришло и сколько они пополнили.',
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for link in links:
        label = f'{link.name} · 👆{link.clicks_count} · 👥{link.registrations_count}'
        rows.append([InlineKeyboardButton(text=label, callback_data=f'myb:lnk:{link.id}')])
    if not links:
        lines += ['', 'Пока нет ни одной ссылки.']
    if len(links) < MAX_LINKS_PER_CLONE:
        rows.append([InlineKeyboardButton(text='➕ Создать ссылку', callback_data=f'myb:lnkadd:{clone.id}')])
    rows.append([InlineKeyboardButton(text='◀️ Назад', callback_data=f'myb:view:{clone.id}')])
    return '\n'.join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_link_detail(db: AsyncSession, link, clone: CloneBot) -> tuple[str, InlineKeyboardMarkup]:
    from app.database.crud.clone_bot_link import get_link_stats

    stats = await get_link_stats(db, link.id)
    url = f'https://t.me/{clone.bot_username}?start={link.start_parameter}'
    lines = [
        f'🔗 <b>{html.escape(link.name)}</b>',
        '',
        f'<code>{url}</code>',
        '',
        f'👆 Переходы: <b>{link.clicks_count}</b>',
        f'👥 Регистрации: <b>{link.registrations_count}</b>',
        f'💳 Пополнили: <b>{_rub(stats.get("real_topup_kopeks", 0))}</b>',
        '',
        'Нажмите на ссылку, чтобы скопировать.',
    ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='🗑 Удалить', callback_data=f'myb:lnkdel:{link.id}')],
            [InlineKeyboardButton(text='◀️ К ссылкам', callback_data=f'myb:links:{clone.id}')],
        ]
    )
    return '\n'.join(lines), kb


@error_handler
async def open_links(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    text, kb = await _render_links_list(db, clone)
    await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()


@error_handler
async def view_link(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    link, clone = await _guarded_link(callback, db, db_user, is_admin)
    if link is None:
        return
    text, kb = await _render_link_detail(db, link, clone)
    await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()


@error_handler
async def link_add_start(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    from app.database.crud.clone_bot_link import MAX_LINKS_PER_CLONE, count_links

    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    if await count_links(db, clone.id) >= MAX_LINKS_PER_CLONE:
        await callback.answer(f'Максимум {MAX_LINKS_PER_CLONE} ссылок на бота', show_alert=True)
        return
    await state.set_state(CloneBotStates.waiting_for_link_name)
    await state.update_data(edit_clone_id=clone.id)
    await callback.message.answer(
        '➕ Пришлите <b>название</b> ссылки — где размещаете рекламу '
        '(например, <code>Канал у Васи</code>). До 50 символов.\n\n'
        'Для отмены — /cancel'
    )
    await callback.answer()


@error_handler
async def process_link_name(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    from app.database.crud.clone_bot_link import create_link

    if await _cancelled(message, state):
        return
    name = (message.text or '').strip()
    if not 1 <= len(name) <= 50:
        await message.answer('❌ Название должно быть от 1 до 50 символов.')
        return
    clone = await _resolve_edit_clone(message, state, db)
    if clone is None:
        return
    link = await create_link(db, clone.id, name)
    await state.clear()
    url = f'https://t.me/{clone.bot_username}?start={link.start_parameter}'
    await message.answer(
        f'✅ Ссылка <b>{html.escape(name)}</b> создана:\n\n'
        f'<code>{url}</code>\n\n'
        'Размещайте её в рекламе — статистика будет в «🔗 Реклама».',
        disable_web_page_preview=True,
    )
    text, kb = await _render_links_list(db, clone)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@error_handler
async def link_delete_confirm(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    link, clone = await _guarded_link(callback, db, db_user, is_admin)
    if link is None:
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='✅ Да, удалить', callback_data=f'myb:lnkdelok:{link.id}')],
            [InlineKeyboardButton(text='◀️ Отмена', callback_data=f'myb:lnk:{link.id}')],
        ]
    )
    await callback.message.edit_text(
        f'Удалить ссылку <b>{html.escape(link.name)}</b>? Она перестанет открывать бота со статистикой, '
        'счётчики пропадут.',
        reply_markup=kb,
    )
    await callback.answer()


@error_handler
async def link_delete(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    from app.database.crud.clone_bot_link import delete_link

    link, clone = await _guarded_link(callback, db, db_user, is_admin)
    if link is None:
        return
    await delete_link(db, link.id)
    await callback.answer('Ссылка удалена')
    text, kb = await _render_links_list(db, clone)
    await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)


# -- наценка (только владельцы-партнёры) ------------------------------------------


def _can_manage_markup(clone: CloneBot, db_user: User, is_admin: bool) -> bool:
    """Наценку видит только ПАРТНЁР-владелец: он зарабатывает % с каждого пополнения,
    и наценка ему выгодна. Обычному владельцу (бонусные дни) кнопку не показываем."""
    if is_admin:
        return True
    return clone.owner_user_id == db_user.id and db_user.is_partner


async def _render_markup(db: AsyncSession, clone: CloneBot) -> tuple[str, InlineKeyboardMarkup]:
    from datetime import UTC, datetime, timedelta

    from app.database.crud.clone_bot import sum_purchases_kopeks
    from app.database.crud.tariff import get_all_tariffs
    from app.services.clone_pricing import apply_clone_markup

    pct = int(clone.pricing_markup_pct or 0)
    lines = [
        '💰 <b>Наценка на тарифы</b>',
        '',
        f'Текущая наценка: <b>{pct}%</b>',
        '',
        'Клиенты вашего бота видят и платят цены с наценкой. Основной бот и другие боты '
        'она не затрагивает. Ваш заработок — партнёрский процент с каждого пополнения: '
        'выше цены → больше пополняют → больше ваш доход.',
    ]

    tariffs = await get_all_tariffs(db, limit=4)
    preview = []
    for t in tariffs:
        prices = t.period_prices or {}
        if getattr(t, 'is_daily', False) and (t.daily_price_kopeks or 0) > 0:
            base = t.daily_price_kopeks
            suffix = '/день'
        elif prices:
            base = prices[min(prices.keys(), key=int)]
            suffix = ''
        else:
            continue
        marked = apply_clone_markup(base, pct)
        row = f'• {html.escape(t.name)}: {_rub(base)}{suffix}'
        if pct > 0:
            row += f' → <b>{_rub(marked)}{suffix}</b>'
        preview.append(row)
    if preview:
        lines += ['', '<b>Цены в вашем боте:</b>', *preview]

    if pct > 0:
        since = datetime.now(UTC) - timedelta(days=30)
        purchases = await sum_purchases_kopeks(db, clone.id, since)
        markup_share = purchases * pct // (100 + pct)
        lines += [
            '',
            f'📈 Покупок за 30 дней: <b>{_rub(purchases)}</b>, '
            f'из них наценка: <b>~{_rub(markup_share)}</b> (оценка по текущему %).',
        ]

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='✏️ Изменить наценку', callback_data=f'myb:mkset:{clone.id}')],
            [InlineKeyboardButton(text='◀️ Назад', callback_data=f'myb:view:{clone.id}')],
        ]
    )
    return '\n'.join(lines), kb


@error_handler
async def open_markup(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    if not _can_manage_markup(clone, db_user, is_admin):
        await callback.answer('Наценка доступна только партнёрам', show_alert=True)
        return
    text, kb = await _render_markup(db, clone)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@error_handler
async def markup_set_start(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    from app.services.clone_pricing import MAX_MARKUP_PCT

    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    if not _can_manage_markup(clone, db_user, is_admin):
        await callback.answer('Наценка доступна только партнёрам', show_alert=True)
        return
    await state.set_state(CloneBotStates.waiting_for_markup)
    await state.update_data(edit_clone_id=clone.id)
    await callback.message.answer(
        f'✏️ Пришлите <b>процент наценки</b> — целое число от 0 до {MAX_MARKUP_PCT}.\n\n'
        'Например: <code>20</code> — цены в вашем боте станут на 20% выше базовых.\n'
        '<code>0</code> — без наценки.\n\n'
        'Для отмены — /cancel'
    )
    await callback.answer()


@error_handler
async def process_markup(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    from app.database.crud.clone_bot import set_pricing_markup
    from app.services.clone_pricing import MAX_MARKUP_PCT

    if await _cancelled(message, state):
        return
    raw = (message.text or '').strip().replace('%', '')
    if not raw.lstrip('-').isdigit():
        await message.answer('❌ Введите целое число, например <code>20</code>.')
        return
    pct = int(raw)
    if not 0 <= pct <= MAX_MARKUP_PCT:
        await message.answer(f'❌ Допустимый диапазон: 0–{MAX_MARKUP_PCT}.')
        return
    clone = await _resolve_edit_clone(message, state, db)
    if clone is None:
        return
    if not _can_manage_markup(clone, db_user, is_admin):
        await state.clear()
        await message.answer('❌ Наценка доступна только партнёрам.')
        return
    clone = await set_pricing_markup(db, clone.id, pct)
    await state.clear()
    await message.answer(
        f'✅ Наценка установлена: <b>{pct}%</b>. Уже действует для новых покупок в вашем боте.'
        if pct > 0
        else '✅ Наценка отключена — в вашем боте базовые цены.'
    )
    await publish_clone_event('reload', clone.id)  # цены в клонере читаются из snapshot
    text, kb = await _render_markup(db, clone)
    await message.answer(text, reply_markup=kb)


# -- рассылки --------------------------------------------------------------------

_BC_STATUS = {'in_progress': '▶️ Идёт', 'completed': '✅ Завершена', 'failed': '❌ Ошибка'}
_BC_BUTTON_RE = re.compile(r'^\s*(?P<label>.+?)\s+-\s+(?P<url>https?://\S+)\s*$')


async def _render_broadcasts(db: AsyncSession, clone: CloneBot) -> tuple[str, InlineKeyboardMarkup]:
    from app.database.crud.clone_broadcast import CLONE_BROADCASTS_PER_DAY, count_today, list_broadcasts

    items = await list_broadcasts(db, clone.id, limit=10)
    used_today = await count_today(db, clone.id)

    lines = ['📢 <b>Рассылки</b>', '']
    if items:
        lines.append('Последние рассылки:')
        for b in items:
            when = b.created_at.strftime('%d.%m %H:%M') if b.created_at else '—'
            status = _BC_STATUS.get(b.status, b.status)
            lines.append(f'• {when} — {status} · ✅{b.sent_count} 🚫{b.failed_count}')
    else:
        lines.append('Вы ещё не делали рассылок.')
    lines += ['', f'Лимит: {used_today}/{CLONE_BROADCASTS_PER_DAY} за сутки.']

    rows: list[list[InlineKeyboardButton]] = []
    if used_today < CLONE_BROADCASTS_PER_DAY:
        rows.append([InlineKeyboardButton(text='➕ Новая рассылка', callback_data=f'myb:bcadd:{clone.id}')])
    rows.append([InlineKeyboardButton(text='◀️ Назад', callback_data=f'myb:view:{clone.id}')])
    return '\n'.join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@error_handler
async def open_broadcasts(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    text, kb = await _render_broadcasts(db, clone)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@error_handler
async def broadcast_add_start(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    from app.database.crud.clone_broadcast import CLONE_BROADCASTS_PER_DAY, count_today

    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    if await count_today(db, clone.id) >= CLONE_BROADCASTS_PER_DAY:
        await callback.answer(f'Лимит {CLONE_BROADCASTS_PER_DAY} рассылок в сутки исчерпан', show_alert=True)
        return
    await state.set_state(CloneBotStates.waiting_for_broadcast_post)
    await state.update_data(edit_clone_id=clone.id, bc_text=None, bc_file_id=None, bc_btn=None, bc_tariffs=False)
    await callback.message.answer(
        '📢 Пришлите <b>пост</b> для рассылки: текст или фото с подписью.\n\n'
        f'Получат её все клиенты бота @{clone.bot_username}.\n'
        'Для отмены — /cancel'
    )
    await callback.answer()


async def _send_broadcast_preview(message: types.Message, state: FSMContext, clone: CloneBot) -> None:
    """Прислать превью поста (как увидят клиенты) + контрольное меню."""
    from app.services.clone_broadcast_service import build_broadcast_keyboard

    data = await state.get_data()
    btn = data.get('bc_btn') or (None, None)
    kb = build_broadcast_keyboard(btn[0], btn[1], data.get('bc_tariffs', False))
    if data.get('bc_file_id'):
        await message.answer_photo(data['bc_file_id'], caption=data.get('bc_text'), reply_markup=kb)
    else:
        await message.answer(data.get('bc_text') or '', reply_markup=kb, disable_web_page_preview=True)

    tariffs_label = '🛒 Кнопка тарифов: ✅' if data.get('bc_tariffs') else '🛒 Кнопка тарифов: ➖'
    control_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='🔗 URL-кнопка', callback_data=f'myb:bcbtn:{clone.id}')],
            [InlineKeyboardButton(text=tariffs_label, callback_data=f'myb:bctar:{clone.id}')],
            [InlineKeyboardButton(text='🚀 Отправить', callback_data=f'myb:bcgo:{clone.id}')],
            [InlineKeyboardButton(text='❌ Отмена', callback_data=f'myb:bcstop:{clone.id}')],
        ]
    )
    await message.answer('👆 Так пост увидят клиенты. Отправляем?', reply_markup=control_kb)


@error_handler
async def process_broadcast_post(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    if await _cancelled(message, state):
        return
    clone = await _resolve_edit_clone(message, state, db)
    if clone is None:
        return
    if message.photo:
        await state.update_data(bc_file_id=message.photo[-1].file_id, bc_text=message.caption or None)
    elif message.text:
        if len(message.text) > 4000:
            await message.answer('❌ Слишком длинный текст (макс. 4000 символов).')
            return
        await state.update_data(bc_text=message.text, bc_file_id=None)
    else:
        await message.answer('❌ Поддерживаются только текст или фото с подписью. Или /cancel.')
        return
    await state.set_state(None)  # пост принят; дальше управление кнопками
    await _send_broadcast_preview(message, state, clone)


async def _broadcast_ctx(
    callback: types.CallbackQuery, db: AsyncSession, db_user: User, is_admin: bool, state: FSMContext
) -> CloneBot | None:
    """Клон + проверка, что в FSM есть подготовленный пост этого клона."""
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return None
    data = await state.get_data()
    if data.get('edit_clone_id') != clone.id or (not data.get('bc_text') and not data.get('bc_file_id')):
        await callback.answer('Пост не найден — начните рассылку заново.', show_alert=True)
        return None
    return clone


@error_handler
async def broadcast_button_start(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _broadcast_ctx(callback, db, db_user, is_admin, state)
    if clone is None:
        return
    await state.set_state(CloneBotStates.waiting_for_broadcast_button)
    await callback.message.answer(
        '🔗 Пришлите кнопку в формате:\n<code>Текст кнопки - https://ссылка</code>\n\n'
        'Чтобы убрать кнопку — пришлите <code>-</code>\n'
        'Для отмены — /cancel'
    )
    await callback.answer()


@error_handler
async def process_broadcast_button(
    message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None
):
    if await _cancelled(message, state):
        return
    clone = await _resolve_edit_clone(message, state, db)
    if clone is None:
        return
    raw = (message.text or '').strip()
    if raw == '-':
        await state.update_data(bc_btn=None)
    else:
        m = _BC_BUTTON_RE.match(raw)
        if not m:
            await message.answer('❌ Формат: <code>Текст кнопки - https://ссылка</code>. Или <code>-</code> чтобы убрать.')
            return
        label = m.group('label')[:64]
        await state.update_data(bc_btn=(label, m.group('url')))
    await state.set_state(None)
    await _send_broadcast_preview(message, state, clone)


@error_handler
async def broadcast_toggle_tariffs(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _broadcast_ctx(callback, db, db_user, is_admin, state)
    if clone is None:
        return
    data = await state.get_data()
    await state.update_data(bc_tariffs=not data.get('bc_tariffs', False))
    await callback.answer('Кнопка тарифов: ' + ('добавлена ✅' if not data.get('bc_tariffs', False) else 'убрана'))
    await _send_broadcast_preview(callback.message, state, clone)


@error_handler
async def broadcast_cancel(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    clone = await _guarded_clone(callback, db, db_user, is_admin)
    if clone is None:
        return
    await state.clear()
    await callback.answer('Рассылка отменена')
    text, kb = await _render_broadcasts(db, clone)
    await callback.message.edit_text(text, reply_markup=kb)


@error_handler
async def broadcast_send(
    callback: types.CallbackQuery, db_user: User, state: FSMContext, db: AsyncSession, is_admin: bool = False, clone_bot=None
):
    import asyncio

    from app.database.crud.clone_broadcast import (
        CLONE_BROADCASTS_PER_DAY,
        count_today,
        create_broadcast,
        get_recipient_telegram_ids,
    )
    from app.services.clone_broadcast_service import run_clone_broadcast

    clone = await _broadcast_ctx(callback, db, db_user, is_admin, state)
    if clone is None:
        return
    if await count_today(db, clone.id) >= CLONE_BROADCASTS_PER_DAY:
        await callback.answer(f'Лимит {CLONE_BROADCASTS_PER_DAY} рассылок в сутки исчерпан', show_alert=True)
        return

    data = await state.get_data()
    recipients = await get_recipient_telegram_ids(db, clone.id)
    if not recipients:
        await callback.answer('У бота пока нет клиентов — некому отправлять.', show_alert=True)
        return

    btn = data.get('bc_btn') or (None, None)
    broadcast = await create_broadcast(
        db,
        clone.id,
        message_text=data.get('bc_text'),
        media_type='photo' if data.get('bc_file_id') else None,
        media_file_id=data.get('bc_file_id'),
        button_text=btn[0],
        button_url=btn[1],
        show_tariffs_button=bool(data.get('bc_tariffs')),
        total_count=len(recipients),
    )
    token = get_decrypted_token(clone)
    await state.clear()

    asyncio.create_task(
        run_clone_broadcast(
            broadcast,
            clone_token=token,
            main_bot=callback.bot,
            owner_chat_id=callback.from_user.id,
        )
    )

    await callback.answer()
    await callback.message.edit_text(
        f'🚀 Рассылка запущена: <b>{len(recipients)}</b> получателей.\n'
        'По завершении пришлю отчёт.'
    )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(open_panel, F.data.startswith('myb:list'))
    dp.callback_query.register(view_bot, F.data.startswith('myb:view:'))
    dp.callback_query.register(toggle_bot, F.data.startswith('myb:tg:'))
    dp.callback_query.register(open_channel_sub, F.data.startswith('myb:sub:'))
    dp.callback_query.register(toggle_channel_sub, F.data.startswith('myb:subtg:'))
    dp.callback_query.register(sub_channel_start, F.data.startswith('myb:subch:'))
    dp.callback_query.register(sub_text_start, F.data.startswith('myb:subtxt:'))
    dp.callback_query.register(open_stats, F.data.startswith('myb:stats:'))
    dp.callback_query.register(open_links, F.data.startswith('myb:links:'))
    dp.callback_query.register(view_link, F.data.startswith('myb:lnk:'))
    dp.callback_query.register(link_add_start, F.data.startswith('myb:lnkadd:'))
    dp.callback_query.register(link_delete_confirm, F.data.startswith('myb:lnkdel:'))
    dp.callback_query.register(link_delete, F.data.startswith('myb:lnkdelok:'))
    dp.callback_query.register(open_markup, F.data.startswith('myb:mk:'))
    dp.callback_query.register(markup_set_start, F.data.startswith('myb:mkset:'))
    dp.message.register(process_markup, CloneBotStates.waiting_for_markup)
    dp.callback_query.register(open_broadcasts, F.data.startswith('myb:bc:'))
    dp.callback_query.register(broadcast_add_start, F.data.startswith('myb:bcadd:'))
    dp.callback_query.register(broadcast_button_start, F.data.startswith('myb:bcbtn:'))
    dp.callback_query.register(broadcast_toggle_tariffs, F.data.startswith('myb:bctar:'))
    dp.callback_query.register(broadcast_send, F.data.startswith('myb:bcgo:'))
    dp.callback_query.register(broadcast_cancel, F.data.startswith('myb:bcstop:'))
    dp.callback_query.register(rename_start, F.data.startswith('myb:rn:'))
    dp.callback_query.register(token_start, F.data.startswith('myb:tok:'))
    dp.callback_query.register(confirm_delete, F.data.startswith('myb:del:'))
    dp.callback_query.register(do_delete, F.data.startswith('myb:delok:'))
    dp.callback_query.register(create_bot_cb, F.data == 'myb:create')
    dp.message.register(process_rename, CloneBotStates.waiting_for_rename)
    dp.message.register(process_new_token, CloneBotStates.waiting_for_new_token)
    dp.message.register(process_sub_channel, CloneBotStates.waiting_for_sub_channel)
    dp.message.register(process_sub_text, CloneBotStates.waiting_for_sub_text)
    dp.message.register(process_link_name, CloneBotStates.waiting_for_link_name)
    dp.message.register(process_broadcast_post, CloneBotStates.waiting_for_broadcast_post)
    dp.message.register(process_broadcast_button, CloneBotStates.waiting_for_broadcast_button)
