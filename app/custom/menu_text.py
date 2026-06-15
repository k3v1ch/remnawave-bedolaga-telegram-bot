"""CUSTOM-UI: A/B/C-тексты главного меню по эталону ВЕРНО VPN (SCR-MAIN-MENU).

Три состояния текста:
- **A** — триал доступен (новый пользователь);
- **B** — есть активная подписка;
- **C** — триал использован, активной подписки нет.

Тексты берутся из locale-ключей ``CUSTOM_MAIN_MENU_A/B/C`` с дефолтами,
дословно перенесёнными из эталона (``verno_mock_bot/app/i18n/texts.py``,
SCR_MAIN_MENU_A/B/C). Плейсхолдеры: ``{user_name}`` (A/B/C), ``{days}`` (B).
"""

from __future__ import annotations

import html


CUSTOM_MAIN_MENU_A_DEFAULT = (
    '{user_name}, добро пожаловать в ВЕРНО VPN! 👋\n'
    'Хотите попробовать надежный и быстрый VPN бесплатно?\n'
    '\n'
    'ВЕРНО VPN - работает верно, всегда и везде.\n'
    '\n'
    'Почему стоит попробовать:\n'
    '\n'
    '✦ YouTube в 4K без рекламы, Telegram летает\n'
    '✦ Скорость до 10 Гбит/с\n'
    '✦ Безлимитный трафик\n'
    '✦ До 100 устройств в одной подписке\n'
    '✦ Без логов, полная приватность, ваши данные в безопасности\n'
    '✦ Выгодная реферальная и партнерская программа\n'
    '\n'
    '🎁 Попробуйте бесплатно - 3 дня без карты и без автопродления.'
)

CUSTOM_MAIN_MENU_B_DEFAULT = (
    '{user_name}, добро пожаловать! 👋\n'
    '\n'
    'ВЕРНО VPN - работает верно, всегда и везде.\n'
    '\n'
    '✦ Скорость до 10 Гбит/с\n'
    '✦ Безлимитный трафик\n'
    '✦ Выгодная реферальная и партнерская программа\n'
    '\n'
    '✅ Подписка активна | Осталось: {days} дней'
)

CUSTOM_MAIN_MENU_C_DEFAULT = (
    '{user_name}, добро пожаловать! 👋\n'
    'Вы уже попробовали наш сервис, продолжим?\n'
    '\n'
    'ВЕРНО VPN - работает верно, всегда и везде.\n'
    '\n'
    'Почему стоит продолжить:\n'
    '\n'
    '✦ YouTube в 4K без рекламы, Telegram летает\n'
    '✦ Скорость до 10 Гбит/с\n'
    '✦ Безлимитный трафик\n'
    '✦ До 100 устройств в одной подписке\n'
    '✦ Без логов, полная приватность, ваши данные в безопасности\n'
    '✦ Выгодная реферальная и партнерская программа\n'
    '\n'
    'Выберите тариф, чтобы продолжить пользоваться. 👇'
)


def _active_subscriptions(user) -> list:
    subscriptions = getattr(user, 'subscriptions', None) or []
    return [
        sub
        for sub in subscriptions
        if getattr(sub, 'is_active', False) or getattr(sub, 'actual_status', None) == 'limited'
    ]


def is_trial_available(user) -> bool:
    """Доступен ли триал пользователю (единый гейт модели User)."""
    try:
        return not user.is_trial_already_used()
    except Exception:
        return False


def build_main_menu_text(user, texts) -> str | None:
    """Выбирает текст главного меню A/B/C по статусу пользователя.

    Возвращает None, если построить текст не удалось (вызывающий код
    откатывается на стандартное поведение бедолаги).
    """
    user_name = html.escape(getattr(user, 'full_name', None) or '')

    active_subs = _active_subscriptions(user)
    if active_subs:
        days = max(int(getattr(sub, 'days_left', 0) or 0) for sub in active_subs)
        template = texts.t('CUSTOM_MAIN_MENU_B', CUSTOM_MAIN_MENU_B_DEFAULT)
        return template.format(user_name=user_name, days=days)

    if is_trial_available(user):
        template = texts.t('CUSTOM_MAIN_MENU_A', CUSTOM_MAIN_MENU_A_DEFAULT)
    else:
        template = texts.t('CUSTOM_MAIN_MENU_C', CUSTOM_MAIN_MENU_C_DEFAULT)

    return template.format(user_name=user_name)
