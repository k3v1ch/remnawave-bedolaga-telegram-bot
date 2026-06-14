"""KELDARI-UI: подарки в боте — покупка подарочной подписки за баланс + ссылка.

Переиспользует GuestPurchase-конвейер Бедолаги: `create_purchase` (is_gift, code-only,
без получателя) + оплата с баланса (`subtract_user_balance` + транзакция GIFT_PAYMENT) +
статус PAID. Подарок остаётся в PAID до клейма — Remnawave-юзер создаётся ТОЛЬКО при
активации получателем (`start.py` deep-link `GIFT_<token>` / `activate_purchase`),
который сам продлевает/заменяет/создаёт подписку у получателя. Без промокодов.

Срок подарка — вечный до активации (как в кабинете). Провижн отложен (на активации).
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from urllib.parse import quote

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.tariff import get_all_tariffs, get_tariff_by_id
from app.database.crud.transaction import create_transaction, emit_transaction_side_effects
from app.database.crud.user import subtract_user_balance
from app.database.models import GuestPurchase, GuestPurchaseStatus, PaymentMethod, TransactionType, User
from app.localization.texts import get_texts
from app.services.guest_purchase_service import GuestPurchaseError, create_purchase
from app.services.user_cart_service import user_cart_service
from app.utils.formatting import format_price_kopeks
from app.utils.photo_message import edit_or_answer_photo


logger = structlog.get_logger(__name__)


KELDARI_GIFT_STATUS_LABELS = {
    GuestPurchaseStatus.PENDING.value: '⏳ ожидает оплаты',
    GuestPurchaseStatus.PAID.value: '🎁 ждёт активации',
    GuestPurchaseStatus.PENDING_ACTIVATION.value: '🎁 ждёт активации',
    GuestPurchaseStatus.DELIVERED.value: '✅ активирован',
    GuestPurchaseStatus.FAILED.value: '❌ ошибка',
    GuestPurchaseStatus.EXPIRED.value: '⌛ истёк',
}
_CLAIMABLE = (GuestPurchaseStatus.PAID.value, GuestPurchaseStatus.PENDING_ACTIVATION.value)


# Telegram ограничивает start-параметр deep-link'а 64 символами. Токен подарка —
# 64 символа (token_urlsafe(48)), а с префиксом «GIFT_» это 69 > 64, и Telegram
# МОЛЧА отбрасывает параметр (бот открывается без него → подарок не активируется).
# Поэтому в ссылку кладём ПРЕФИКС токена; start.py и публичный site-claim ищут
# подарок по startswith. 32 символа base64 ≈ 192 бита — коллизии/перебор невозможны,
# «GIFT_»+32 = 37 ≤ 64. Та же длина используется в кабинете
# (cabinet/routes/gift.py::_GIFT_SHARE_TOKEN_LEN) — ссылки единообразны.
_GIFT_LINK_TOKEN_LEN = 32


def _gift_link(token: str) -> str:
    """Telegram deep-link для активации подарка ботом."""
    username = settings.get_bot_username() or 'bot'
    return f'https://t.me/{username}?start=GIFT_{token[:_GIFT_LINK_TOKEN_LEN]}'


def _gift_site_link(token: str) -> str | None:
    """Ссылка на страницу активации на сайте (/buy/gift/<префикс>). None, если
    CABINET_URL не настроен."""
    base = (getattr(settings, 'CABINET_URL', '') or '').rstrip('/')
    if not base:
        return None
    return f'{base}/buy/gift/{token[:_GIFT_LINK_TOKEN_LEN]}'


def _share_url(link: str) -> str:
    text = quote('Дарю тебе подписку ВЕРНО VPN 🎁')
    return f'https://t.me/share/url?url={quote(link, safe="")}&text={text}'


def _tariff_periods(tariff) -> list[tuple[int, int]]:
    """[(days, price_kopeks), ...] из tariff.period_prices, без суточных."""
    prices = getattr(tariff, 'period_prices', None) or {}
    out: list[tuple[int, int]] = []
    for key, value in prices.items():
        try:
            days = int(key)
            price = int(value)
        except (TypeError, ValueError):
            continue
        if days > 0 and price > 0:
            out.append((days, price))
    return sorted(out)


async def _giftable_tariffs(db: AsyncSession):
    tariffs = await get_all_tariffs(db)
    return [
        t
        for t in tariffs
        if getattr(t, 'show_in_gift', True) and not getattr(t, 'is_daily', False) and _tariff_periods(t)
    ]


def _back_row(callback_data: str, text: str = '‹ Назад') -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=text, callback_data=callback_data)]


# ─────────────────────────────────────────────────────────────
# Экран «Подарки» (вход) — список своих + CTA создать
# ─────────────────────────────────────────────────────────────

async def show_gift_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    result = await db.execute(
        select(GuestPurchase)
        .options(selectinload(GuestPurchase.tariff))
        .where(GuestPurchase.buyer_user_id == db_user.id, GuestPurchase.is_gift.is_(True))
        .order_by(desc(GuestPurchase.created_at))
        .limit(10)
    )
    gifts = result.scalars().all()

    lines = [
        '🎁 <b>Подарки</b>',
        '',
        'Подарите подписку другу — он активирует её одной ссылкой, без промокодов.',
    ]
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=texts.t('KELDARI_GIFT_CREATE_BUTTON', '🎁 Подарить подписку'), callback_data='kgift_create', style='primary')]
    ]
    # KELDARI-UI: подарки-карточки (все статусы). Клик по подарку → экран деталей
    # (show_gift_card): тариф/срок/цена/когда куплен + кто и когда активировал;
    # для неактивированных — ссылки и кнопки шаринга.
    if gifts:
        lines.append('')
        lines.append('<b>Подарки:</b>')
    for gift in gifts:
        tname = gift.tariff.name if gift.tariff else '—'
        if gift.status in _CLAIMABLE:
            emoji = '🎁'
        elif gift.status == GuestPurchaseStatus.DELIVERED.value:
            emoji = '✅'
        else:
            emoji = '•'
        rows.append(
            [InlineKeyboardButton(text=f'{emoji} {tname} · {gift.period_days} дн', callback_data=f'kgift_card:{gift.id}')]
        )

    rows.append(_back_row('menu_subscription'))
    await edit_or_answer_photo(callback=callback, caption='\n'.join(lines), keyboard=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode='HTML')
    await callback.answer()


# ─────────────────────────────────────────────────────────────
# Создание: тариф → период → подтверждение → выполнение
# ─────────────────────────────────────────────────────────────

async def start_create(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    tariffs = await _giftable_tariffs(db)
    rows: list[list[InlineKeyboardButton]] = []
    for tariff in tariffs:
        devices = getattr(tariff, 'device_limit', 0) or 0
        rows.append([InlineKeyboardButton(text=f'{tariff.name} · до {devices} устр.', callback_data=f'kgift_tariff:{tariff.id}')])
    rows.append(_back_row('keldari_gift'))
    text = '🎁 <b>Подарить подписку</b>\n\nВыберите тариф:' if tariffs else '🎁 Подарить подписку\n\nСейчас нет тарифов, доступных для подарка.'
    await edit_or_answer_photo(callback=callback, caption=text, keyboard=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode='HTML')
    await callback.answer()


async def choose_period(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    tariff_id = int((callback.data or '').split(':')[1])
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    rows: list[list[InlineKeyboardButton]] = []
    for days, price in _tariff_periods(tariff):
        rows.append([InlineKeyboardButton(text=f'{days} дн — {format_price_kopeks(price)}', callback_data=f'kgift_period:{tariff_id}:{days}')])
    rows.append(_back_row('kgift_create'))
    await edit_or_answer_photo(
        callback=callback,
        caption=f'🎁 <b>Тариф: {html.escape(tariff.name)}</b>\n\nВыберите срок подарка:',
        keyboard=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode='HTML',
    )
    await callback.answer()


def _price_for(tariff, days: int) -> int | None:
    return dict(_tariff_periods(tariff)).get(days)


async def confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    _, tariff_id_s, days_s = (callback.data or '').split(':')
    tariff_id, days = int(tariff_id_s), int(days_s)
    tariff = await get_tariff_by_id(db, tariff_id)
    price = _price_for(tariff, days) if tariff else None
    if not tariff or not price:
        await callback.answer('Цена не найдена', show_alert=True)
        return
    balance = db_user.balance_kopeks or 0
    lines = [
        '🎁 <b>Подтверждение подарка</b>',
        '',
        f'Тариф: {html.escape(tariff.name)}',
        f'Срок: {days} дн',
        f'Цена: <b>{format_price_kopeks(price)}</b>',
        f'Ваш баланс: {format_price_kopeks(balance)}',
    ]
    rows: list[list[InlineKeyboardButton]] = []
    if balance >= price:
        rows.append([InlineKeyboardButton(text=f'🎁 Подарить за {format_price_kopeks(price)}', callback_data=f'kgift_confirm:{tariff_id}:{days}', style='success')])
    else:
        deficit = price - balance
        # KELDARI-UI: сохраняем «корзину-подарок» → после пополнения дефицита подарок
        # создастся автоматически (хук auto_purchase_saved_cart_after_topup → keldari_gift).
        await user_cart_service.save_user_cart(
            db_user.id,
            {'type': 'keldari_gift', 'tariff_id': tariff_id, 'period_days': days, 'total_price': price, 'return_to_cart': True},
            ttl=3600,
        )
        lines.append('')
        lines.append(f'⚠️ Не хватает <b>{format_price_kopeks(deficit)}</b>.')
        lines.append('Пополните — и подарок оформится автоматически.')
        rows.append([InlineKeyboardButton(text=f'💳 Пополнить {format_price_kopeks(deficit)} и подарить', callback_data=f'kbal_topup:{deficit}', style='primary')])
    rows.append(_back_row(f'kgift_tariff:{tariff_id}'))
    await edit_or_answer_photo(callback=callback, caption='\n'.join(lines), keyboard=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode='HTML')
    await callback.answer()


async def _create_gift_from_balance(db: AsyncSession, user: User, tariff, period_days: int, price_kopeks: int) -> GuestPurchase | None:
    """Создаёт code-only подарок и списывает баланс (зеркало cabinet/routes/gift.py).

    Подарок остаётся в статусе PAID — провижн в Remnawave произойдёт при активации.
    """
    description = f'Подарок: {tariff.name} ({period_days}д)'
    try:
        purchase = await create_purchase(
            db,
            landing=None,
            tariff=tariff,
            period_days=period_days,
            amount_kopeks=price_kopeks,
            contact_type='telegram',
            contact_value=str(user.telegram_id or user.id),
            payment_method='balance',
            is_gift=True,
            source='cabinet',
            buyer_user_id=user.id,
            commit=False,
        )
        balance_ok = await subtract_user_balance(
            db, user, price_kopeks, description=description, create_transaction=False, commit=False
        )
        if not balance_ok:
            await db.rollback()
            return None
        transaction = await create_transaction(
            db,
            user_id=user.id,
            type=TransactionType.GIFT_PAYMENT,
            amount_kopeks=price_kopeks,
            description=description,
            payment_method=PaymentMethod.BALANCE,
            commit=False,
        )
        purchase.status = GuestPurchaseStatus.PAID.value
        purchase.paid_at = datetime.now(UTC)
        await db.commit()
        try:
            await emit_transaction_side_effects(
                db,
                transaction,
                amount_kopeks=price_kopeks,
                user_id=user.id,
                type=TransactionType.GIFT_PAYMENT,
                payment_method=PaymentMethod.BALANCE,
                description=description,
            )
        except Exception as side_error:
            logger.warning('keldari_gift: side-effects не выполнены', error=str(side_error))
        logger.info('keldari_gift: подарок создан', user_id=user.id, purchase_id=purchase.id, amount_kopeks=price_kopeks, period_days=period_days)
        return purchase
    except GuestPurchaseError as gift_error:
        await db.rollback()
        logger.error('keldari_gift: GuestPurchaseError', error=gift_error.message)
        return None
    except Exception as error:
        await db.rollback()
        logger.error('keldari_gift: ошибка создания подарка', error=str(error))
        return None


def _gift_links_caption(token: str) -> str:
    """Текст-блок с обеими ссылками активации подарка: Telegram + сайт."""
    tg = _gift_link(token)
    site = _gift_site_link(token)
    lines = ['🔗 <b>Telegram:</b>', f'<code>{html.escape(tg)}</code>']
    if site:
        lines += ['', '🌐 <b>Сайт:</b>', f'<code>{html.escape(site)}</code>']
    return '\n'.join(lines)


def _gift_ready_keyboard(token: str) -> InlineKeyboardMarkup:
    """Кнопки шаринга подарка: переслать в Telegram и/или поделиться ссылкой на сайт."""
    tg = _gift_link(token)
    site = _gift_site_link(token)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text='📤 Переслать в Telegram', url=_share_url(tg))]
    ]
    if site:
        rows.append([InlineKeyboardButton(text='🌐 Поделиться ссылкой на сайт', url=_share_url(site))])
    rows.append([InlineKeyboardButton(text='‹ К подаркам', callback_data='keldari_gift')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def execute_gift(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    _, tariff_id_s, days_s = (callback.data or '').split(':')
    tariff_id, days = int(tariff_id_s), int(days_s)
    tariff = await get_tariff_by_id(db, tariff_id)
    price = _price_for(tariff, days) if tariff else None
    if not tariff or not price:
        await callback.answer('Цена не найдена', show_alert=True)
        return
    if (db_user.balance_kopeks or 0) < price:
        await callback.answer('Недостаточно средств на балансе', show_alert=True)
        await confirm(callback, db_user, db)
        return
    purchase = await _create_gift_from_balance(db, db_user, tariff, days, price)
    if not purchase:
        await callback.answer('Не удалось создать подарок. Попробуйте позже.', show_alert=True)
        return
    text = (
        '✅ <b>Подарок готов!</b>\n\n'
        f'Тариф: {html.escape(tariff.name)}\n'
        f'Срок: {days} дн\n\n'
        'Перешлите другу любую из ссылок — он активирует подписку одним кликом, без промокодов:\n\n'
        f'{_gift_links_caption(purchase.token)}'
    )
    await edit_or_answer_photo(callback=callback, caption=text, keyboard=_gift_ready_keyboard(purchase.token), parse_mode='HTML')
    await callback.answer('Готово 🎁')


def _fmt_dt(dt) -> str:
    return dt.strftime('%d.%m.%Y %H:%M') if dt else '—'


async def show_gift_card(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Карточка подарка: тариф/срок/устройства/цена/когда куплен + кто и когда
    активировал. Для НЕактивированных — ссылки активации и кнопки шаринга;
    для активированных копировать токен нельзя (только детали)."""
    gift_id = int((callback.data or '').split(':')[1])
    result = await db.execute(
        select(GuestPurchase)
        .options(selectinload(GuestPurchase.tariff), selectinload(GuestPurchase.user))
        .where(GuestPurchase.id == gift_id, GuestPurchase.buyer_user_id == db_user.id, GuestPurchase.is_gift.is_(True))
    )
    gift = result.scalars().first()
    if not gift:
        await callback.answer('Подарок не найден', show_alert=True)
        return

    tname = gift.tariff.name if gift.tariff else '—'
    devices = getattr(gift.tariff, 'device_limit', 0) or 0
    status_label = KELDARI_GIFT_STATUS_LABELS.get(gift.status, gift.status)
    is_claimable = gift.status in _CLAIMABLE
    is_delivered = gift.status == GuestPurchaseStatus.DELIVERED.value

    lines = [
        f'🎁 <b>{html.escape(tname)} · {gift.period_days} дн</b>',
        '',
        f'Статус: {status_label}',
        f'Устройств: до {devices}' if devices else 'Устройств: ∞',
        f'Стоимость: {format_price_kopeks(gift.amount_kopeks)}',
        f'Куплен: {_fmt_dt(gift.created_at)}',
    ]
    if is_delivered:
        who = f'@{gift.user.username}' if gift.user and gift.user.username else '—'
        lines += [f'Активировал: {who}', f'Когда активирован: {_fmt_dt(gift.delivered_at)}']

    if is_claimable:
        lines += ['', 'Ссылки для активации (перешлите другу любую):', '', _gift_links_caption(gift.token)]
        keyboard = _gift_ready_keyboard(gift.token)
    else:
        # Активированный/иной — копирование недоступно, только назад к списку.
        keyboard = InlineKeyboardMarkup(inline_keyboard=[_back_row('keldari_gift')])

    await edit_or_answer_photo(callback=callback, caption='\n'.join(lines), keyboard=keyboard, parse_mode='HTML')
    await callback.answer()


# ─────────────────────────────────────────────────────────────
# Сквозное «пополни дефицит → продолжи» (общий экран + завершение подарка)
# ─────────────────────────────────────────────────────────────

async def show_topup_for_amount(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Экран оплаты на конкретную сумму (дефицит). После зачисления покупка/подарок
    завершается автоматически (auto_purchase_saved_cart_after_topup). Общий для подарка и подписки."""
    from app.keyboards.inline import get_payment_methods_keyboard

    try:
        kopeks = int((callback.data or '').split(':')[1])
    except (IndexError, ValueError):
        await callback.answer()
        return
    texts = get_texts(db_user.language)
    keyboard = get_payment_methods_keyboard(kopeks, db_user.language)
    rows = [list(row) for row in keyboard.inline_keyboard]
    rows.append([InlineKeyboardButton(text=texts.t('KELDARI_BACK_BUTTON', '‹ Назад'), callback_data='back_to_menu')])
    text = (
        f'💳 <b>Пополнение на {format_price_kopeks(kopeks)}</b>\n\n'
        'Выберите способ оплаты — после зачисления покупка завершится автоматически.'
    )
    await edit_or_answer_photo(callback=callback, caption=text, keyboard=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode='HTML')
    await callback.answer()


async def complete_gift_after_topup(db: AsyncSession, user: User, cart: dict, *, bot=None) -> None:
    """Завершает создание подарка после пополнения (ветка type=='keldari_gift' в
    auto_purchase_saved_cart_after_topup). Если пополнения не хватило — корзина живёт по TTL."""
    price = int(cart.get('total_price') or 0)
    tariff_id = cart.get('tariff_id')
    days = int(cart.get('period_days') or 0)
    if not (price and tariff_id and days):
        await user_cart_service.delete_user_cart(user.id)
        return
    if (user.balance_kopeks or 0) < price:
        logger.info('keldari_gift: пополнения пока не хватает для подарка', user_id=user.id, need=price, have=user.balance_kopeks)
        return
    tariff = await get_tariff_by_id(db, int(tariff_id))
    if not tariff:
        await user_cart_service.delete_user_cart(user.id)
        return
    purchase = await _create_gift_from_balance(db, user, tariff, days, price)
    await user_cart_service.delete_user_cart(user.id)
    try:
        await user_cart_service.clear_topup_intent(user.id)
    except Exception:
        pass
    if not purchase:
        return
    logger.info('keldari_gift: подарок создан после пополнения', user_id=user.id, purchase_id=purchase.id)
    if bot and user.telegram_id:
        text = (
            '✅ <b>Подарок готов!</b>\n\n'
            f'Тариф: {html.escape(tariff.name)}\n'
            f'Срок: {days} дн\n\n'
            'Перешлите другу любую из ссылок — он активирует подписку одним кликом:\n\n'
            f'{_gift_links_caption(purchase.token)}'
        )
        try:
            await bot.send_message(user.telegram_id, text, reply_markup=_gift_ready_keyboard(purchase.token), parse_mode='HTML')
        except Exception as send_error:
            logger.warning('keldari_gift: не удалось отправить ссылку после пополнения', error=str(send_error))


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_gift_menu, F.data == 'keldari_gift')
    dp.callback_query.register(show_topup_for_amount, F.data.startswith('kbal_topup:'))
    dp.callback_query.register(start_create, F.data == 'kgift_create')
    dp.callback_query.register(choose_period, F.data.startswith('kgift_tariff:'))
    dp.callback_query.register(confirm, F.data.startswith('kgift_period:'))
    dp.callback_query.register(execute_gift, F.data.startswith('kgift_confirm:'))
    dp.callback_query.register(show_gift_card, F.data.startswith('kgift_card:'))
