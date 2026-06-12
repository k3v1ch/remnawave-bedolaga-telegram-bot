"""KELDARI-UI: статичные инфо-экраны онбординга по эталону ВЕРНО VPN.

Callbacks:
- ``keldari_tariffs_info`` — SCR-TARIFFS-INFO (цифры тарифов подтягиваются
  динамически из БД-тарифов; при ошибке/пустом списке — общий текст без цифр);
- ``keldari_how_it_works`` — SCR-HOW-IT-WORKS (статичный).

Клавиатура: CTA ([Попробовать бесплатно] / [Выбрать тариф]) + [‹ Назад] → back_to_menu.
"""

from __future__ import annotations

import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keldari.menu_text import is_trial_available
from app.localization.texts import get_texts
from app.utils.formatting import format_price_kopeks
from app.utils.photo_message import edit_or_answer_photo


logger = structlog.get_logger(__name__)


KELDARI_HOW_IT_WORKS_DEFAULT = (
    '💡 Всё просто:\n'
    '\n'
    '1. Активируете бесплатный период\n'
    '2. Получаете доступ на 3 дня — без карты, без обязательств\n'
    '3. Понравилось? Продлеваете подписку вручную\n'
    '\n'
    'Мы гарантируем:\n'
    '✦ Высокую скорость\n'
    '✦ Безопасность личных данных\n'
    '✦ Безлимитный трафик\n'
    '✦ Поддержку 24/7'
)

KELDARI_TARIFFS_INFO_HEADER_DEFAULT = (
    '⚡️ У нас есть несколько тарифов — под разные задачи и количество устройств.'
)
KELDARI_TARIFFS_INFO_LINE_DEFAULT = '{name} | до {devices} устройств | от {price}'
KELDARI_TARIFFS_INFO_FOOTER_DEFAULT = 'Чем выше тариф, тем больше устройств и тем выгоднее стоимость.'
KELDARI_TARIFFS_INFO_FALLBACK_DEFAULT = (
    '⚡️ У нас есть несколько тарифов — под разные задачи и количество устройств.\n'
    '\n'
    'Чем выше тариф, тем больше устройств и тем выгоднее стоимость.'
)


def _get_info_keyboard(texts, db_user: User) -> InlineKeyboardMarkup:
    """CTA + [‹ Назад] — аналог _kb_info_common эталона."""
    if is_trial_available(db_user):
        cta = InlineKeyboardButton(
            text=texts.t('KELDARI_MAIN_MENU_TRIAL_BUTTON', 'Попробовать бесплатно'),
            callback_data='trial_activate',
            style='primary',
        )
    else:
        cta = InlineKeyboardButton(
            text=texts.t('KELDARI_MAIN_MENU_CHOOSE_TARIFF_BUTTON', 'Выбрать тариф'),
            callback_data='tariff_list',
            style='primary',
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [cta],
            [InlineKeyboardButton(text=texts.t('KELDARI_BACK_BUTTON', '‹ Назад'), callback_data='back_to_menu')],
        ]
    )


async def _build_tariffs_info_text(db: AsyncSession, texts) -> str:
    """SCR-TARIFFS-INFO с динамическими цифрами из БД-тарифов."""
    try:
        from app.database.crud.tariff import get_all_tariffs

        tariffs = await get_all_tariffs(db)
    except Exception as error:
        logger.warning('KELDARI-UI: не удалось загрузить тарифы для инфо-экрана', error=error)
        tariffs = []

    lines: list[str] = []
    line_template = texts.t('KELDARI_TARIFFS_INFO_LINE', KELDARI_TARIFFS_INFO_LINE_DEFAULT)

    for tariff in tariffs:
        if getattr(tariff, 'is_daily', False):
            daily_price = getattr(tariff, 'daily_price_kopeks', 0) or 0
            if daily_price <= 0:
                continue
            price = f'{format_price_kopeks(daily_price, compact=True)}/день'
        else:
            prices = getattr(tariff, 'period_prices', None) or {}
            if not prices:
                continue
            min_price = prices[min(prices.keys(), key=int)]
            price = format_price_kopeks(min_price, compact=True)

        try:
            lines.append(
                line_template.format(
                    name=html.escape(tariff.name or ''),
                    devices=getattr(tariff, 'device_limit', 0) or 0,
                    price=price,
                )
            )
        except Exception as format_error:
            logger.debug('KELDARI-UI: ошибка форматирования строки тарифа', error=format_error)

    if not lines:
        return texts.t('KELDARI_TARIFFS_INFO_FALLBACK', KELDARI_TARIFFS_INFO_FALLBACK_DEFAULT)

    header = texts.t('KELDARI_TARIFFS_INFO_HEADER', KELDARI_TARIFFS_INFO_HEADER_DEFAULT)
    footer = texts.t('KELDARI_TARIFFS_INFO_FOOTER', KELDARI_TARIFFS_INFO_FOOTER_DEFAULT)
    return '\n\n'.join([header, '\n'.join(lines), footer])


async def show_tariffs_info(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    text = await _build_tariffs_info_text(db, texts)
    await edit_or_answer_photo(
        callback=callback,
        caption=text,
        keyboard=_get_info_keyboard(texts, db_user),
        parse_mode='HTML',
    )
    await callback.answer()


async def show_how_it_works(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    await edit_or_answer_photo(
        callback=callback,
        caption=texts.t('KELDARI_HOW_IT_WORKS', KELDARI_HOW_IT_WORKS_DEFAULT),
        keyboard=_get_info_keyboard(texts, db_user),
        parse_mode='HTML',
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_tariffs_info, F.data == 'keldari_tariffs_info')
    dp.callback_query.register(show_how_it_works, F.data == 'keldari_how_it_works')
