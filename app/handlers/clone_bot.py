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
_MAX_NAME_LEN = 40
_MAX_TITLE_LEN = 40


async def _cancelled(message: types.Message, state: FSMContext) -> bool:
    if (message.text or '').strip().lower() == '/cancel':
        await state.clear()
        await message.answer('Отменено.')
        return True
    return False


@error_handler
async def cmd_clone(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None):
    # A clone bot itself must never offer onboarding — mothership only.
    if clone_bot is not None:
        return
    if not settings.CLONE_TOKEN_SECRET:
        await message.answer('🚧 Функция подключения своих ботов сейчас недоступна.')
        return
    if await count_active(db) >= settings.CLONE_MAX_ACTIVE:
        await message.answer('⚠️ Достигнут общий лимит активных ботов. Попробуйте позже.')
        return
    if await count_for_owner(db, db_user.id) >= settings.CLONE_MAX_PER_USER:
        await message.answer(f'⚠️ На один аккаунт можно подключить не более {settings.CLONE_MAX_PER_USER} ботов.')
        return
    await state.set_state(CloneBotStates.waiting_for_token)
    await message.answer(
        '🤖 <b>Подключение своего бота</b>\n\n'
        'Пришлите токен бота от @BotFather (вида <code>123456:ABC-DEF...</code>).\n'
        'Я подниму его на наших серверах и заведу для него отдельный профиль в панели.\n\n'
        'Для отмены — /cancel'
    )


@error_handler
async def process_token(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None):
    if clone_bot is not None:
        return
    if await _cancelled(message, state):
        return
    token = (message.text or '').strip()
    if not _TOKEN_RE.match(token):
        await message.answer('❌ Это не похоже на токен бота. Пришлите токен от @BotFather или /cancel.')
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

    await state.update_data(token=token, bot_id=me.id, bot_username=me.username, bot_title=me.full_name)
    await state.set_state(CloneBotStates.waiting_for_squad_name)
    await message.answer(
        f'✅ Бот <b>@{me.username}</b> распознан.\n\n'
        'Придумайте <b>название сквада</b> — внутреннее имя группы в панели (например, «Reseller Иван»).'
    )


@error_handler
async def process_squad_name(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None):
    if clone_bot is not None:
        return
    if await _cancelled(message, state):
        return
    name = (message.text or '').strip()
    if not 1 <= len(name) <= _MAX_NAME_LEN:
        await message.answer(f'❌ Название должно быть от 1 до {_MAX_NAME_LEN} символов.')
        return
    await state.update_data(squad_name=name)
    await state.set_state(CloneBotStates.waiting_for_profile_title)
    await message.answer(
        'Отлично! Теперь пришлите <b>заголовок профиля</b> — название, которое клиенты увидят '
        'в приложении VPN (например, «MyVPN»).'
    )


@error_handler
async def process_profile_title(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession, clone_bot=None):
    if clone_bot is not None:
        return
    if await _cancelled(message, state):
        return
    title = (message.text or '').strip()
    if not 1 <= len(title) <= _MAX_TITLE_LEN:
        await message.answer(f'❌ Заголовок должен быть от 1 до {_MAX_TITLE_LEN} символов.')
        return

    data = await state.get_data()
    token = data.get('token')
    bot_id = data.get('bot_id')
    bot_username = data.get('bot_username')
    bot_title = data.get('bot_title')
    squad_name = data.get('squad_name')
    if not token or not bot_id or not squad_name:
        await state.clear()
        await message.answer('⚠️ Сессия истекла. Начните заново: /clone')
        return

    await message.answer('⏳ Создаю бота и сквад в панели…')

    clone = await create_clone_bot(
        db,
        owner_user_id=db_user.id,
        bot_id=bot_id,
        token=token,
        bot_username=bot_username,
        bot_title=bot_title,
        status=CloneBotStatus.PENDING,
    )

    try:
        squad_uuid, squad_real_name = await provision_squad(squad_name, title)
    except Exception as error:
        logger.exception('Squad provisioning failed', clone_id=clone.id)
        await set_status(db, clone.id, CloneBotStatus.ERROR, last_error=str(error)[:480])
        await state.clear()
        await message.answer('❌ Не удалось создать сквад в панели. Бот сохранён со статусом «ошибка» — попробуйте позже из /clone.')
        return

    await set_squad(
        db,
        clone.id,
        external_squad_uuid=squad_uuid,
        external_squad_name=squad_real_name,
        profile_title=title,
        subpage_config_uuid=None,
    )
    await set_status(db, clone.id, CloneBotStatus.ACTIVE)
    await publish_clone_event('add', clone.id)

    await state.clear()
    await message.answer(
        '🎉 <b>Готово!</b>\n\n'
        f'Бот: @{bot_username}\n'
        f'Сквад: <b>{squad_real_name}</b>\n'
        f'Заголовок профиля: <b>{title}</b>\n\n'
        'Бот уже работает на наших серверах. Все, кто зарегистрируются через него, '
        'автоматически попадут в этот профиль.'
    )


def register_handlers(dp: Dispatcher):
    dp.message.register(cmd_clone, Command('clone'))
    dp.message.register(process_token, CloneBotStates.waiting_for_token)
    dp.message.register(process_squad_name, CloneBotStates.waiting_for_squad_name)
    dp.message.register(process_profile_title, CloneBotStates.waiting_for_profile_title)
