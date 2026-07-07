"""Отправка рассылок клон-ботов.

Запускается фоновой asyncio-задачей из панели «Мои боты» (main-процесс). Шлёт СТРОГО
юзерам одного клона от имени САМОГО клон-бота (временный ``Bot`` из его токена) —
основной бот и другие клоны не затрагиваются.

Медиа: пост составляется в основном боте, поэтому ``media_file_id`` чужой для клона
(file_id в Telegram привязан к боту). Первому получателю фото уходит байтами
(скачиваем основным ботом), из ответа берём clone-scoped file_id и дальше шлём им.
"""

from __future__ import annotations

import asyncio

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot_factory import create_bot
from app.database.crud.clone_broadcast import finish_broadcast, get_recipient_telegram_ids
from app.database.database import AsyncSessionLocal
from app.database.models import CloneBroadcast


logger = structlog.get_logger(__name__)

_SEND_DELAY = 0.06  # ~16 msg/s — с запасом от лимита Telegram (30/s)


def build_broadcast_keyboard(
    button_text: str | None, button_url: str | None, show_tariffs: bool
) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if button_text and button_url:
        rows.append([InlineKeyboardButton(text=button_text, url=button_url)])
    if show_tariffs:
        # menu_buy зарегистрирован в общем «магазинном» диспетчере → работает в клонах.
        rows.append([InlineKeyboardButton(text='🛒 Перейти к тарифам', callback_data='menu_buy')])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _download_media_bytes(main_bot: Bot, file_id: str) -> bytes | None:
    try:
        file = await main_bot.get_file(file_id)
        buffer = await main_bot.download_file(file.file_path)
        return buffer.read() if buffer else None
    except Exception:
        logger.warning('Не удалось скачать медиа рассылки у основного бота', exc_info=True)
        return None


async def run_clone_broadcast(
    broadcast: CloneBroadcast,
    *,
    clone_token: str,
    main_bot: Bot,
    owner_chat_id: int | None = None,
) -> None:
    """Фоновая отправка. Ошибки одной доставки не прерывают рассылку; итог — в БД,
    владельцу (``owner_chat_id``) уходит отчёт основным ботом."""
    broadcast_id = broadcast.id
    clone_bot_id = broadcast.clone_bot_id
    text = broadcast.message_text
    media_type = broadcast.media_type
    kb = build_broadcast_keyboard(broadcast.button_text, broadcast.button_url, broadcast.show_tariffs_button)

    async with AsyncSessionLocal() as db:
        recipients = await get_recipient_telegram_ids(db, clone_bot_id)

    media_bytes: bytes | None = None
    if media_type == 'photo' and broadcast.media_file_id:
        media_bytes = await _download_media_bytes(main_bot, broadcast.media_file_id)
        if media_bytes is None:
            async with AsyncSessionLocal() as db:
                await finish_broadcast(db, broadcast_id, sent=0, failed=0, status='failed')
            if owner_chat_id:
                try:
                    await main_bot.send_message(owner_chat_id, '❌ Рассылка не запустилась: не удалось обработать фото.')
                except Exception:
                    pass
            return

    sent = 0
    failed = 0
    clone_file_id: str | None = None
    bot = create_bot(token=clone_token)
    try:
        for chat_id in recipients:
            for attempt in (1, 2):
                try:
                    if media_type == 'photo':
                        if clone_file_id:
                            msg = await bot.send_photo(chat_id, clone_file_id, caption=text, reply_markup=kb)
                        else:
                            photo = BufferedInputFile(media_bytes, filename='broadcast.jpg')
                            msg = await bot.send_photo(chat_id, photo, caption=text, reply_markup=kb)
                        if msg.photo:
                            clone_file_id = msg.photo[-1].file_id
                    else:
                        await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
                    sent += 1
                    break
                except TelegramRetryAfter as e:
                    if attempt == 2:
                        failed += 1
                        break
                    await asyncio.sleep(e.retry_after + 1)
                except TelegramForbiddenError:
                    failed += 1  # юзер заблокировал клона
                    break
                except Exception:
                    logger.debug('Clone broadcast delivery failed', chat_id=chat_id, exc_info=True)
                    failed += 1
                    break
            await asyncio.sleep(_SEND_DELAY)
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass

    async with AsyncSessionLocal() as db:
        await finish_broadcast(db, broadcast_id, sent=sent, failed=failed, status='completed')

    logger.info('Clone broadcast finished', broadcast_id=broadcast_id, clone_id=clone_bot_id, sent=sent, failed=failed)

    if owner_chat_id:
        try:
            await main_bot.send_message(
                owner_chat_id,
                f'📢 Рассылка завершена.\n\n✅ Доставлено: <b>{sent}</b>\n🚫 Не доставлено: <b>{failed}</b>',
            )
        except Exception:
            pass
