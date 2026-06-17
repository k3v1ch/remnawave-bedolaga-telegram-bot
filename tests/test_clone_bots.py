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
