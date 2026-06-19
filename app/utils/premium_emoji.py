"""Премиум (custom) эмодзи для CUSTOM-UI экранов — как в vernovpnbot mock.

Карта ``EMOJI_MAP`` (unicode → custom_emoji_id) перенесена из verno_mock_bot
(§22.7). :func:`build_caption_entities` строит ``MessageEntity[custom_emoji]`` для
caption/текста.

Telegram требует offset/length в UTF-16 code units. Эмодзи + следующий VS16
(U+FE0F) объединяются в одну entity.

Fallback на обычные эмодзи (если премиум недоступен / выключен) делает вызывающий
код: при ошибке Telegram сообщение переотправляется без entities, а сами символы
остаются обычными эмодзи — бот не падает. См. :mod:`app.utils.photo_message`.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import structlog
from aiogram.types import MessageEntity

from app.config import settings


logger = structlog.get_logger(__name__)


# unicode → custom_emoji_id (дословно из verno_mock_bot app/design/tokens.py)
EMOJI_MAP: dict[str, str] = {
    '⚡': '6023761060786346622',
    '✨': '6021425109678429764',
    '🚀': '5258332798409783582',
    '🔥': '5213049150026818687',
    '💎': '6037083366438737901',
    '✅': '5985596818912712352',
    '❌': '5985346521103604145',
    '⚠': '5881702736843511327',
    '🟢': '5339112148175959615',
    '🔴': '5337017423906226569',
    '🟡': '5339082633160703625',
    '⏳': '5807879906951960923',
    '🚫': '5985346521103604145',
    '💰': '6030443364178992166',
    '💸': '6030558512252197022',
    '💵': '6030352469786105758',
    '💳': '6030410254276106984',
    '🤖': '6030400221232501136',
    '📊': '5936143551854285132',
    '📈': '5938539885907415367',
    '👷': '5944844421156577695',
    '👥': '6021690418398239007',
    '🎁': '6023826881160157558',
    '🔗': '6028171274939797252',
    '📌': '5796440171364749940',
    '🎯': '6025879072368761539',
    '📢': '5771695636411847302',
    '📹': '6019542324864882867',
    '🛒': '6030664675253820292',
    '🔐': '6019568309417023812',
    '🔑': '6005570495603282482',
    '🔔': '6021536113108196448',
    '🔕': '6021440013214948027',
    '✉': '5967280668885913944',
    '🗑': '6021413766669801212',
    '🔄': '5877410604225924969',
    '▶': '5807414083388971488',
    '⏸': '5807945302124012062',
    '➕': '5775937998948404844',
    '📝': '6006038041448156880',
    '📋': '6021435576513730578',
    '⚙': '6021637109264160908',
    '📜': '6021454607513819417',
    '📅': '6023880246128810031',
    '💡': '6024093882097080691',
    '👇': '6023566962624306038',
    '📱': '6019199238582311098',
    '💻': '6019168392127190964',
    '📺': '6019110203910265775',
    '👋': '6023985511482268644',
    '🎉': '5994502837327892086',
    '👍': '6023940002008799618',
    '🥺': '5942913498349571809',
    '🥴': '5927054181285237634',
    '👁': '6024008227564296298',
    '👤': '6024039683904772353',
    '🗓': '6023880246128810031',
    '🕘': '6034898821517940846',
    '✍': '5985774024968379294',
    '✏': '5985774024968379294',
    '⛔': '5985346521103604145',
    '📡': '6021486789703769089',
    '💼': '6021650913289050282',
    '📄': '6021454607513819417',
    '⭐': '5895708410447401643',
    '⌨': '6021741116192201252',
}


def _utf16_len(ch: str) -> int:
    return 1 if ord(ch) < 0x10000 else 2


def _utf16_str_len(s: str) -> int:
    return sum(_utf16_len(ch) for ch in s)


def text_has_premium_emoji(text: str) -> bool:
    """Быстрая проверка: есть ли в ``text`` хоть один символ из EMOJI_MAP."""
    return any(ch in EMOJI_MAP for ch in text)


# Непремиум-эмодзи (нет custom_emoji_id) → ближайший из EMOJI_MAP, либо '' (удалить).
# Цель: чтобы у пользователя НЕ оставалось обычных эмодзи рядом с премиумными.
EMOJI_NORMALIZE: dict[str, str] = {
    # --- замена близким премиумным ---
    '✦': '✨',
    '🌐': '📡',
    '🌍': '📡',
    '📶': '📡',
    '🎫': '🎁',
    '🤝': '👥',
    '🧑': '👤',
    '👀': '👁',
    '⏰': '⏳',
    '⌛': '⏳',
    '⏱': '⏳',
    '🕐': '🕘',
    '🕒': '🕘',
    '🔧': '⚙',
    '🛠': '⚙',
    '🔒': '🔐',
    '🔓': '🔐',
    '🛡': '🔐',
    '🛟': '🔐',
    '📦': '🛒',
    '🚚': '🛒',
    '📨': '✉',
    '📤': '✉',
    '📥': '✉',
    '📬': '✉',
    '💬': '✉',
    '♻': '🔄',
    '🔁': '🔄',
    '🏦': '💰',
    '🪙': '💰',
    '🧾': '📄',
    '🗄': '📋',
    '⚖': '📜',
    '❗': '⚠',
    '✖': '❌',
    '🛑': '🚫',
    '🏆': '⭐',
    '🏅': '⭐',
    '🖥': '💻',
    '📆': '📅',
    '🎲': '🎯',
    '🏷': '📌',
    '📍': '📌',
    '🗳': '📊',
    '📉': '📊',
    '🧹': '🗑',
    '🚦': '🟢',
    '📎': '🔗',
    '⛓': '🔗',
    '📷': '📹',
    '🎥': '📹',
    '🧊': '⏸',
    '🥶': '⏸',
    '⏹': '⏸',
    '📣': '📢',
    '📩': '✉',
    '🍎': '📱',
    '🐧': '💻',
    '💱': '💰',
    '🎰': '🎯',
    '🎬': '📹',
    '🎟': '🎁',
    '🚧': '⚙',
    '🚨': '⚠',
    '▸': '▶',
    '⚪': '🟡',
    '🟣': '🟡',
    '🟠': '🟡',
    '🟦': '🟢',
    '⚫': '🔴',
    # --- удаляем (нет близкого премиум-аналога) ---
    '⬅': '',
    '➡': '',
    '⬆': '',
    '⬇': '',
    '↩': '',
    '◀': '',
    '→': '',
    '⏭': '',
    '🔚': '',
    '❓': '',
    '❔': '',
    '♾': '',
    '😔': '',
    '🎭': '',
    '🧪': '',
    '🧩': '',
    '🔍': '',
    '💤': '',
    '➖': '',
    '🏠': '',
    '🖼': '',
    '👆': '',
    '🙏': '',
}

# Множество символов нормализации (для быстрой проверки).
_NORMALIZE_CHARS = frozenset(EMOJI_NORMALIZE)


def text_has_normalizable(text: str) -> bool:
    """Быстрая проверка: есть ли в ``text`` непремиум-эмодзи из EMOJI_NORMALIZE."""
    return any(ch in _NORMALIZE_CHARS for ch in text)


def normalize_emojis(text: str) -> str:
    """Заменяет непремиум-эмодзи на близкие премиумные или удаляет их.

    Удаляемые эмодзи поглощают следующий VS16 и один пробел справа, чтобы не плодить
    двойные пробелы; затем подчищаются хвостовые пробелы в строках.
    """
    if not text:
        return text
    out = text
    removed = False
    for src, dst in EMOJI_NORMALIZE.items():
        if src not in out:
            continue
        if dst:
            out = out.replace(src + '️', dst).replace(src, dst)
        else:
            out = re.sub(re.escape(src) + '️? ?', '', out)
            removed = True
    if removed:
        out = '\n'.join(line.rstrip(' ') for line in out.split('\n'))
    return out


def _custom_emoji_entities(text: str) -> list[MessageEntity]:
    """Строит entities[custom_emoji] для всех эмодзи из EMOJI_MAP в ``text``.

    Offset/length считаются в UTF-16 code units (требование Telegram). Эмодзи и
    следующий за ним VS16 (U+FE0F) объединяются в одну entity.
    """
    entities: list[MessageEntity] = []
    utf16_pos = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        cp_len = _utf16_len(ch)
        next_ch = text[i + 1] if i + 1 < n else ''
        if ch in EMOJI_MAP:
            length = cp_len
            consumed = 1
            if next_ch == '️':  # variation selector-16
                length += _utf16_len(next_ch)
                consumed += 1
            entities.append(
                MessageEntity(
                    type='custom_emoji',
                    offset=utf16_pos,
                    length=length,
                    custom_emoji_id=EMOJI_MAP[ch],
                )
            )
            utf16_pos += length
            i += consumed
        else:
            utf16_pos += cp_len
            i += 1
    return entities


def build_caption_entities(text: str) -> list[MessageEntity] | None:
    """Строит entities[custom_emoji] для всех эмодзи из EMOJI_MAP в ``text``.

    Возвращает ``None``, если премиум-эмодзи выключены глобально или совпадений нет
    (тогда вызывающий код шлёт как обычно, без entities).
    """
    if not getattr(settings, 'USE_PREMIUM_EMOJI', True):
        return None

    entities = _custom_emoji_entities(text)
    # Лимит Telegram — 100 entities на сообщение; наши экраны далеко ниже.
    if not entities or len(entities) > 100:
        return None
    return entities


# Telegram-HTML теги → тип MessageEntity. Набор — как в _ALLOWED_TAGS
# (app/utils/markdown_to_telegram.py), который и порождает эти теги.
_TAG_TO_ENTITY: dict[str, str] = {
    'b': 'bold',
    'strong': 'bold',
    'i': 'italic',
    'em': 'italic',
    'u': 'underline',
    'ins': 'underline',
    's': 'strikethrough',
    'strike': 'strikethrough',
    'del': 'strikethrough',
    'code': 'code',
    'pre': 'pre',
    'a': 'text_link',
    'blockquote': 'blockquote',
    'tg-spoiler': 'spoiler',
    'tg-emoji': 'custom_emoji',
}


class _TelegramHTMLParser(HTMLParser):
    """Парсит Telegram-HTML в (plain_text, entities), смещения в UTF-16.

    ``convert_charrefs=True`` (дефолт) → ``&amp;``/``&lt;`` декодируются в ``handle_data``,
    отдельная обработка charref не нужна. Неизвестные теги игнорируются, их текст
    сохраняется.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.pos = 0  # позиция в UTF-16 code units
        self.entities: list[MessageEntity] = []
        # стек открытых тегов: (tag, start_pos, attrs_dict)
        self._stack: list[tuple[str, int, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_d = {k.lower(): (v or '') for k, v in attrs}
        # <span class="tg-spoiler"> → spoiler
        if tag == 'span' and 'tg-spoiler' in attr_d.get('class', ''):
            tag = 'tg-spoiler'
        self._stack.append((tag, self.pos, attr_d))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # самозакрывающийся тег (напр. <br/>) — без содержимого, entity не создаём
        if tag.lower() == 'br':
            self.handle_data('\n')

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == 'span':
            tag = 'tg-spoiler'
        # ищем ближайший подходящий открытый тег
        for idx in range(len(self._stack) - 1, -1, -1):
            open_tag, start, attr_d = self._stack[idx]
            if open_tag == tag:
                del self._stack[idx]
                self._emit_entity(open_tag, start, attr_d)
                return
        # закрывающий без открывающего — игнорируем

    def _emit_entity(self, tag: str, start: int, attr_d: dict[str, str]) -> None:
        entity_type = _TAG_TO_ENTITY.get(tag)
        length = self.pos - start
        if entity_type is None or length <= 0:
            return
        kwargs: dict[str, object] = {'type': entity_type, 'offset': start, 'length': length}
        if entity_type == 'text_link':
            href = attr_d.get('href', '')
            if not href:
                return
            kwargs['url'] = href
        elif entity_type == 'custom_emoji':
            emoji_id = attr_d.get('emoji-id') or attr_d.get('data-emoji-id', '')
            if not emoji_id:
                return
            kwargs['custom_emoji_id'] = emoji_id
        elif entity_type == 'pre':
            language = attr_d.get('language', '')
            if language:
                kwargs['language'] = language
        self.entities.append(MessageEntity(**kwargs))

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
        self.pos += _utf16_str_len(data)

    def result(self) -> tuple[str, list[MessageEntity]]:
        return ''.join(self.parts), self.entities


def html_to_entities(html: str) -> tuple[str, list[MessageEntity]]:
    """Переводит Telegram-HTML в ``(plain_text, entities)``.

    Поддерживает теги из :data:`_TAG_TO_ENTITY`. Смещения в UTF-16 code units.
    """
    parser = _TelegramHTMLParser()
    parser.feed(html)
    parser.close()
    return parser.result()


def combine_entities(
    text: str, parse_mode: str | None
) -> tuple[str, list[MessageEntity]] | None:
    """Готовит ``(text, entities)`` так, чтобы эмодзи стали премиум, а форматирование —
    сохранилось.

    Telegram игнорирует ``parse_mode`` при наличии ``entities``, поэтому при HTML мы сами
    парсим разметку в entities и добавляем custom_emoji.

    Возвращает ``None`` (не трогать сообщение), если: премиум выключен, нет совпадений по
    EMOJI_MAP, ``parse_mode`` не HTML/None, или при любой ошибке парсинга — вызывающий код
    тогда шлёт исходный текст как раньше, обычными эмодзи (бот не падает).
    """
    if not getattr(settings, 'USE_PREMIUM_EMOJI', True):
        return None
    if not text:
        return None
    # Срабатываем, если есть что нормализовать ИЛИ что превратить в премиум.
    if not text_has_premium_emoji(text) and not text_has_normalizable(text):
        return None

    mode = (parse_mode or '').upper()
    # Markdown/MarkdownV2 и прочее не парсим — оставляем как есть.
    if mode not in ('', 'HTML'):
        return None

    try:
        normalized = normalize_emojis(text)
        changed = normalized != text
        if mode == 'HTML':
            plain, fmt_entities = html_to_entities(normalized)
        else:
            plain, fmt_entities = normalized, []
        emoji_entities = _custom_emoji_entities(plain)
        # Если эмодзи в премиум не превращаются и текст не менялся — не трогаем.
        if not emoji_entities and not changed:
            return None
        entities = fmt_entities + emoji_entities
        # Лимит Telegram — 100 entities; при превышении сохраняем форматирование,
        # а нормализованный текст всё равно отдаём.
        if len(entities) > 100:
            entities = fmt_entities[:100]
        entities.sort(key=lambda e: e.offset)
        return plain, entities
    except Exception as error:  # noqa: BLE001 — fallback важнее, бот не должен падать
        logger.debug('combine_entities: ошибка нормализации/парсинга, fallback на обычный текст', error=error)
        return None
