import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services import referral_service


async def test_commission_accrues_before_minimum_first_topup(monkeypatch):
    user = SimpleNamespace(
        id=1,
        telegram_id=101,
        full_name='Test User',
        referred_by_id=2,
        has_made_first_topup=False,
    )
    referrer = SimpleNamespace(
        id=2,
        telegram_id=202,
        full_name='Referrer',
    )

    db = SimpleNamespace(
        commit=AsyncMock(),
        execute=AsyncMock(),
    )

    get_user_mock = AsyncMock(side_effect=[user, referrer])
    monkeypatch.setattr(referral_service, 'get_user_by_id', get_user_mock)
    add_user_balance_mock = AsyncMock()
    monkeypatch.setattr(referral_service, 'add_user_balance', add_user_balance_mock)
    create_referral_earning_mock = AsyncMock()
    monkeypatch.setattr(referral_service, 'create_referral_earning', create_referral_earning_mock)
    monkeypatch.setattr(referral_service, 'get_user_campaign_id', AsyncMock(return_value=None))

    monkeypatch.setattr(referral_service.settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 20000)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 5000)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 10000)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_COMMISSION_PERCENT', 25)

    topup_amount = 15000

    result = await referral_service.process_referral_topup(db, user.id, topup_amount)

    assert result is True
    assert user.has_made_first_topup is False

    add_user_balance_mock.assert_awaited_once()
    add_call = add_user_balance_mock.await_args
    assert add_call.args[1] is referrer
    assert add_call.args[2] == 3750
    assert 'Комиссия' in add_call.args[3]
    assert add_call.kwargs.get('bot') is None

    create_referral_earning_mock.assert_awaited_once()
    earning_call = create_referral_earning_mock.await_args
    assert earning_call.kwargs['amount_kopeks'] == 3750
    assert earning_call.kwargs['reason'] == 'referral_commission_topup'


async def test_first_topup_inviter_gets_fixed_plus_commission(monkeypatch):
    """Inviter bonus should be fixed bonus + commission, not max(fixed, commission)."""
    user = SimpleNamespace(
        id=1,
        telegram_id=101,
        full_name='Test User',
        referred_by_id=2,
        has_made_first_topup=False,
        is_partner=False,
    )
    referrer = SimpleNamespace(
        id=2,
        telegram_id=202,
        full_name='Referrer',
        email=None,
        is_partner=False,
    )

    db = SimpleNamespace(
        commit=AsyncMock(),
        execute=AsyncMock(),
    )

    get_user_mock = AsyncMock(side_effect=[user, referrer])
    monkeypatch.setattr(referral_service, 'get_user_by_id', get_user_mock)
    add_user_balance_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(referral_service, 'add_user_balance', add_user_balance_mock)
    create_referral_earning_mock = AsyncMock()
    monkeypatch.setattr(referral_service, 'create_referral_earning', create_referral_earning_mock)
    monkeypatch.setattr(referral_service, 'get_commission_payment_count', AsyncMock(return_value=0))
    monkeypatch.setattr(referral_service, 'get_user_campaign_id', AsyncMock(return_value=None))
    monkeypatch.setattr(referral_service, 'get_effective_referral_commission_percent', lambda u: 15)

    monkeypatch.setattr(referral_service.settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 10000)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 5000)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 5000)  # 50 rub
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_COMMISSION_PERCENT', 15)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_WELCOME_MONEY_PERCENT', 0)

    topup_amount = 50000  # 500 rub

    result = await referral_service.process_referral_topup(db, user.id, topup_amount)

    assert result is True
    assert user.has_made_first_topup is True

    # add_user_balance called twice: first for referral's own bonus, then for inviter bonus
    assert add_user_balance_mock.await_count == 2

    # Second call is the inviter bonus: fixed 5000 + commission 15% of 50000 = 7500 → total 12500
    inviter_call = add_user_balance_mock.await_args_list[1]
    expected_commission = int(50000 * 15 / 100)  # 7500
    expected_inviter_bonus = 5000 + expected_commission  # 12500
    assert inviter_call.args[2] == expected_inviter_bonus

    # With old max() logic, this would have been max(5000, 7500) = 7500 — wrong!
    assert expected_inviter_bonus == 12500


async def test_first_topup_welcome_money_bonus_non_withdrawable(monkeypatch):
    """Приветственный бонус деньгами начисляется обоим и НЕ создаёт ReferralEarning."""
    user = SimpleNamespace(
        id=1,
        telegram_id=101,
        full_name='Test User',
        referred_by_id=2,
        has_made_first_topup=False,
        is_partner=False,
    )
    referrer = SimpleNamespace(
        id=2,
        telegram_id=202,
        full_name='Referrer',
        email=None,
        is_partner=False,
    )

    db = SimpleNamespace(
        commit=AsyncMock(),
        execute=AsyncMock(),
    )

    get_user_mock = AsyncMock(side_effect=[user, referrer])
    monkeypatch.setattr(referral_service, 'get_user_by_id', get_user_mock)
    add_user_balance_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(referral_service, 'add_user_balance', add_user_balance_mock)
    create_referral_earning_mock = AsyncMock()
    monkeypatch.setattr(referral_service, 'create_referral_earning', create_referral_earning_mock)
    monkeypatch.setattr(referral_service, 'get_commission_payment_count', AsyncMock(return_value=0))
    monkeypatch.setattr(referral_service, 'get_user_campaign_id', AsyncMock(return_value=None))
    monkeypatch.setattr(referral_service, 'get_effective_referral_commission_percent', lambda u: 0)

    monkeypatch.setattr(referral_service.settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 10000)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 0)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 0)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_COMMISSION_PERCENT', 0)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_WELCOME_MONEY_PERCENT', 15)

    topup_amount = 50000  # 500 rub

    result = await referral_service.process_referral_topup(db, user.id, topup_amount)

    assert result is True
    assert user.has_made_first_topup is True

    # Бонус начислен обоим: другу и пригласившему
    assert add_user_balance_mock.await_count == 2
    expected_bonus = int(50000 * 15 / 100)  # 7500

    friend_call = add_user_balance_mock.await_args_list[0]
    referrer_call = add_user_balance_mock.await_args_list[1]
    assert friend_call.args[1] is user
    assert friend_call.args[2] == expected_bonus
    assert referrer_call.args[1] is referrer
    assert referrer_call.args[2] == expected_bonus

    # Бонус невыводимый — ReferralEarning не создаётся
    create_referral_earning_mock.assert_not_awaited()


async def test_first_topup_welcome_money_skipped_for_partner_chain(monkeypatch):
    """Welcome 15% деньгами не начисляется, если инвитер — одобренный партнёр.

    Партнёр живёт на индивидуальной комиссии + выводе реферального баланса,
    welcome предназначен для обычных рефералов без права на вывод.
    Комиссия партнёру при этом начисляется как обычно.
    """
    user = SimpleNamespace(
        id=1,
        telegram_id=101,
        full_name='Friend',
        referred_by_id=2,
        has_made_first_topup=False,
        is_partner=False,
    )
    referrer = SimpleNamespace(
        id=2,
        telegram_id=202,
        full_name='Partner',
        email=None,
        is_partner=True,
    )

    db = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock())

    monkeypatch.setattr(referral_service, 'get_user_by_id', AsyncMock(side_effect=[user, referrer]))
    add_user_balance_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(referral_service, 'add_user_balance', add_user_balance_mock)
    create_referral_earning_mock = AsyncMock()
    monkeypatch.setattr(referral_service, 'create_referral_earning', create_referral_earning_mock)
    monkeypatch.setattr(referral_service, 'get_commission_payment_count', AsyncMock(return_value=0))
    monkeypatch.setattr(referral_service, 'get_user_campaign_id', AsyncMock(return_value=None))
    monkeypatch.setattr(referral_service, 'get_effective_referral_commission_percent', lambda u: 25)

    monkeypatch.setattr(referral_service.settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 10000)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 0)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 0)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_COMMISSION_PERCENT', 0)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_WELCOME_MONEY_PERCENT', 15)

    topup_amount = 40000  # 400 ₽

    result = await referral_service.process_referral_topup(db, user.id, topup_amount)

    assert result is True
    assert user.has_made_first_topup is True

    # Только одно начисление — комиссия 25% инвитеру; welcome обоим пропущен.
    assert add_user_balance_mock.await_count == 1
    inviter_call = add_user_balance_mock.await_args_list[0]
    assert inviter_call.args[1] is referrer
    assert inviter_call.args[2] == 10000  # 25% от 40000

    # ReferralEarning создаётся именно для комиссии (выводимый бонус), не для welcome.
    create_referral_earning_mock.assert_awaited_once()
    earning_kwargs = create_referral_earning_mock.await_args.kwargs
    assert earning_kwargs['reason'] == 'referral_first_topup'
    assert earning_kwargs['amount_kopeks'] == 10000


async def test_welcome_days_skipped_for_partner_chain(monkeypatch):
    """Welcome 15% днями не начисляется, если инвитер — одобренный партнёр."""
    user = SimpleNamespace(id=1, telegram_id=101, full_name='Friend', referred_by_id=2, is_partner=False)
    referrer = SimpleNamespace(id=2, telegram_id=202, full_name='Partner', remnawave_uuid='ref-uuid', is_partner=True)

    db = SimpleNamespace(execute=AsyncMock())

    monkeypatch.setattr(referral_service, 'get_user_by_id', AsyncMock(side_effect=[user, referrer]))
    create_referral_earning_mock = AsyncMock()
    monkeypatch.setattr(referral_service, 'create_referral_earning', create_referral_earning_mock)

    extend_mock = AsyncMock()
    create_paid_mock = AsyncMock()
    import app.database.crud.subscription as sub_crud

    monkeypatch.setattr(sub_crud, 'extend_subscription', extend_mock)
    monkeypatch.setattr(sub_crud, 'create_paid_subscription', create_paid_mock)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_id', AsyncMock())

    monkeypatch.setattr(referral_service.settings, 'REFERRAL_WELCOME_DAYS_PERCENT', 15)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_WELCOME_DAYS_MIN_PERIOD_DAYS', 7)

    result = await referral_service.process_referral_subscription_days_bonus(db, user.id, period_days=30)

    assert result is True
    extend_mock.assert_not_awaited()
    create_paid_mock.assert_not_awaited()
    create_referral_earning_mock.assert_not_awaited()


async def test_welcome_days_skipped_when_user_not_referred(monkeypatch):
    """Бонус днями не начисляется, если у пользователя нет referred_by_id."""
    user = SimpleNamespace(id=1, telegram_id=101, full_name='Solo User', referred_by_id=None)

    db = SimpleNamespace(execute=AsyncMock())

    get_user_mock = AsyncMock(return_value=user)
    monkeypatch.setattr(referral_service, 'get_user_by_id', get_user_mock)
    create_referral_earning_mock = AsyncMock()
    monkeypatch.setattr(referral_service, 'create_referral_earning', create_referral_earning_mock)

    monkeypatch.setattr(referral_service.settings, 'REFERRAL_WELCOME_DAYS_PERCENT', 15)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_WELCOME_DAYS_MIN_PERIOD_DAYS', 7)

    result = await referral_service.process_referral_subscription_days_bonus(db, user.id, period_days=30)

    assert result is True
    create_referral_earning_mock.assert_not_awaited()


async def test_welcome_days_skipped_when_period_below_threshold(monkeypatch):
    """Бонус днями не начисляется, если куплено меньше минимального периода."""
    user = SimpleNamespace(id=1, telegram_id=101, full_name='Friend', referred_by_id=2)

    db = SimpleNamespace(execute=AsyncMock())

    get_user_mock = AsyncMock(return_value=user)
    monkeypatch.setattr(referral_service, 'get_user_by_id', get_user_mock)
    create_referral_earning_mock = AsyncMock()
    monkeypatch.setattr(referral_service, 'create_referral_earning', create_referral_earning_mock)

    monkeypatch.setattr(referral_service.settings, 'REFERRAL_WELCOME_DAYS_PERCENT', 15)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_WELCOME_DAYS_MIN_PERIOD_DAYS', 7)

    # Купили подписку на 3 дня — ниже порога в 7
    result = await referral_service.process_referral_subscription_days_bonus(db, user.id, period_days=3)

    assert result is True
    # get_user_by_id даже не должен вызываться, т.к. проверка периода идёт раньше
    create_referral_earning_mock.assert_not_awaited()


async def test_welcome_days_skipped_when_period_days_missing(monkeypatch):
    """Без явного period_days бонус не срабатывает — защита от старых call-site."""
    db = SimpleNamespace(execute=AsyncMock())

    get_user_mock = AsyncMock()
    monkeypatch.setattr(referral_service, 'get_user_by_id', get_user_mock)

    monkeypatch.setattr(referral_service.settings, 'REFERRAL_WELCOME_DAYS_PERCENT', 15)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_WELCOME_DAYS_MIN_PERIOD_DAYS', 7)

    result = await referral_service.process_referral_subscription_days_bonus(db, 1, period_days=None)

    assert result is True
    get_user_mock.assert_not_awaited()


async def test_welcome_days_bonus_computed_from_period(monkeypatch):
    """Бонус считается строго от period_days, а не от остатка подписки."""
    user = SimpleNamespace(id=1, telegram_id=101, full_name='Friend', referred_by_id=2, is_partner=False)
    referrer = SimpleNamespace(id=2, telegram_id=202, full_name='Referrer', remnawave_uuid='ref-uuid', is_partner=False)

    friend_sub = SimpleNamespace(id=10, end_date=object(), remnawave_uuid='friend-uuid')
    referrer_sub = SimpleNamespace(id=20, end_date=object(), remnawave_uuid='ref-sub-uuid')

    earnings_result = SimpleNamespace(first=lambda: None)
    db = SimpleNamespace(execute=AsyncMock(return_value=earnings_result))

    get_user_mock = AsyncMock(side_effect=[user, referrer])
    monkeypatch.setattr(referral_service, 'get_user_by_id', get_user_mock)

    create_referral_earning_mock = AsyncMock()
    monkeypatch.setattr(referral_service, 'create_referral_earning', create_referral_earning_mock)

    sync_mock = AsyncMock()
    monkeypatch.setattr(referral_service, '_sync_bonus_subscription', sync_mock)

    extend_mock = AsyncMock()
    create_paid_mock = AsyncMock()
    get_sub_mock = AsyncMock(side_effect=[friend_sub, referrer_sub])

    import app.database.crud.subscription as sub_crud

    monkeypatch.setattr(sub_crud, 'extend_subscription', extend_mock)
    monkeypatch.setattr(sub_crud, 'create_paid_subscription', create_paid_mock)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_id', get_sub_mock)

    monkeypatch.setattr(referral_service.settings, 'REFERRAL_WELCOME_DAYS_PERCENT', 15)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_WELCOME_DAYS_MIN_PERIOD_DAYS', 7)

    result = await referral_service.process_referral_subscription_days_bonus(db, user.id, period_days=30)

    assert result is True

    # 30 * 15% + 50 = 500 // 100 = 5 дней — округляется к целому
    expected_bonus_days = 5
    # У друга есть подписка → extend; у реферера тоже → extend (create_paid_subscription не вызван)
    assert extend_mock.await_count == 2
    create_paid_mock.assert_not_awaited()
    assert extend_mock.await_args_list[0].args[1] is friend_sub
    assert extend_mock.await_args_list[0].args[2] == expected_bonus_days
    assert extend_mock.await_args_list[1].args[1] is referrer_sub
    assert extend_mock.await_args_list[1].args[2] == expected_bonus_days

    create_referral_earning_mock.assert_awaited_once()


async def test_welcome_days_creates_default_sub_for_referrer_without_one(monkeypatch):
    """Если у реферера нет подписки — создаём sub на дефолтном тарифе."""
    user = SimpleNamespace(id=1, telegram_id=101, full_name='Friend', referred_by_id=2, is_partner=False)
    referrer = SimpleNamespace(id=2, telegram_id=202, full_name='Referrer', remnawave_uuid=None, is_partner=False)
    friend_sub = SimpleNamespace(id=10, end_date=object(), remnawave_uuid='friend-uuid')

    default_tariff = SimpleNamespace(
        id=1,
        is_active=True,
        traffic_limit_gb=70,
        device_limit=5,
        allowed_squads=['squad-uuid'],
    )

    earnings_result = SimpleNamespace(first=lambda: None)
    db = SimpleNamespace(execute=AsyncMock(return_value=earnings_result))

    get_user_mock = AsyncMock(side_effect=[user, referrer])
    monkeypatch.setattr(referral_service, 'get_user_by_id', get_user_mock)

    resolve_tariff_mock = AsyncMock(return_value=default_tariff)
    monkeypatch.setattr(referral_service, '_resolve_default_bonus_tariff', resolve_tariff_mock)

    create_referral_earning_mock = AsyncMock()
    monkeypatch.setattr(referral_service, 'create_referral_earning', create_referral_earning_mock)
    monkeypatch.setattr(referral_service, '_sync_bonus_subscription', AsyncMock())

    extend_mock = AsyncMock()
    create_paid_mock = AsyncMock(return_value=SimpleNamespace(id=33))
    get_sub_mock = AsyncMock(side_effect=[friend_sub, None])

    import app.database.crud.subscription as sub_crud

    monkeypatch.setattr(sub_crud, 'extend_subscription', extend_mock)
    monkeypatch.setattr(sub_crud, 'create_paid_subscription', create_paid_mock)
    monkeypatch.setattr(sub_crud, 'get_subscription_by_user_id', get_sub_mock)

    monkeypatch.setattr(referral_service.settings, 'REFERRAL_WELCOME_DAYS_PERCENT', 15)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_WELCOME_DAYS_MIN_PERIOD_DAYS', 7)

    result = await referral_service.process_referral_subscription_days_bonus(db, user.id, period_days=30)

    assert result is True

    # Реферер получил новую подписку на тарифе "Обычный"
    create_paid_mock.assert_awaited_once()
    call_kwargs = create_paid_mock.await_args.kwargs
    assert call_kwargs['tariff_id'] == default_tariff.id
    assert call_kwargs['traffic_limit_gb'] == default_tariff.traffic_limit_gb
    assert call_kwargs['device_limit'] == default_tariff.device_limit
