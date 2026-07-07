"""Regression for the referral invite screen (share-button flow).

The invite screen was reworked: instead of putting tap-to-copy links into the
message body, the bot referral link now travels in a Telegram «📤 Поделиться»
button (``t.me/share/url``) and the cabinet/site link rides inside that share's
prefilled text. The message body itself is just a short instruction — no raw
links. This guards that wiring.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import app.handlers.referral as ref


async def test_create_invite_message_uses_share_button(monkeypatch):
    captured = {}

    async def fake_edit(callback, text, keyboard):
        captured['text'] = text
        captured['keyboard'] = keyboard

    monkeypatch.setattr(ref, 'edit_or_answer_photo', fake_edit)
    # get_*_referral_link are methods on the Settings class — patch on the class.
    monkeypatch.setattr(
        type(ref.settings), 'get_bot_referral_link', lambda self, code, bot: 'https://t.me/bot?start=ref_X'
    )
    monkeypatch.setattr(
        type(ref.settings), 'get_cabinet_referral_link', lambda self, code: 'https://cab.example/?ref=X&u=1'
    )
    monkeypatch.setattr(ref.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 0)

    db_user = SimpleNamespace(referral_code='X', language='ru')
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username='bot'))
    callback = MagicMock()
    callback.bot = bot
    callback.answer = AsyncMock()

    await ref.create_invite_message(callback, db_user)

    text = captured['text']
    # Тело сообщения — короткая инструкция, без сырых ссылок (они уехали в кнопку «Поделиться»).
    assert 'Поделиться' in text
    assert 'https://t.me/bot?start=ref_X' not in text
    assert 'https://cab.example' not in text

    # Ссылка на бота лежит в кнопке «Поделиться» (t.me/share/url), а не в тексте.
    keyboard = captured['keyboard']
    share_buttons = [
        btn
        for row in keyboard.inline_keyboard
        for btn in row
        if getattr(btn, 'url', None) and 't.me/share/url' in btn.url
    ]
    assert share_buttons, 'нет кнопки «Поделиться» с share-ссылкой'

    qs = parse_qs(urlparse(share_buttons[0].url).query)
    # url-параметр share — реф-ссылка на бота (Telegram покажет её превью сверху).
    assert qs['url'] == ['https://t.me/bot?start=ref_X']
    # Текст приглашения внутри share несёт ссылку на сайт/кабинет.
    assert 'https://cab.example/?ref=X&u=1' in qs['text'][0]
