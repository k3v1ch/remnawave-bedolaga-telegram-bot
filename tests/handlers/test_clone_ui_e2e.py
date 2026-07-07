"""E2E-тесты июльских доработок клон-ботов (UI-поток целиком, Telegram замокан).

Покрывают ровно ручной чек-лист деплоя:
1. После создания клона (process_token) сразу приходит карточка управления ботом.
2. «Мои боты» даже для админа показывает ТОЛЬКО его ботов (админский обзор уехал в acl:*).
3. Админка → Пользователи содержит кнопку «Клон-боты» (acl:list:0), а у списка acl есть «Назад».
4. Экраны «7 дней за сторис/пост» встраивают личную реф-ссылку в надпись «Пользуюсь ВЕРНО VPN».
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.handlers.admin.clone_bots as acl
import app.handlers.clone_bot as cb
import app.handlers.custom_mock as cm
import app.handlers.custom_reseller as cr
from app.keyboards.admin import get_admin_users_submenu_keyboard


def _callbacks(markup) -> set[str]:
    return {
        btn.callback_data for row in markup.inline_keyboard for btn in row if getattr(btn, 'callback_data', None)
    }


# ---------------------------------------------------------------------------
# 1. /clone онбординг: после «Готово!» сразу открывается карточка управления
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_token_opens_management_card(monkeypatch):
    me = SimpleNamespace(id=777, username='testclone_bot', full_name='Test Clone')
    probe = MagicMock()
    probe.get_me = AsyncMock(return_value=me)
    probe.session.close = AsyncMock()
    monkeypatch.setattr(cb, 'create_bot', lambda token: probe)

    monkeypatch.setattr(cb, 'get_clone_bot_by_bot_id', AsyncMock(return_value=None))
    clone = SimpleNamespace(
        id=42,
        owner_user_id=1,
        bot_id=777,
        bot_username='testclone_bot',
        status='active',
        external_squad_name='TestVPN',
        profile_title='TestVPN',
        last_error=None,
        pricing_markup_pct=0,
    )
    monkeypatch.setattr(cb, 'create_clone_bot', AsyncMock(return_value=clone))
    monkeypatch.setattr(cb, 'provision_squad', AsyncMock(return_value=('uuid-1', 'TestVPN')))
    monkeypatch.setattr(cb, 'set_squad', AsyncMock())
    monkeypatch.setattr(cb, 'set_status', AsyncMock())
    monkeypatch.setattr(cb, 'publish_clone_event', AsyncMock())
    admin_notify = MagicMock()
    admin_notify.return_value.send_clone_bot_created_notification = AsyncMock()
    monkeypatch.setattr(cb, 'AdminNotificationService', admin_notify)

    # process_token лениво импортирует get_clone_bot из crud и _render_detail из
    # custom_reseller — crud мокаем, а карточку рендерим по-настоящему.
    monkeypatch.setattr('app.database.crud.clone_bot.get_clone_bot', AsyncMock(return_value=clone))
    monkeypatch.setattr(cr, 'get_stats_bulk', AsyncMock(return_value={42: {}}))
    monkeypatch.setattr(cr, 'count_active_subscribers', AsyncMock(return_value=0))

    db_user = SimpleNamespace(id=1, is_partner=False)
    state = MagicMock()
    state.get_data = AsyncMock(return_value={'clone_name': 'TestVPN'})
    state.clear = AsyncMock()
    message = MagicMock()
    message.text = '123456789:' + 'A' * 35
    message.answer = AsyncMock()

    await cb.process_token(message, db_user, state, db=None)

    texts = [call.args[0] for call in message.answer.await_args_list]
    assert any('Готово' in t for t in texts), 'нет сообщения об успешном создании'

    # Последнее сообщение — карточка управления с полным набором кнопок панели.
    last = message.answer.await_args_list[-1]
    kb = last.kwargs.get('reply_markup')
    assert kb is not None, 'после «Готово!» не пришла карточка управления'
    cbs = _callbacks(kb)
    assert {'myb:stats:42:a', 'myb:links:42', 'myb:bc:42', 'myb:sub:42'} <= cbs
    # Владелец НЕ партнёр — кнопки «Наценка» быть не должно.
    assert 'myb:mk:42' not in cbs
    assert '@testclone_bot' in last.args[0]
    state.clear.assert_awaited()


# ---------------------------------------------------------------------------
# 2. «Мои боты»: даже админ видит только СВОИХ ботов
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_my_bots_list_is_owner_scoped_even_for_admin(monkeypatch):
    list_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(cr, 'list_clone_bots', list_mock)
    monkeypatch.setattr(cr, 'get_stats_bulk', AsyncMock(return_value={}))
    monkeypatch.setattr(cr, 'count_for_owner', AsyncMock(return_value=0))

    db_user = SimpleNamespace(id=5, is_partner=False)
    text, kb = await cr._render_list(None, db_user, is_admin=True, page=0)

    # Запрос в БД жёстко скоуплен на владельца — админский флаг не расширяет выборку.
    assert list_mock.await_args.kwargs.get('owner_user_id') == 5
    assert 'Мои боты' in text
    assert 'админ' not in text.lower()
    cbs = _callbacks(kb)
    assert 'myb:create' in cbs
    assert 'back_to_menu' in cbs


# ---------------------------------------------------------------------------
# 3. Админский обзор клонов: вход из админки и «Назад» обратно в неё
# ---------------------------------------------------------------------------


def test_admin_users_submenu_has_clone_bots_entry():
    kb = get_admin_users_submenu_keyboard('ru')
    assert 'acl:list:0' in _callbacks(kb), 'в админке (Пользователи) нет кнопки «Клон-боты»'


@pytest.mark.asyncio
async def test_admin_clone_list_has_back_to_admin(monkeypatch):
    monkeypatch.setattr(acl, 'list_clone_bots', AsyncMock(return_value=[]))
    monkeypatch.setattr(acl, 'get_stats_bulk', AsyncMock(return_value={}))

    text, kb = await acl._render_list(None, 0)

    assert 'Клон-боты' in text
    assert 'admin_submenu_users' in _callbacks(kb), 'у админского списка клонов нет «Назад» в админку'


# ---------------------------------------------------------------------------
# 4. «7 дней за сторис/пост»: реф-ссылка встроена в «Пользуюсь ВЕРНО VPN»
# ---------------------------------------------------------------------------


def _link_urls(caption: str, entities) -> list[str]:
    """Собирает все URL: из HTML-текста (премиум-эмодзи выключены) или entities."""
    urls = []
    if entities:
        urls += [e.url for e in entities if getattr(e, 'url', None)]
    import re

    urls += re.findall(r'href="([^"]+)"', caption)
    return urls


@pytest.mark.parametrize(
    ('handler_name', 'image_kind'), [('show_ref_stories', 'stories'), ('show_ref_post', 'post')]
)
@pytest.mark.asyncio
async def test_bonus_screens_embed_ref_link_in_caption(monkeypatch, handler_name, image_kind):
    captured = {}

    async def fake_photo(callback, caption, keyboard, parse_mode=None, caption_entities=None, **kwargs):
        captured['caption'] = caption
        captured['entities'] = caption_entities
        captured['keyboard'] = keyboard

    monkeypatch.setattr(cm, 'edit_or_answer_photo', fake_photo)

    db_user = SimpleNamespace(referral_code='REF123', language='ru')
    callback = MagicMock()
    callback.bot.get_me = AsyncMock(return_value=SimpleNamespace(username='vernovpn_bot'))
    callback.answer = AsyncMock()

    await getattr(cm, handler_name)(callback, db_user)

    caption = captured['caption']
    assert 'Пользуюсь ВЕРНО VPN' in caption

    from app.config import settings

    expected = settings.get_bot_referral_link('REF123', 'vernovpn_bot')
    assert 'REF123' in expected  # sanity: ссылка собрана из личного кода
    assert expected in _link_urls(caption, captured['entities']), (
        'реф-ссылка не встроена в надпись «Пользуюсь ВЕРНО VPN»'
    )
    # Голой ссылки в видимом тексте быть не должно — только как href/entity.
    visible = caption if captured['entities'] else __import__('re').sub(r'<[^>]+>', '', caption)
    if captured['entities']:
        assert expected not in caption
    else:
        assert expected not in visible

    assert f'kmock_img:{image_kind}' in _callbacks(captured['keyboard'])


@pytest.mark.asyncio
async def test_bonus_screen_without_ref_code_falls_back_to_hint(monkeypatch):
    captured = {}

    async def fake_photo(callback, caption, keyboard, parse_mode=None, caption_entities=None, **kwargs):
        captured['caption'] = caption

    monkeypatch.setattr(cm, 'edit_or_answer_photo', fake_photo)

    db_user = SimpleNamespace(referral_code=None, language='ru')
    callback = MagicMock()
    callback.answer = AsyncMock()

    await cm.show_ref_stories(callback, db_user)

    assert cm.CUSTOM_REF_LINK_HINT in captured['caption']
