"""Tests for the clone-bot required-subscription gate (обяз. подписка).

Covers the middleware branch ``_handle_clone_channel_sub`` (pass-through rules,
member check verdicts, deny screen) and the panel-side channel-ref parsing.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


@dataclass
class FakeSnapshot:
    clone_id: int = 7
    bot_id: int = 111
    channel_sub_enabled: bool = True
    channel_sub_chat_id: int | None = -100123
    channel_sub_link: str | None = 'https://t.me/owner_channel'
    channel_sub_title: str | None = 'Owner Channel'
    channel_sub_text: str | None = None


def _make_middleware():
    from app.middlewares.channel_checker import ChannelCheckerMiddleware

    return ChannelCheckerMiddleware()


def _fake_message(telegram_id: int = 555):
    from aiogram.types import Message

    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock()
    msg.from_user.id = telegram_id
    msg.text = '/start'
    msg.answer = AsyncMock()
    return msg


class TestCloneChannelSubGate:
    @pytest.mark.asyncio
    async def test_disabled_sub_passes_through(self) -> None:
        mw = _make_middleware()
        handler = AsyncMock(return_value='ok')
        snapshot = FakeSnapshot(channel_sub_enabled=False)
        result = await mw._handle_clone_channel_sub(handler, _fake_message(), {'state': None}, snapshot)
        assert result == 'ok'
        handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_channel_passes_through(self) -> None:
        mw = _make_middleware()
        handler = AsyncMock(return_value='ok')
        snapshot = FakeSnapshot(channel_sub_chat_id=None)
        result = await mw._handle_clone_channel_sub(handler, _fake_message(), {'state': None}, snapshot)
        assert result == 'ok'

    @pytest.mark.asyncio
    async def test_subscribed_user_passes_and_caches(self) -> None:
        from app.middlewares import channel_checker

        mw = _make_middleware()
        handler = AsyncMock(return_value='ok')
        msg = _fake_message()
        bot = MagicMock()
        member = MagicMock()
        member.status = 'member'
        bot.get_chat_member = AsyncMock(return_value=member)

        with patch.object(channel_checker, 'cache') as mock_cache:
            mock_cache.exists = AsyncMock(return_value=False)
            mock_cache.set = AsyncMock(return_value=True)
            result = await mw._handle_clone_channel_sub(
                handler, msg, {'state': None, 'bot': bot}, FakeSnapshot()
            )
        assert result == 'ok'
        bot.get_chat_member.assert_awaited_once_with(-100123, 555)
        mock_cache.set.assert_awaited()  # положительный вердикт закэширован

    @pytest.mark.asyncio
    async def test_unsubscribed_user_gets_deny_screen(self) -> None:
        from app.middlewares import channel_checker

        mw = _make_middleware()
        handler = AsyncMock(return_value='ok')
        msg = _fake_message()
        bot = MagicMock()
        member = MagicMock()
        member.status = 'left'
        bot.get_chat_member = AsyncMock(return_value=member)

        with patch.object(channel_checker, 'cache') as mock_cache:
            mock_cache.exists = AsyncMock(return_value=False)
            mock_cache.set = AsyncMock(return_value=True)
            await mw._handle_clone_channel_sub(handler, msg, {'state': None, 'bot': bot}, FakeSnapshot())

        handler.assert_not_awaited()
        msg.answer.assert_awaited_once()
        text = msg.answer.await_args.args[0]
        assert 'подпишитесь' in text.lower()
        kb = msg.answer.await_args.kwargs['reply_markup']
        assert kb.inline_keyboard[0][0].url == 'https://t.me/owner_channel'
        assert kb.inline_keyboard[1][0].callback_data == 'clonesub_check'

    @pytest.mark.asyncio
    async def test_custom_deny_text_is_used(self) -> None:
        from app.middlewares import channel_checker

        mw = _make_middleware()
        msg = _fake_message()
        bot = MagicMock()
        member = MagicMock()
        member.status = 'left'
        bot.get_chat_member = AsyncMock(return_value=member)
        snapshot = FakeSnapshot(channel_sub_text='Мой кастомный текст!')

        with patch.object(channel_checker, 'cache') as mock_cache:
            mock_cache.exists = AsyncMock(return_value=False)
            mock_cache.set = AsyncMock(return_value=True)
            await mw._handle_clone_channel_sub(AsyncMock(), msg, {'state': None, 'bot': bot}, snapshot)

        assert msg.answer.await_args.args[0] == 'Мой кастомный текст!'

    @pytest.mark.asyncio
    async def test_api_error_fails_open(self) -> None:
        """Бота выгнали из канала → проверка невозможна → юзеров НЕ блокируем."""
        from aiogram.exceptions import TelegramAPIError

        from app.middlewares import channel_checker

        mw = _make_middleware()
        handler = AsyncMock(return_value='ok')
        msg = _fake_message()
        bot = MagicMock()
        bot.get_chat_member = AsyncMock(
            side_effect=TelegramAPIError(method=MagicMock(), message='member list is inaccessible')
        )

        with patch.object(channel_checker, 'cache') as mock_cache:
            mock_cache.exists = AsyncMock(return_value=False)
            result = await mw._handle_clone_channel_sub(
                handler, msg, {'state': None, 'bot': bot}, FakeSnapshot()
            )
        assert result == 'ok'

    @pytest.mark.asyncio
    async def test_cached_verdict_skips_api(self) -> None:
        from app.middlewares import channel_checker

        mw = _make_middleware()
        handler = AsyncMock(return_value='ok')
        msg = _fake_message()
        bot = MagicMock()
        bot.get_chat_member = AsyncMock()

        with patch.object(channel_checker, 'cache') as mock_cache:
            mock_cache.exists = AsyncMock(return_value=True)
            result = await mw._handle_clone_channel_sub(
                handler, msg, {'state': None, 'bot': bot}, FakeSnapshot()
            )
        assert result == 'ok'
        bot.get_chat_member.assert_not_awaited()


class TestChannelRefParsing:
    def test_parse_variants(self) -> None:
        from app.services.clone_bot_service import parse_channel_ref

        assert parse_channel_ref('@my_channel') == '@my_channel'
        assert parse_channel_ref('my_channel') == '@my_channel'
        assert parse_channel_ref('https://t.me/my_channel') == '@my_channel'
        assert parse_channel_ref('t.me/my_channel/') == '@my_channel'
        assert parse_channel_ref('не канал!') is None
        assert parse_channel_ref('https://t.me/+privateinvite') is None


class TestSnapshotFields:
    def test_snapshot_defaults(self) -> None:
        from app.services.clone_runtime.registry import CloneSnapshot

        s = CloneSnapshot(
            clone_id=1,
            bot_id=2,
            bot_username='x',
            external_squad_uuid=None,
            profile_title=None,
            webhook_secret='s',
        )
        assert s.channel_sub_enabled is False
        assert s.channel_sub_chat_id is None
        assert s.channel_sub_text is None
