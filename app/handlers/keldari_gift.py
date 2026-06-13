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


def _gift_link(token: str) -> str:
    username = settings.get_bot_username() or 'bot'
    return f'https://t.me/{username}?start=GIFT_{token}'


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
    if gifts:
        lines.append('')
        lines.append('<b>Ваши подарки:</b>')
        for gift in gifts:
            tname = gift.tariff.name if gift.tariff else '—'
            status = KELDARI_GIFT_STATUS_LABELS.get(gift.status, gift.status)
            lines.append(f'• {html.escape(tname)}, {gift.period_days} дн — {status}')
            if gift.status in _CLAIMABLE:
                rows.append(
                    [InlineKeyboardButton(text=f'🔗 Ссылка: {html.escape(tname)} {gift.period_days}дн', callback_data=f'kgift_link:{gift.id}')]
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
        lines.append('')
        lines.append(f'⚠️ Не хватает <b>{format_price_kopeks(deficit)}</b> на балансе.')
        # TODO (след. шаг, общий с Фазой 2): сквозное «пополни дефицит и авто-продолжи».
        rows.append([InlineKeyboardButton(text=f'💳 Пополнить баланс', callback_data='balance_topup')])
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


def _gift_ready_keyboard(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='📤 Отправить другу', url=_share_url(link))],
            [InlineKeyboardButton(text='‹ К подаркам', callback_data='keldari_gift')],
        ]
    )


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
    link = _gift_link(purchase.token)
    text = (
        '✅ <b>Подарок готов!</b>\n\n'
        f'Тариф: {html.escape(tariff.name)}\n'
        f'Срок: {days} дн\n\n'
        'Перешлите другу эту ссылку — он активирует подписку одним кликом, без промокодов:\n'
        f'<code>{html.escape(link)}</code>'
    )
    await edit_or_answer_photo(callback=callback, caption=text, keyboard=_gift_ready_keyboard(link), parse_mode='HTML')
    await callback.answer('Готово 🎁')


async def show_gift_link(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    gift_id = int((callback.data or '').split(':')[1])
    result = await db.execute(
        select(GuestPurchase)
        .options(selectinload(GuestPurchase.tariff))
        .where(GuestPurchase.id == gift_id, GuestPurchase.buyer_user_id == db_user.id, GuestPurchase.is_gift.is_(True))
    )
    gift = result.scalars().first()
    if not gift or gift.status not in _CLAIMABLE:
        await callback.answer('Ссылка недоступна (подарок уже активирован или не найден)', show_alert=True)
        return
    link = _gift_link(gift.token)
    tname = gift.tariff.name if gift.tariff else '—'
    text = (
        f'🎁 <b>{html.escape(tname)} · {gift.period_days} дн</b>\n\n'
        'Ссылка для активации (перешлите другу):\n'
        f'<code>{html.escape(link)}</code>'
    )
    await edit_or_answer_photo(callback=callback, caption=text, keyboard=_gift_ready_keyboard(link), parse_mode='HTML')
    await callback.answer()


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_gift_menu, F.data == 'keldari_gift')
    dp.callback_query.register(start_create, F.data == 'kgift_create')
    dp.callback_query.register(choose_period, F.data.startswith('kgift_tariff:'))
    dp.callback_query.register(confirm, F.data.startswith('kgift_period:'))
    dp.callback_query.register(execute_gift, F.data.startswith('kgift_confirm:'))
    dp.callback_query.register(show_gift_link, F.data.startswith('kgift_link:'))
