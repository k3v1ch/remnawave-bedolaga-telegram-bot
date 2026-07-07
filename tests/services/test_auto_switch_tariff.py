"""Тесты автосмены тарифа из сохранённой корзины после пополнения баланса.

Покрывают диспетчеризацию `_auto_switch_tariff` по switch_kind (instant/period/daily),
списание баланса, проверку направления (up/down-grade) и гард недостатка средств.
Тяжёлый «хвост» (_finalize_switch: Remnawave/уведомления/очистка) замокан.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.services import subscription_auto_purchase_service as svc


def _make_db():
    db = MagicMock(spec=AsyncSession)
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _make_user():
    user = MagicMock(spec=User)
    user.id = 1
    user.telegram_id = 11
    user.balance_kopeks = 500_000
    user.language = 'ru'
    user.promo_offer_discount_percent = 0
    user.remnawave_uuid = 'uuid-1'
    return user


def _make_tariffs(is_daily=False):
    new_tariff = MagicMock()
    new_tariff.id = 2
    new_tariff.is_active = True
    new_tariff.is_daily = is_daily
    new_tariff.name = 'Pro'
    new_tariff.traffic_limit_gb = 0
    new_tariff.device_limit = 3
    new_tariff.max_device_limit = None
    new_tariff.allowed_squads = ['squad-a']
    cur_tariff = MagicMock()
    cur_tariff.id = 1
    cur_tariff.device_limit = 1
    return new_tariff, cur_tariff


def _make_sub():
    sub = MagicMock()
    sub.id = 7
    sub.tariff_id = 1
    sub.device_limit = 1
    sub.end_date = datetime.now(UTC) + timedelta(days=10)
    return sub


def _patch_common(monkeypatch, *, new_tariff, cur_tariff, sub, user):
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_UPGRADE_ENABLED', True)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_DOWNGRADE_ENABLED', True)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_TARIFF_SWITCH', True)

    async def _get_tariff(db, tid):
        return new_tariff if tid == new_tariff.id else cur_tariff

    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(side_effect=_get_tariff))
    monkeypatch.setattr('app.database.crud.subscription.get_subscription_by_id_for_user', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.database.crud.subscription.get_subscription_by_user_id', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.server_squad.get_all_server_squads', AsyncMock(return_value=([], 0)))
    monkeypatch.setattr('app.database.crud.subscription.calc_device_limit_on_tariff_switch', MagicMock(return_value=3))

    finalize = AsyncMock()
    monkeypatch.setattr(svc, '_finalize_switch', finalize)
    return finalize


def _switch_result(*, upgrade_cost, is_upgrade):
    res = MagicMock()
    res.upgrade_cost = upgrade_cost
    res.is_upgrade = is_upgrade
    res.offer_discount_pct = 0
    return res


async def test_instant_upgrade_success(monkeypatch):
    user, db = _make_user(), _make_db()
    new_tariff, cur_tariff = _make_tariffs()
    sub = _make_sub()
    finalize = _patch_common(monkeypatch, new_tariff=new_tariff, cur_tariff=cur_tariff, sub=sub, user=user)

    monkeypatch.setattr(
        svc.pricing_engine,
        'calculate_tariff_switch_cost',
        MagicMock(return_value=_switch_result(upgrade_cost=100_000, is_upgrade=True)),
    )
    subtract = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, 'subtract_user_balance', subtract)

    cart = {'cart_mode': 'tariff_switch', 'switch_kind': 'instant', 'tariff_id': 2, 'subscription_id': 7}
    ok = await svc._auto_switch_tariff(db, user, cart, bot=None)

    assert ok is True
    subtract.assert_awaited_once()
    assert subtract.await_args.args[2] == 100_000  # списали ровно доплату
    finalize.assert_awaited_once()
    assert sub.tariff_id == new_tariff.id


async def test_instant_upgrade_insufficient_balance_skips(monkeypatch):
    user, db = _make_user(), _make_db()
    user.balance_kopeks = 50_000  # меньше доплаты
    new_tariff, cur_tariff = _make_tariffs()
    sub = _make_sub()
    finalize = _patch_common(monkeypatch, new_tariff=new_tariff, cur_tariff=cur_tariff, sub=sub, user=user)

    monkeypatch.setattr(
        svc.pricing_engine,
        'calculate_tariff_switch_cost',
        MagicMock(return_value=_switch_result(upgrade_cost=100_000, is_upgrade=True)),
    )
    subtract = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, 'subtract_user_balance', subtract)

    cart = {'cart_mode': 'tariff_switch', 'switch_kind': 'instant', 'tariff_id': 2, 'subscription_id': 7}
    ok = await svc._auto_switch_tariff(db, user, cart, bot=None)

    assert ok is False
    subtract.assert_not_awaited()
    finalize.assert_not_awaited()


async def test_upgrade_disabled_skips(monkeypatch):
    user, db = _make_user(), _make_db()
    new_tariff, cur_tariff = _make_tariffs()
    sub = _make_sub()
    finalize = _patch_common(monkeypatch, new_tariff=new_tariff, cur_tariff=cur_tariff, sub=sub, user=user)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_UPGRADE_ENABLED', False)

    monkeypatch.setattr(
        svc.pricing_engine,
        'calculate_tariff_switch_cost',
        MagicMock(return_value=_switch_result(upgrade_cost=100_000, is_upgrade=True)),
    )
    subtract = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, 'subtract_user_balance', subtract)

    cart = {'cart_mode': 'tariff_switch', 'switch_kind': 'instant', 'tariff_id': 2, 'subscription_id': 7}
    ok = await svc._auto_switch_tariff(db, user, cart, bot=None)

    assert ok is False
    subtract.assert_not_awaited()
    finalize.assert_not_awaited()


async def test_period_switch_success(monkeypatch):
    user, db = _make_user(), _make_db()
    new_tariff, cur_tariff = _make_tariffs()
    new_tariff.period_prices = {'30': 100_000}
    sub = _make_sub()
    finalize = _patch_common(monkeypatch, new_tariff=new_tariff, cur_tariff=cur_tariff, sub=sub, user=user)

    monkeypatch.setattr(
        svc.pricing_engine,
        'calculate_tariff_switch_cost',
        MagicMock(return_value=_switch_result(upgrade_cost=0, is_upgrade=True)),
    )
    price_result = MagicMock()
    price_result.final_total = 150_000
    price_result.promo_offer_discount = 0
    monkeypatch.setattr(
        svc.pricing_engine, 'calculate_tariff_purchase_price', AsyncMock(return_value=price_result)
    )
    subtract = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, 'subtract_user_balance', subtract)
    monkeypatch.setattr(svc, 'extend_subscription', AsyncMock(return_value=sub))

    cart = {
        'cart_mode': 'tariff_switch',
        'switch_kind': 'period',
        'tariff_id': 2,
        'period_days': 30,
        'subscription_id': 7,
    }
    ok = await svc._auto_switch_tariff(db, user, cart, bot=None)

    assert ok is True
    subtract.assert_awaited_once()
    assert subtract.await_args.args[2] == 150_000
    svc.extend_subscription.assert_awaited_once()
    finalize.assert_awaited_once()


async def test_daily_switch_success(monkeypatch):
    user, db = _make_user(), _make_db()
    new_tariff, cur_tariff = _make_tariffs(is_daily=True)
    sub = _make_sub()
    finalize = _patch_common(monkeypatch, new_tariff=new_tariff, cur_tariff=cur_tariff, sub=sub, user=user)

    monkeypatch.setattr(
        svc.pricing_engine,
        'calculate_tariff_switch_cost',
        MagicMock(return_value=_switch_result(upgrade_cost=0, is_upgrade=False)),
    )
    daily_result = MagicMock()
    daily_result.final_total = 5_000
    daily_result.breakdown = {'offer_discount_pct': 0}
    monkeypatch.setattr(
        svc.pricing_engine, 'calculate_tariff_purchase_price', AsyncMock(return_value=daily_result)
    )
    subtract = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, 'subtract_user_balance', subtract)

    cart = {'cart_mode': 'tariff_switch', 'switch_kind': 'daily', 'tariff_id': 2, 'subscription_id': 7}
    ok = await svc._auto_switch_tariff(db, user, cart, bot=None)

    assert ok is True
    subtract.assert_awaited_once()
    assert subtract.await_args.args[2] == 5_000
    finalize.assert_awaited_once()
    assert sub.tariff_id == new_tariff.id
