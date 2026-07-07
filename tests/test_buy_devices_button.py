"""«➕ Докупить устройства» в подменю «⚙️ Управление» появляется только когда докупка
реально доступна: тариф с ценой за доп. устройство (>0) и подписка не триальная."""

from types import SimpleNamespace

from app.keyboards.inline import get_subscription_manage_keyboard


def _has_buy_devices(markup) -> bool:
    return any(
        btn.callback_data == 'subscription_change_devices'
        for row in markup.inline_keyboard
        for btn in row
    )


def _sub(*, is_daily=False, device_price=3000):
    tariff = SimpleNamespace(is_daily=is_daily, device_price_kopeks=device_price)
    return SimpleNamespace(id=1, tariff=tariff)


def test_buy_devices_button_shown_with_device_price():
    markup = get_subscription_manage_keyboard('ru', is_trial=False, subscription=_sub(device_price=3000))
    assert _has_buy_devices(markup)


def test_buy_devices_button_hidden_without_device_price():
    markup = get_subscription_manage_keyboard('ru', is_trial=False, subscription=_sub(device_price=0))
    assert not _has_buy_devices(markup)


def test_buy_devices_button_hidden_for_trial():
    markup = get_subscription_manage_keyboard('ru', is_trial=True, subscription=_sub(device_price=3000))
    assert not _has_buy_devices(markup)
