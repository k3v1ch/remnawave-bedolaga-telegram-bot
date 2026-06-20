"""Unit tests for the white-label clone-bot logic (pure / mocked — no real DB)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.keyboards.inline import get_subscription_keyboard
from app.services.clone_bot_service import resolve_external_squad_uuid
from app.middlewares.tenant_context import TenantContextMiddleware
from app.utils.clone_context import is_clone_context, reset_current_clone, set_current_clone
from app.utils.crypto import decrypt_secret, encrypt_secret


def _callbacks(markup) -> set[str]:
    return {btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data}


class TestResolveExternalSquadUuid:
    """The clone's squad overrides the tariff's; non-clone users never hit the DB."""

    @pytest.mark.asyncio
    async def test_clone_squad_overrides_tariff(self):
        db = AsyncMock()
        db.get.return_value = MagicMock(external_squad_uuid='CLONE-SQUAD')
        tariff = MagicMock(external_squad_uuid='TARIFF-SQUAD')

        result = await resolve_external_squad_uuid(db, clone_bot_id=7, tariff=tariff)

        assert result == 'CLONE-SQUAD'

    @pytest.mark.asyncio
    async def test_non_clone_user_falls_back_to_tariff_without_db(self):
        db = AsyncMock()
        tariff = MagicMock(external_squad_uuid='TARIFF-SQUAD')

        result = await resolve_external_squad_uuid(db, clone_bot_id=None, tariff=tariff)

        assert result == 'TARIFF-SQUAD'
        db.get.assert_not_awaited()  # fast path: no DB lookup for normal users

    @pytest.mark.asyncio
    async def test_clone_without_squad_falls_back_to_tariff(self):
        db = AsyncMock()
        db.get.return_value = MagicMock(external_squad_uuid=None)
        tariff = MagicMock(external_squad_uuid='TARIFF-SQUAD')

        result = await resolve_external_squad_uuid(db, clone_bot_id=7, tariff=tariff)

        assert result == 'TARIFF-SQUAD'

    @pytest.mark.asyncio
    async def test_returns_none_when_nothing_set(self):
        db = AsyncMock()
        tariff = MagicMock(external_squad_uuid=None)

        result = await resolve_external_squad_uuid(db, clone_bot_id=None, tariff=tariff)

        assert result is None


class TestTokenCrypto:
    def test_round_trip(self, monkeypatch):
        monkeypatch.setattr(settings, 'CLONE_TOKEN_SECRET', 'unit-test-secret')
        token = '123456:AAExampleBotToken-_x'
        encrypted = encrypt_secret(token)
        assert encrypted != token
        assert decrypt_secret(encrypted) == token

    def test_wrong_key_raises(self, monkeypatch):
        monkeypatch.setattr(settings, 'CLONE_TOKEN_SECRET', 'secret-A')
        encrypted = encrypt_secret('payload')
        monkeypatch.setattr(settings, 'CLONE_TOKEN_SECRET', 'secret-B')
        with pytest.raises(ValueError):
            decrypt_secret(encrypted)


class TestTenantContextMiddleware:
    @pytest.mark.asyncio
    async def test_injects_clone_snapshot_by_bot_id(self):
        registry = MagicMock()
        entry = MagicMock()
        registry.get_by_bot_id.return_value = entry
        mw = TenantContextMiddleware(registry)
        handler = AsyncMock(return_value='OK')
        bot = MagicMock()
        bot.id = 555
        data = {'bot': bot}

        result = await mw(handler, MagicMock(), data)

        assert result == 'OK'
        assert data['clone_bot'] is entry.snapshot
        registry.get_by_bot_id.assert_called_once_with(555)
        handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_bot_in_data_yields_none(self):
        registry = MagicMock()
        mw = TenantContextMiddleware(registry)
        handler = AsyncMock(return_value='OK')
        data: dict = {}

        result = await mw(handler, MagicMock(), data)

        assert result == 'OK'
        assert data['clone_bot'] is None
        registry.get_by_bot_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_sets_clone_contextvar_during_handler_and_resets_after(self):
        registry = MagicMock()
        registry.get_by_bot_id.return_value = MagicMock()  # resolves to a clone
        mw = TenantContextMiddleware(registry)

        seen: dict = {}

        async def handler(_event, _data):
            seen['in_handler'] = is_clone_context()
            return 'OK'

        bot = MagicMock()
        bot.id = 1
        assert is_clone_context() is False
        await mw(handler, MagicMock(), {'bot': bot})

        assert seen['in_handler'] is True  # visible to UI builders deep in the call stack
        assert is_clone_context() is False  # reset after the update is handled


class TestCloneSubscriptionKeyboard:
    """[🎁 Подарки] and [Профиль] are main-shop-only and hidden on clone bots."""

    def test_main_bot_shows_gift_and_profile(self):
        assert is_clone_context() is False
        cbs = _callbacks(get_subscription_keyboard(language='ru', has_subscription=True, subscription=None))
        assert 'custom_gift' in cbs
        assert 'custom_profile' in cbs
        assert 'menu_balance' in cbs
        assert 'custom_manage' in cbs

    def test_clone_hides_gift_and_profile(self):
        token = set_current_clone(MagicMock())  # any non-None snapshot = clone context
        try:
            cbs = _callbacks(get_subscription_keyboard(language='ru', has_subscription=True, subscription=None))
        finally:
            reset_current_clone(token)

        assert 'custom_gift' not in cbs  # Подарки убраны
        assert 'custom_profile' not in cbs  # Профиль убран
        assert 'menu_balance' in cbs  # Баланс остаётся
        assert 'custom_manage' in cbs  # Управление остаётся


class TestCloneOwnerTopupReward:
    """Награда владельцу клон-бота: партнёр → %, не партнёр → +дни; с гардами."""

    def _user(self, *, uid, clone_bot_id=None, referred_by_id=None):
        u = MagicMock()
        u.id = uid
        u.clone_bot_id = clone_bot_id
        u.referred_by_id = referred_by_id
        u.full_name = f'User{uid}'
        return u

    def _owner(self, *, uid, is_partner, percent=20):
        o = MagicMock()
        o.id = uid
        o.is_partner = is_partner
        o.telegram_id = 1000 + uid
        o.referral_commission_percent = percent
        return o

    def _patch(self, monkeypatch, *, buyer, owner, clone=True):
        import app.services.referral_service as rs

        async def fake_get_user_by_id(db, uid):
            if uid == buyer.id:
                return buyer
            if owner and uid == owner.id:
                return owner
            return None

        async def fake_get_clone_bot(db, cid):
            return MagicMock(owner_user_id=owner.id, bot_username='aiusdaoudhbot') if (clone and owner) else None

        monkeypatch.setattr(rs, 'get_user_by_id', fake_get_user_by_id)
        monkeypatch.setattr('app.database.crud.clone_bot.get_clone_bot', fake_get_clone_bot)

        balance = AsyncMock(return_value=True)
        days = AsyncMock(return_value=10)  # начислено 10 дней
        earning = AsyncMock()
        notify = AsyncMock()
        monkeypatch.setattr(rs, 'add_user_balance', balance)
        monkeypatch.setattr(rs, '_award_inviter_topup_days', days)
        monkeypatch.setattr(rs, 'create_referral_earning', earning)
        monkeypatch.setattr(rs, 'send_referral_notification', notify)
        self._notify = notify
        return balance, days, earning

    @pytest.mark.asyncio
    async def test_partner_owner_gets_commission_no_days(self, monkeypatch):
        from app.services.referral_service import process_clone_owner_topup

        buyer = self._user(uid=2, clone_bot_id=7)
        owner = self._owner(uid=1, is_partner=True, percent=20)
        balance, days, earning = self._patch(monkeypatch, buyer=buyer, owner=owner)

        await process_clone_owner_topup(AsyncMock(), buyer.id, 100000, bot=MagicMock())

        assert balance.await_count == 1
        assert balance.await_args.args[2] == 20000  # 20% от 1000₽
        assert days.await_count == 0
        assert earning.await_count == 1
        assert self._notify.await_count == 1  # владелец уведомлён о доходе

    @pytest.mark.asyncio
    async def test_non_partner_owner_gets_days_above_min(self, monkeypatch):
        from app.services.referral_service import process_clone_owner_topup

        buyer = self._user(uid=2, clone_bot_id=7)
        owner = self._owner(uid=1, is_partner=False)
        balance, days, earning = self._patch(monkeypatch, buyer=buyer, owner=owner)

        await process_clone_owner_topup(AsyncMock(), buyer.id, settings.REFERRAL_MINIMUM_TOPUP_KOPEKS, bot=MagicMock())

        assert days.await_count == 1
        assert days.await_args.kwargs['earning_reason'] == 'clone_owner_topup_days'
        assert days.await_args.kwargs['bot'] is None  # реф-текст заглушён
        assert balance.await_count == 0
        assert self._notify.await_count == 1  # владелец уведомлён клон-текстом

    @pytest.mark.asyncio
    async def test_non_partner_below_min_no_reward(self, monkeypatch):
        from app.services.referral_service import process_clone_owner_topup

        buyer = self._user(uid=2, clone_bot_id=7)
        owner = self._owner(uid=1, is_partner=False)
        balance, days, _ = self._patch(monkeypatch, buyer=buyer, owner=owner)

        await process_clone_owner_topup(AsyncMock(), buyer.id, settings.REFERRAL_MINIMUM_TOPUP_KOPEKS - 1, bot=MagicMock())

        assert days.await_count == 0
        assert balance.await_count == 0

    @pytest.mark.asyncio
    async def test_no_clone_bot_id_skips(self, monkeypatch):
        from app.services.referral_service import process_clone_owner_topup

        buyer = self._user(uid=2, clone_bot_id=None)
        owner = self._owner(uid=1, is_partner=True)
        balance, days, _ = self._patch(monkeypatch, buyer=buyer, owner=owner)

        await process_clone_owner_topup(AsyncMock(), buyer.id, 100000, bot=MagicMock())

        assert balance.await_count == 0 and days.await_count == 0

    @pytest.mark.asyncio
    async def test_self_purchase_skips(self, monkeypatch):
        from app.services.referral_service import process_clone_owner_topup

        buyer = self._user(uid=1, clone_bot_id=7)  # покупатель == владелец
        owner = self._owner(uid=1, is_partner=True)
        balance, days, _ = self._patch(monkeypatch, buyer=buyer, owner=owner)

        await process_clone_owner_topup(AsyncMock(), buyer.id, 100000, bot=MagicMock())

        assert balance.await_count == 0 and days.await_count == 0

    @pytest.mark.asyncio
    async def test_owner_is_referrer_skips_double_pay(self, monkeypatch):
        from app.services.referral_service import process_clone_owner_topup

        buyer = self._user(uid=2, clone_bot_id=7, referred_by_id=1)
        owner = self._owner(uid=1, is_partner=True)
        balance, days, _ = self._patch(monkeypatch, buyer=buyer, owner=owner)

        await process_clone_owner_topup(AsyncMock(), buyer.id, 100000, bot=MagicMock())

        assert balance.await_count == 0 and days.await_count == 0
