# IMPLEMENTATION_PLAN — перенос визуала ВЕРНО VPN на keldari-bot

> Парная задача: инфраструктуру (docker-compose, Caddy, keldari.online) делает второй агент.
> Здесь — только бот. База: docs/keldari/UI_MAPPING.md.
> Стратегия совместимости с апстримом BEDOLAGA-DEV: **слой 1 (локали) > слой 2 (конфиг/админка) >
> слой 3 (точечные патчи)**. Все патчи — в местах, уже форкнутых в `keldari-ui`, либо в новых файлах.

---

## Слой 1 — locale-оверрайды (≈70% работы, ноль конфликтов с апстримом)

**Механизм:** файл `locales/ru.json` (каталог `LOCALES_PATH`, мержится поверх
`app/localization/locales/ru.json` ключ-за-ключом). В git держим его в репо
(`locales/ru.json` уже отслеживается) — diff к апстриму не возникает, т.к. апстрим меняет
только `app/localization/locales/`.

**Что переносится локалью (тексты + лейблы кнопок):**

| Блок | Ключи бедолаги (основные) |
|---|---|
| Главное меню | `MAIN_MENU`, `MAIN_MENU_ACTION_PROMPT`, `SUB_STATUS_*`, `MAIN_MENU_INVITE_BUTTON`, `MAIN_MENU_INFO_BUTTON`, `CONNECT_BUTTON`, `BALANCE_BUTTON`/`BALANCE_BUTTON_DEFAULT` |
| Онбординг | `WELCOME_FALLBACK`, `POST_REGISTRATION_TRIAL_BUTTON`, `SKIP_BUTTON`, `CHANNEL_SUBSCRIBE_BUTTON`, `CHANNEL_CHECK_BUTTON`, `RULES_*`, `TRIAL_ACTIVATED` |
| Подписка | `SUBSCRIPTION_INFO`, `MENU_SUBSCRIPTION`, `MENU_EXTEND_SUBSCRIPTION`, `MENU_BUY_SUBSCRIPTION`, `AUTOPAY_BUTTON`, `MY_SUBSCRIPTIONS_BUTTON`, статусы `SUBSCRIPTION_STATUS_*` |
| Баланс | `BALANCE_INFO`, `BALANCE_TOP_UP`, `BALANCE_HISTORY`, `PAYMENT_*` (лейблы методов → «СБП (QR)», «Банковская карта», «Криптовалюта»), тексты успеха/ошибки пополнения |
| Промокод | `PROMOCODE_ENTER`, `PROMOCODE_SUCCESS/INVALID/EXPIRED/USED`, `PROMOCODE_*` |
| Рефералка | `REFERRAL_INFO` (переносим ├/└-структуру SCR-REF), `CREATE_INVITE_BUTTON`, `SHOW_QR_BUTTON`, `REFERRAL_LIST_BUTTON`, `REFERRAL_ANALYTICS_BUTTON`, реф-уведомления |
| Инфо/поддержка | `MENU_INFO*`, `MENU_SUPPORT`, `CREATE_TICKET_BUTTON`, `MY_TICKETS_BUTTON`, `CONTACT_SUPPORT_BUTTON`, `FAQ_*`, `RULES_TEXT_DEFAULT`, `PRIVACY_POLICY_*` |
| Общие | `BACK`, `MAIN_MENU_BUTTON`, `BACK_TO_MAIN_MENU_BUTTON`, fallback-сообщения |

**Premium emoji в текстах** — прямо в значениях ключей:
`"BALANCE_INFO": "<tg-emoji emoji-id=\"6030443364178992166\">💰</tg-emoji> Ваш баланс: …"`.
ID брать из `verno_mock_bot/app/design/tokens.py` (EMOJI_MAP, 59 шт.).

**Рекомендация:** держать исходник в `docs/keldari/locale_src/ru.verno.json` + мини-скрипт
генерации (`tools/build_locale.py`): подстановка `<tg-emoji>`-обёрток по EMOJI_MAP включается
флагом, чтобы можно было собрать вариант без premium (если у бота нет Fragment-username).
Оценка: 1-1.5 дня на полный проход по ключам + вычитка.

**Ограничение:** `SUPPORT_INFO` и `TRAFFIC_*` локалью не перекрыть (генерятся в
`app/localization/texts.py:115`) — для `SUPPORT_INFO` либо принять текст из
`_DYNAMIC_LANGUAGE_CONFIGS`, либо точечный патч (5 строк, см. слой 3).

---

## Слой 2 — конфиг и админка (без правок кода)

1. **Welcome `/start` (SCR-START-NEW/REF)** — редактор приветствия в админке (БД,
   `welcome_text`, HTML + плейсхолдеры). Вставить эталонный текст c ✦-списком и
   `<tg-emoji>`. Реф-приписка — через плейсхолдеры/условный блок, если поддержан;
   иначе слой 3.
2. **Правила сервиса** — админка (RULES в БД).
3. **Доп. кнопки главного меню** — `MainMenuButtonService` (URL-кнопки «Инструкция»,
   «Поддержка», «Наш канал», «Открыть приложение») — создаются из админки, без кода.
   Проверить: в форкнутом `get_main_menu_keyboard_async` custom_buttons сейчас
   игнорируются (параметр принимается, но не вставляется) → мини-патч в слое 3.
4. **Menu layout + button styles** (`MENU_LAYOUT_ENABLED=true`, кабинет/API):
   если останемся в `MAIN_MENU_MODE=cabinet` — раскладка, лейблы per-language,
   `style` и `icon_custom_emoji_id` кнопок меню задаются конфигом. Если меню «как в
   эталоне» (callback-кнопки, не WebApp) — используем default-режим и слой 3.
5. **ENV:** `SUPPORT_USERNAME`, `CONNECT_BUTTON_MODE=link` (эталон открывает URL),
   `MAIN_MENU_MODE`, суммы пресетов пополнения, `AVAILABLE_LANGUAGES=ru`
   (эталон одноязычный — упрощает вычитку), `DEFAULT_LANGUAGE=ru`.

Оценка: 0.5 дня + согласование с инфра-агентом (volume `./locales`, env).

---

## Слой 3 — точечные патчи кода (минимальный diff)

Все правки либо в УЖЕ форкнутых местах, либо в новых файлах `app/handlers/keldari_*.py`.

| # | Патч | Файл | Объём | Риск конфликтов с апстримом |
|---|---|---|---|---|
| P1 | Главное меню в стиле SCR-MAIN-MENU: ряды кнопок A/B/C (trial CTA / Выбрать тариф), `[Открыть приложение](url)`, `[Управление подпиской]`, `[Реферальная программа]`, `[Инструкция]+[Поддержка]` (URL), `[Информация о нас]`; `style=primary` на CTA + использование `custom_buttons` | `app/keyboards/inline.py:28-99` (`get_main_menu_keyboard_async` — уже форкнут) | ~60 строк | Низкий (функция уже наша) |
| P2 | A/B/C-состояния текста главного меню (3 locale-ключа `KELDARI_MAIN_MENU_A/B/C`, выбор по trial/sub-статусу, фоллбэк на `MAIN_MENU`) | `app/handlers/menu.py:1213` (`get_main_menu_text`) — обёртка в начале функции, ранний return | ~25 строк | Средний (апстрим трогает этот файл) — оформить как вызов хелпера из нового файла `app/keldari/menu_text.py` |
| P3 | Инфо-экраны без бэкенда: SCR-TARIFFS-INFO, SCR-HOW-IT-WORKS, SCR-ABOUT-ссылки; новые callbacks `keldari_tariffs_info`, `keldari_how_it_works` | НОВЫЙ `app/handlers/keldari_info.py` + 2 строки регистрации в `app/handlers/__init__.py` | ~80 строк нового файла | Минимальный |
| P4 | Онбординг-клавиатура: `[Посмотреть тарифы]`, `[Как это работает]` к пост-регистрации | `app/keyboards/inline.py:222` (`get_post_registration_keyboard`) | ~10 строк | Низкий |
| P5 | Клавиатура подписки в стиле SCR-ACCOUNT: порядок рядов, `[Сменить тариф]`, `[Баланс]+[Профиль]` ряд, `style`/`icon_custom_emoji_id` | `app/keyboards/inline.py:1031` (`get_subscription_keyboard`) | ~40 строк | Средний — минимизировать: только порядок/доп.кнопки, лейблы из локали |
| P6 | Баланс-клавиатура: 1-в-ряд `[Пополнить](primary)`, `[Ввести промокод]`→`menu_promocode`, `[История операций]` | `app/keyboards/inline.py:1489` (`get_balance_keyboard`) | ~15 строк | Низкий |
| P7 | Кнопки после триала (SCR-TRIAL-ACTIVATED): `[Подключиться]+[Инструкция]+[‹ Главное меню]` | `app/handlers/subscription/purchase.py` (места `TRIAL_ACTIVATED`) — вынести клавиатуру в хелпер | ~20 строк | Средний (purchase.py горячий у апстрима) |
| P8 | Шаблоны лейблов тарифа/периода/подтверждения («{name} \| {N} устр. \| от {price}₽», «Приобрести — {total}₽», success-style) через locale-ключи | `app/handlers/subscription/tariff_purchase.py:179,201,270` | ~20 строк | Средний |
| P9 | `SUPPORT_INFO` уважает locale-оверрайд (не перетирать, если ключ есть в user-локали) | `app/localization/texts.py:137` | ~5 строк | Низкий, кандидат в PR апстриму |
| P10 | (Опц.) premium-emoji helper: функция `pe(emoji)` → `<tg-emoji>`-обёртка по EMOJI_MAP для динамически собираемых текстов | НОВЫЙ `app/keldari/design.py` (порт tokens.py) | ~70 строк | Минимальный |

Суммарно: ~300 строк диффа в существующих файлах сосредоточены в keyboards/inline.py (уже
форкнут) + 2-3 новых файла. Оценка: 2-3 дня с ручной проверкой экранов.

---

## Порядок работ

1. **Шаг 0 (0.5 д):** зафиксировать продуктовые решения: 
   (а) меню — callback-стиль эталона или текущий WebApp-кабинет? 
   (б) поддержка — URL или тикет-центр? 
   (в) есть ли у бота Fragment-username (для premium emoji)?
2. **Шаг 1 (1-1.5 д):** `locales/ru.json` — полный проход по ключам из UI_MAPPING §1,
   без premium emoji. Прогон бота, скриншоты экранов.
3. **Шаг 2 (0.5 д):** админка: welcome text, правила, main-menu URL-кнопки; env-конфиг
   совместно с инфра-агентом.
4. **Шаг 3 (1-2 д):** патчи P1-P8 по приоритету: P1/P2 (главное меню) → P6 (баланс) →
   P5 (подписка) → P3/P4 (онбординг/инфо) → P7/P8.
5. **Шаг 4 (0.5 д):** premium-слой: `<tg-emoji>` в локаль (генерация скриптом), 
   `style`/`icon_custom_emoji_id` в патченных клавиатурах; проверка на клиентах
   (старый Telegram Desktop — Bot API 9.4 поля должны игнорироваться).
6. **Шаг 5:** регрессия платёжных потоков (топап → success/failed тексты), триала,
   рефералки; `tests/` бедолаги содержит i18n-guards (коммит 8468ba77) — прогнать.

## Главные риски

1. **Расхождение продуктовых моделей.** Эталон — «3 фикс. тарифа, устройства, ключ VLESS»;
   бедолага — тарифы из БД, трафик/страны/мульти-подписки. Тексты с конкретикой
   («до 5 устройств», «149₽») нельзя хардкодить — либо плейсхолдеры, либо согласовать
   тарифную сетку с продуктом ДО заливки текстов.
2. **Premium emoji в текстах не отрисуются**, если у бота нет username с Fragment —
   деградация мягкая (unicode-эмодзи), но «премиальный» вид пропадает. Проверить на
   реальном боте до массовой простановки `<tg-emoji>`.
3. **Merge-конфликты с апстримом** в `purchase.py`/`menu.py`/`inline.py` — митигировано:
   90% переносим локалью, патчи помечать `# KELDARI-UI` и держать тонкими (вызов
   хелперов из `app/keldari/`), `get_main_menu_keyboard_async` уже наш.
4. **40+ экранов эталона без бэкенда** (подарки, профиль-email, TikTok, white-label,
   worker) — не обещать в этапе 1; кнопки на эти разделы скрыть, иначе «мёртвые» CTA.
5. **`Texts.t()` молча подставляет дефолт** при опечатке в ключе locale-оверрайда
   (ключи нормализуются в UPPERCASE) — обязательно визуальная проверка каждого экрана
   + прогон locale-integrity тестов.
6. **HTML-эскейпинг:** эталонные тексты plain, бедолага — HTML; символы `<`, `>`, `&`
   в переносимых строках экранировать (`&lt;` и т.п.), иначе TelegramBadRequest.
