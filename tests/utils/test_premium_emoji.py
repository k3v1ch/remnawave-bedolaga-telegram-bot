"""Тесты для премиум-эмодзи: HTML→entities парсер и combine_entities.

Проверяем, что форматирование (b/code/a) сохраняется как entities с верными UTF-16
смещениями, а эмодзи из EMOJI_MAP превращаются в custom_emoji; битый ввод и
неподдерживаемый parse_mode → fallback (None / без изменений)."""

from app.utils import premium_emoji
from app.utils.premium_emoji import (
    EMOJI_MAP,
    combine_entities,
    html_to_entities,
    normalize_emojis,
)


def _by_type(entities, type_):
    return [e for e in entities if e.type == type_]


def test_html_to_entities_bold_offsets():
    plain, ents = html_to_entities('Привет <b>мир</b>!')
    assert plain == 'Привет мир!'
    bold = _by_type(ents, 'bold')
    assert len(bold) == 1
    assert bold[0].offset == len('Привет ')
    assert bold[0].length == len('мир')


def test_html_to_entities_code_and_link():
    plain, ents = html_to_entities('<code>cfg</code> и <a href="https://e.x">тут</a>')
    assert plain == 'cfg и тут'
    assert _by_type(ents, 'code')[0].offset == 0
    link = _by_type(ents, 'text_link')[0]
    assert link.url == 'https://e.x'
    assert link.offset == len('cfg и ')
    assert link.length == len('тут')


def test_html_to_entities_nested_tags():
    plain, ents = html_to_entities('<b>a<i>b</i></b>')
    assert plain == 'ab'
    assert _by_type(ents, 'bold')[0].offset == 0
    assert _by_type(ents, 'bold')[0].length == 2
    italic = _by_type(ents, 'italic')[0]
    assert italic.offset == 1
    assert italic.length == 1


def test_html_to_entities_unescapes_amp():
    plain, ents = html_to_entities('<b>A &amp; B</b>')
    assert plain == 'A & B'
    assert _by_type(ents, 'bold')[0].length == len('A & B')


def test_html_to_entities_emoji_utf16_offset():
    # 🔥 — суррогатная пара (2 UTF-16 units), bold идёт после неё
    plain, ents = html_to_entities('🔥<b>x</b>')
    assert plain == '🔥x'
    assert _by_type(ents, 'bold')[0].offset == 2  # после 🔥 (2 units)


def test_combine_entities_html_plus_emoji():
    fire = next(ch for ch, _id in EMOJI_MAP.items() if ch == '🔥')
    text = f'<b>Жара</b> {fire}'
    result = combine_entities(text, 'HTML')
    assert result is not None
    plain, ents = result
    assert plain == 'Жара 🔥'
    assert _by_type(ents, 'bold')
    custom = _by_type(ents, 'custom_emoji')
    assert custom and custom[0].custom_emoji_id == EMOJI_MAP['🔥']
    # entities отсортированы по offset
    assert [e.offset for e in ents] == sorted(e.offset for e in ents)


def test_combine_entities_plain_mode_no_html():
    result = combine_entities('Огонь 🔥', None)
    assert result is not None
    plain, ents = result
    assert plain == 'Огонь 🔥'
    assert _by_type(ents, 'custom_emoji')


def test_combine_entities_no_premium_emoji_returns_none():
    assert combine_entities('<b>просто текст без премиум-символов</b>', 'HTML') is None


def test_combine_entities_markdown_unsupported():
    assert combine_entities('*жара* 🔥', 'MarkdownV2') is None


def test_combine_entities_disabled_globally(monkeypatch):
    monkeypatch.setattr(premium_emoji.settings, 'USE_PREMIUM_EMOJI', False)
    assert combine_entities('🔥', 'HTML') is None


def test_combine_entities_broken_html_falls_back(monkeypatch):
    # Эмуляция ошибки парсера → должен вернуть None, не пробросить исключение
    def boom(_text):
        raise ValueError('broken')

    monkeypatch.setattr(premium_emoji, 'html_to_entities', boom)
    assert combine_entities('<b>🔥', 'HTML') is None


def test_normalize_replaces_with_mapped():
    # 🔧→⚙, 🔒→🔐 (близкие премиумные)
    assert normalize_emojis('🔧 Настройки 🔒') == '⚙ Настройки 🔐'


def test_normalize_removes_with_space():
    assert normalize_emojis('🏠 Главная') == 'Главная'
    assert normalize_emojis('A → B') == 'A B'
    assert normalize_emojis('Назад ⬅️') == 'Назад'


def test_combine_entities_normalizes_without_premium():
    # текст без премиум-эмодзи, но с удаляемым → должен вернуть очищенный текст
    result = combine_entities('Просто 🏠 текст', None)
    assert result is not None
    plain, ents = result
    assert plain == 'Просто текст'


def test_combine_entities_normalized_becomes_premium():
    # 🔧 нормализуется в ⚙ и становится custom_emoji
    result = combine_entities('<b>X</b> 🔧', 'HTML')
    assert result is not None
    plain, ents = result
    assert plain == 'X ⚙'
    assert any(e.type == 'custom_emoji' for e in ents)
    assert any(e.type == 'bold' for e in ents)


def test_button_leading_emoji_to_icon():
    from app.middlewares.premium_emoji_request import _process_button
    from aiogram.types import InlineKeyboardButton

    nb = _process_button(InlineKeyboardButton(text='💎 Премиум', callback_data='x'))
    assert nb.text == 'Премиум'
    assert nb.icon_custom_emoji_id == EMOJI_MAP['💎']


def test_button_normalize_then_icon():
    from app.middlewares.premium_emoji_request import _process_button
    from aiogram.types import InlineKeyboardButton

    nb = _process_button(InlineKeyboardButton(text='🔧 Настройки', callback_data='x'))
    assert nb.text == 'Настройки'
    assert nb.icon_custom_emoji_id == EMOJI_MAP['⚙']


def test_button_emoji_only_not_emptied():
    from app.middlewares.premium_emoji_request import _process_button
    from aiogram.types import InlineKeyboardButton

    nb = _process_button(InlineKeyboardButton(text='💎', callback_data='x'))
    assert nb.text == '💎'  # не опустошаем текст кнопки


def test_button_trailing_emoji_stripped():
    # ✨ ... ✨: ведущий → премиум-иконка, хвостовой (стал бы обычным рядом) — убираем.
    from app.middlewares.premium_emoji_request import _process_button
    from aiogram.types import InlineKeyboardButton

    nb = _process_button(InlineKeyboardButton(text='✨ 7 дней за сторис ✨', callback_data='x'))
    assert nb.text == '7 дней за сторис'
    assert nb.icon_custom_emoji_id == EMOJI_MAP['✨']


def test_button_normalized_trailing_arrow_stripped():
    # ⚙️ Управление ▸: ▸→▶ (mapped) остался бы обычным рядом с премиум-иконкой ⚙ — убираем.
    from app.middlewares.premium_emoji_request import _process_button
    from aiogram.types import InlineKeyboardButton

    nb = _process_button(InlineKeyboardButton(text='⚙️ Управление ▸', callback_data='x'))
    assert nb.text == 'Управление'
    assert nb.icon_custom_emoji_id == EMOJI_MAP['⚙']


def test_button_no_icon_keeps_lone_emoji():
    # Нет премиум-иконки → одиночный обычный эмодзи разнобоя не создаёт, не трогаем.
    from app.middlewares.premium_emoji_request import _process_button
    from aiogram.types import InlineKeyboardButton

    nb = _process_button(InlineKeyboardButton(text='Назад ✨', callback_data='x'))
    assert nb.text == 'Назад ✨'
    assert not nb.icon_custom_emoji_id
