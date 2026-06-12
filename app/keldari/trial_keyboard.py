"""KELDARI-UI: пост-обработка клавиатуры экрана SCR-TRIAL-ACTIVATED.

Эталон (kb_trial_activated): `[Подключиться](primary) [Инструкция](url)` в один
ряд + `[‹ Главное меню]`. Бедолага строит клавиатуру подключения в 5 вариантах
(режимы CONNECT_BUTTON_MODE) — вместо дублирования правок в каждом branch'е
применяем стили/кнопку «Инструкция» поверх готовой клавиатуры.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def apply_trial_activated_styling(markup: InlineKeyboardMarkup, texts) -> InlineKeyboardMarkup:
    """Делает первую кнопку (Подключиться) акцентной и добавляет [Инструкция](url)."""
    rows = [list(row) for row in (markup.inline_keyboard or [])]
    if not rows:
        return markup

    rows[0] = [button.model_copy(update={'style': 'primary'}) for button in rows[0]]

    instructions_button = InlineKeyboardButton(
        text=texts.t('KELDARI_MAIN_MENU_INSTRUCTIONS_BUTTON', 'Инструкция'),
        url=texts.t('KELDARI_INSTRUCTIONS_URL', 'https://telegra.ph/verno-vpn-instructions'),
    )
    if len(rows[0]) == 1:
        rows[0].append(instructions_button)
    else:
        rows.insert(1, [instructions_button])

    return InlineKeyboardMarkup(inline_keyboard=rows)
