"""Исходящий session-middleware: премиум (custom) эмодзи во ВСЕХ сообщениях бота.

Перехватывает каждый исходящий вызов Bot API и, если в тексте/подписи есть эмодзи из
``EMOJI_MAP``, заменяет их на ``custom_emoji`` entities (с сохранением HTML-форматирования —
см. :func:`app.utils.premium_emoji.combine_entities`). Один хук покрывает меню, уведомления,
рассылки и админку; навешивается в :func:`app.bot_factory.create_bot`, поэтому работает и в
основном боте, и в white-label клонах.

Безопасность: при любой ошибке/неподдерживаемом ``parse_mode`` сообщение уходит как раньше,
обычными эмодзи (бот не падает). Глобальный рубильник — ``settings.USE_PREMIUM_EMOJI``.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from aiogram import Bot
from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.methods import Response, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.utils.premium_emoji import EMOJI_MAP, combine_entities, normalize_emojis


logger = structlog.get_logger(__name__)

# Методы и поля (текст, entities), которые надо обрабатывать.
_TEXT_METHODS: dict[str, tuple[str, str]] = {
    'SendMessage': ('text', 'entities'),
    'EditMessageText': ('text', 'entities'),
}
_CAPTION_METHODS: dict[str, tuple[str, str]] = {
    name: ('caption', 'caption_entities')
    for name in (
        'SendPhoto',
        'SendDocument',
        'SendVideo',
        'SendAnimation',
        'SendAudio',
        'SendVoice',
        'EditMessageCaption',
        'CopyMessage',
    )
}


def _resolve_parse_mode(value: Any, bot: Bot) -> str | None:
    """Default-sentinel → дефолт бота; иначе само значение."""
    if isinstance(value, Default):
        return getattr(bot.default, 'parse_mode', None)
    return value


def _build_update(obj: Any, text_attr: str, entities_attr: str, bot: Bot) -> dict[str, Any] | None:
    """Считает обновление полей ``obj`` под премиум-эмодзи или ``None`` (не трогать).

    Не мутирует ``obj`` — pydantic-модели методов/InputMedia могут быть frozen, поэтому
    применяем через ``model_copy(update=...)`` в вызывающем коде.
    """
    text = getattr(obj, text_attr, None)
    if not isinstance(text, str) or not text:
        return None
    # Уже посчитанные entities (напр. custom_mock) — не трогаем.
    if getattr(obj, entities_attr, None):
        return None
    parse_mode = _resolve_parse_mode(getattr(obj, 'parse_mode', None), bot)
    result = combine_entities(text, parse_mode)
    if result is None:
        return None
    new_text, entities = result
    update: dict[str, Any] = {text_attr: new_text, entities_attr: entities}
    if 'parse_mode' in getattr(type(obj), 'model_fields', {}):
        update['parse_mode'] = None
    return update


def _split_leading_emoji(text: str) -> tuple[str | None, str]:
    """Если текст начинается с эмодзи из EMOJI_MAP — вернуть (эмодзи, остаток без него).

    Поглощает VS16 и пробелы после эмодзи. Иначе — ``(None, text)``.
    """
    stripped = text.lstrip()
    if not stripped:
        return None, text
    ch = stripped[0]
    if ch not in EMOJI_MAP:
        return None, text
    rest = stripped[1:]
    if rest[:1] == '️':  # VS16
        rest = rest[1:]
    return ch, rest.lstrip(' ')


def _strip_mapped_emoji(text: str) -> str:
    """Убирает из текста кнопки все эмодзи из EMOJI_MAP (+ VS16), схлопывая пробелы.

    В тексте inline-кнопки кастомные (премиум) эмодзи невозможны — премиумной может быть
    только ОДНА ведущая иконка через ``icon_custom_emoji_id``. Любой оставшийся mapped-эмодзи
    отрисуется обычным и встанет рядом с премиум-иконкой → визуальный разнобой «премиум +
    обычный» (напр. ``✨ 7 дней за сторис ✨`` или ``⚙️ Управление ▸``). Поэтому их вырезаем.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in EMOJI_MAP:
            i += 1
            if i < n and text[i] == '️':  # VS16
                i += 1
            continue
        out.append(ch)
        i += 1
    return re.sub(r' {2,}', ' ', ''.join(out)).strip()


def _process_button(btn: InlineKeyboardButton) -> InlineKeyboardButton:
    """Премиум-иконка кнопки: ведущий mapped-эмодзи → icon_custom_emoji_id (без дубля в
    тексте); остальные непремиум-эмодзи в тексте нормализуются."""
    text = btn.text or ''
    update: dict[str, Any] = {}

    # 1) Сначала нормализуем непремиум-эмодзи (🔧→⚙ и т.п. или удаление).
    text = normalize_emojis(text)

    # 2) Ведущий премиум-эмодзи → иконка (убираем из текста, чтобы не было дубля).
    if not btn.icon_custom_emoji_id:
        emoji, rest = _split_leading_emoji(text)
        if emoji and rest.strip():
            update['icon_custom_emoji_id'] = EMOJI_MAP[emoji]
            text = rest

    # 3) Если у кнопки есть премиум-иконка (поставили сейчас или была раньше) — вычищаем из
    # текста все ОСТАВШИЕСЯ mapped-эмодзи: в тексте кнопки они станут обычными и дадут разнобой
    # с премиум-иконкой. Не трогаем кнопки вовсе без иконки (одиночный обычный эмодзи без премиума
    # рядом разнобоя не создаёт).
    if update.get('icon_custom_emoji_id') or btn.icon_custom_emoji_id:
        stripped = _strip_mapped_emoji(text)
        if stripped:
            text = stripped

    if not update and text == (btn.text or ''):
        return btn
    # Текст кнопки не должен стать пустым.
    if not text.strip():
        return btn
    update['text'] = text
    return btn.model_copy(update=update)


def _process_inline_markup(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup | None:
    """Возвращает новую разметку с премиум-иконками/нормализацией или ``None``."""
    changed = False
    new_rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        new_row = []
        for btn in row:
            nb = _process_button(btn)
            new_row.append(nb)
            if nb is not btn:
                changed = True
        new_rows.append(new_row)
    if not changed:
        return None
    return markup.model_copy(update={'inline_keyboard': new_rows})


class PremiumEmojiRequestMiddleware(BaseRequestMiddleware):
    """Переписывает исходящие методы Bot API под премиум-эмодзи."""

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        if getattr(settings, 'USE_PREMIUM_EMOJI', True):
            try:
                method = self._rewrite(method, bot)
            except Exception as error:  # noqa: BLE001 — отправка важнее премиум-эмодзи
                logger.debug('PremiumEmoji middleware: пропуск из-за ошибки', error=error)
        return await make_request(bot, method)

    @staticmethod
    def _rewrite(method: TelegramMethod[Any], bot: Bot) -> TelegramMethod[Any]:
        method = PremiumEmojiRequestMiddleware._rewrite_text(method, bot)
        method = PremiumEmojiRequestMiddleware._rewrite_markup(method)
        return method

    @staticmethod
    def _rewrite_text(method: TelegramMethod[Any], bot: Bot) -> TelegramMethod[Any]:
        name = type(method).__name__
        fields = _TEXT_METHODS.get(name) or _CAPTION_METHODS.get(name)
        if fields is not None:
            update = _build_update(method, fields[0], fields[1], bot)
            return method.model_copy(update=update) if update else method
        if name == 'EditMessageMedia':
            media = getattr(method, 'media', None)
            if media is not None:
                update = _build_update(media, 'caption', 'caption_entities', bot)
                if update:
                    new_media = media.model_copy(update=update)
                    return method.model_copy(update={'media': new_media})
        return method

    @staticmethod
    def _rewrite_markup(method: TelegramMethod[Any]) -> TelegramMethod[Any]:
        markup = getattr(method, 'reply_markup', None)
        if not isinstance(markup, InlineKeyboardMarkup):
            return method
        new_markup = _process_inline_markup(markup)
        if new_markup is None:
            return method
        return method.model_copy(update={'reply_markup': new_markup})
