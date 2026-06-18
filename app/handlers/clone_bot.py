"""Self-serve white-label clone-bot onboarding (runs in the MAIN bot).

Flow: user sends /clone → pastes a BotFather token → we validate via getMe and check
uniqueness → ask for an external-squad name and a profile title → create the clone row,
provision the Remnawave external squad (name + profileTitle), mark it ACTIVE, and publish
a hot-swap event so the cloner host picks the bot up live (no restart).

Gated to the mothership only (a clone bot must not spawn clones) and behind
``CLONE_TOKEN_SECRET`` + per-user / global caps.
"""

from __future__ import annotations

import re

import structlog
from aiogram import Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.config import settings
from app.database.crud.clone_bot import (
    count_active,
    count_for_owner,
    create_clone_bot,
    delete_clone_bot,
    get_clone_bot_by_bot_id,
    set_squad,
    set_status,
)
from app.database.models import CloneBotStatus, User
from app.services.clone_bot_service import provision_squad
from app.services.clone_runtime.coordinator import publish_clone_event
from app.states import CloneBotStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)

_TOKEN_RE = re.compile(r'^\d{5,}:[\w-]{30,}$')
# Remnawave rejects external-squad names outside this set (Latin only):
# "Name can only contain letters, numbers, underscores, dashes and spaces".
_SQUAD_NAME_RE = re.compile(r'^[A-Za-z0-9 _-]+$')
_MAX_NAME_LEN = 40
_MAX_TITLE_LEN = 40


async def _cancelled(message: types.Message, state: FSMContext) -> bool:
    if (message.text or '').strip().lower() == '/cancel':
        await state.clear()
        await message.answer('Отменено.')
        return True
    return False


async def start_clone_onboarding(target, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None) -> None:
    """Shared onboarding entry — used by the /clone command and the in-bot reseller
    panel ("Мои боты"). ``target`` is anything with an async ``.answer()`` (a Message
    or ``callback.message``). Available in clone bots too — a reseller can grow their own
    sub-network; every new bot is just another sibling clone owned by its creator."""
    if not settings.CLONE_TOKEN_SECRET:
        await target.answer('🚧 Функция подключения своих ботов сейчас недоступна.')
        return
    if await count_active(db) >= settings.CLONE_MAX_ACTIVE:
        await target.answer('⚠️ Достигнут общий лимит активных ботов. Попробуйте позже.')
        return
    if await count_for_owner(db, db_user.id) >= settings.CLONE_MAX_PER_USER:
        await target.answer(f'⚠️ На один аккаунт можно подключить не более {settings.CLONE_MAX_PER_USER} ботов.')
        return
    # Step 1 — NAME first (more intuitive than asking for a token blind).
    await state.set_state(CloneBotStates.waiting_for_squad_name)
    await target.answer(
        '🤖 <b>Создание своего бота</b>\n\n'
        'Шаг 1/2 — придумайте <b>название</b> вашего VPN (его увидят клиенты в приложении).\n\n'
        '❗️ Только <b>английские</b> буквы (A–Z), цифры, пробел, дефис «-» и подчёркивание «_». '
        'Кириллица и эмодзи не подойдут.\n'
        'Например: <code>MyVPN</code>, <code>Ivan VPN</code>, <code>Turbo-Net</code>.\n\n'
        'Для отмены — /cancel'
    )


@error_handler
async def cmd_clone(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None):
    await start_clone_onboarding(message, db_user, state, db, clone_bot)


@error_handler
async def process_squad_name(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None):
    """Step 1 — the NAME (used both for the panel squad and the client-facing profile
    title). Validated against the panel's charset up front, then we ask for the token."""
    if await _cancelled(message, state):
        return
    name = (message.text or '').strip()
    if not 1 <= len(name) <= _MAX_NAME_LEN:
        await message.answer(f'❌ Название должно быть от 1 до {_MAX_NAME_LEN} символов.')
        return
    if not _SQUAD_NAME_RE.match(name):
        await message.answer(
            '❌ Можно только <b>английские</b> буквы (A–Z), цифры, пробел, дефис «-» и подчёркивание «_». '
            'Кириллица и эмодзи не подойдут — пришлите другое название.'
        )
        return

    await state.update_data(clone_name=name)
    await state.set_state(CloneBotStates.waiting_for_token)
    await message.answer(
        f'✅ Название: <b>{name}</b>\n\n'
        'Шаг 2/2 — пришлите <b>токен</b> бота от @BotFather (вида <code>123456:ABC-DEF...</code>).\n'
        'Я подниму его на наших серверах — дальше он работает сам.\n\n'
        'Для отмены — /cancel'
    )


@error_handler
async def process_token(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None):
    """Step 2 — the TOKEN. Validates via getMe + uniqueness, then creates the clone,
    provisions the squad (name from step 1) and activates it (hot-swap)."""
    if await _cancelled(message, state):
        return
    token = (message.text or '').strip()
    if not _TOKEN_RE.match(token):
        await message.answer('❌ Это не похоже на токен бота. Пришлите токен от @BotFather или /cancel.')
        return

    data = await state.get_data()
    name = data.get('clone_name')
    if not name:
        await state.clear()
        await message.answer('⚠️ Сессия истекла. Начните заново: /clone')
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

    if await get_clone_bot_by_bot_id(db, me.id) is not None:
        await state.clear()
        await message.answer('⚠️ Этот бот уже подключён.')
        return

    await message.answer(f'⏳ Поднимаю бота <b>@{me.username}</b> под названием <b>{name}</b>…')

    clone = await create_clone_bot(
        db,
        owner_user_id=db_user.id,
        bot_id=me.id,
        token=token,
        bot_username=me.username,
        bot_title=me.full_name,
        status=CloneBotStatus.PENDING,
    )

    try:
        # Same name for the internal squad AND the client-facing profile title.
        squad_uuid, squad_real_name = await provision_squad(name, name)
    except Exception:
        logger.exception('Squad provisioning failed', clone_id=clone.id)
        # Roll the row back so the user can retry the same bot cleanly (the uniqueness
        # guard would otherwise treat the failed bot as "already connected").
        await delete_clone_bot(db, clone.id)
        await state.clear()
        await message.answer('❌ Не удалось создать бота. Попробуйте ещё раз чуть позже: /clone')
        return

    await set_squad(
        db,
        clone.id,
        external_squad_uuid=squad_uuid,
        external_squad_name=squad_real_name,
        profile_title=name,
        subpage_config_uuid=None,
    )
    await set_status(db, clone.id, CloneBotStatus.ACTIVE)
    await publish_clone_event('add', clone.id)

    await state.clear()
    await message.answer(
        '🎉 <b>Готово!</b>\n\n'
        f'Бот: @{me.username}\n'
        f'Название: <b>{name}</b>\n\n'
        f'Бот <b>{name}</b> уже работает — можете им пользоваться и приглашать клиентов. '
        'Все, кто зарегистрируются через него, станут вашими.'
    )


def register_handlers(dp: Dispatcher):
    dp.message.register(cmd_clone, Command('clone'))
    dp.message.register(process_squad_name, CloneBotStates.waiting_for_squad_name)
    dp.message.register(process_token, CloneBotStates.waiting_for_token)
