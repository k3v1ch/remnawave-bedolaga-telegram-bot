"""CUSTOM-UI: пост-обработка клавиатуры экрана SCR-TRIAL-ACTIVATED.

Эталон (kb_trial_activated): `[Подключиться](primary) [Инструкция](url)` в один
ряд + `[‹ Главное меню]`. Бедолага строит клавиатуру подключения в 5 вариантах
(режимы CONNECT_BUTTON_MODE) — вместо дублирования правок в каждом branch'е
применяем стили/кнопку «Инструкция» поверх готовой клавиатуры.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def apply_trial_activated_styling(markup: InlineKeyboardMarkup, texts) -> InlineKeyboardMarkup:
    """Делает первую кнопку (Подключиться) акцентной.

    Кнопку «Инструкция» больше не добавляем: инструкция по настройке уже открывается
    по самой ссылке подписки, отдельная кнопка дублировала её и была бесполезной.
    """
    rows = [list(row) for row in (markup.inline_keyboard or [])]
    if not rows:
        return markup

    rows[0] = [button.model_copy(update={'style': 'primary'}) for button in rows[0]]

    return InlineKeyboardMarkup(inline_keyboard=rows)
