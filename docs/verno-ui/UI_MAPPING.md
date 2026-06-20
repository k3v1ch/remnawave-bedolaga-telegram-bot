# UI_MAPPING — карта переноса визуала ВЕРНО VPN (verno_mock_bot) на бэкенд Бедолаги (keldari-bot)

> Этап 1 (анализ). Эталон: `/root/vernovpnbot/Files/verno_mock_bot` (107 SCR-ID, спека
> `/root/vernovpnbot/Docs/BotV3pravki_FINAL_DEV.md`). Бэкенд: `/root/keldari-bot`, ветка `keldari-ui`
> (форк BEDOLAGA-DEV v3.60.0, уже содержит свою кастомизацию главного меню).
>
> Принцип: **минимальная инвазивность** — сначала locale-оверрайды (`locales/ru.json`),
> потом конфиг/админка, и только в крайнем случае точечные патчи кода.

---

## 0. Как устроены тексты и кнопки в бедолаге (итог исследования)

### 0.1 Тексты — полностью переопределяемы БЕЗ правки кода

- Все user-тексты и лейблы кнопок идут через `Texts` / `get_texts()`:
  `app/localization/texts.py:144` → `app/localization/loader.py:289 (load_locale)`.
- Два слоя локалей, мерж ключ-за-ключом (`loader.py:288-300`):
  1. **дефолт**: `app/localization/locales/ru.json` (1800 строк, вшит в репо);
  2. **оверрайд**: `{LOCALES_PATH}/ru.json|yml` (по умолчанию `./locales`, `app/config.py:107`) —
     монтируется volume'ом, кладём ТОЛЬКО изменяемые ключи.
- Ключи нормализуются в UPPERCASE, вложенные dict'ы — flatten через `_` (`loader.py:136-162`).
- В коде почти везде `texts.t('KEY', 'дефолт')` или `texts.KEY` → каждый лейбл кнопки и каждый
  экранный текст имеет свой locale-ключ.
- **parse_mode = HTML глобально** (`app/bot_factory.py:27`) → в locale-значениях допустим HTML,
  включая `<tg-emoji emoji-id="...">⚡</tg-emoji>` (premium emoji в текстах сообщений!).
- Хот-релоад: `reload_locales()` (`texts.py:296`) — есть в админке.

**Исключения (нельзя перекрыть локалью, генерируются кодом):**
- `TRAFFIC_*`, `TRAFFIC_UNLIMITED`, `SUPPORT_INFO` — перетираются `_build_dynamic_values()`
  (`app/localization/texts.py:115-141`);
- `RULES_TEXT` — из БД (редактируется в админке), дефолт `RULES_TEXT_DEFAULT` из локали;
- приветствие `/start` — из БД (`app/database/crud/welcome_text.py`, редактор в админке,
  HTML + плейсхолдеры), fallback — locale-ключ `WELCOME_FALLBACK`.

### 0.2 Встроенная кастомизация кнопок/меню (есть из коробки)

| Механизм | Где | Что даёт |
|---|---|---|
| Доп. кнопки главного меню | `app/services/main_menu_button_service.py`, модель `MainMenuButton` (`app/database/models.py:3430`), управление из админки | Произвольные URL/callback-кнопки в главном меню, видимость по условиям |
| Конструктор меню (menu layout) | `MENU_LAYOUT_ENABLED` (`app/config.py:343`), `app/services/menu_layout/` (`constants.py: DEFAULT_MENU_CONFIG` — rows/buttons/conditions/max_per_row), API `app/webapi/routes/menu_layout.py`, кабинет `app/cabinet/routes/admin_menu_layout.py`, кэш `app/utils/menu_layout_cache.py` | Раскладка главного меню (порядок строк, состав, кол-во в ряд, custom-кнопки) через API/веб-админку |
| Стили кнопок (Bot API 9.4) | `app/utils/button_styles_cache.py`, `app/cabinet/routes/admin_button_styles.py` | Пер-секционные `style` (primary/success/danger), `labels` per-language, `icon_custom_emoji_id` (premium-иконка кнопки) |
| Режимы главного меню | `MAIN_MENU_MODE: default \| cabinet` (`app/config.py:887`) | `cabinet` — меню строится из menu_layout-конфига (`inline.py:306 _build_cabinet_main_menu_keyboard`) |
| Цвет/иконки в коде | `InlineKeyboardButton(style=..., icon_custom_emoji_id=...)` — aiogram ≥3.25 (pyproject) поддерживает нативно | То же, что фабрика mock-бота |

**Важно про ветку `keldari-ui`:** `get_main_menu_keyboard_async` (`app/keyboards/inline.py:28-99`)
уже переопределён форком: «Кабинет (WebApp) / Подключиться / Пригласить / Инфо / Админка».
Перенос меню ВЕРНО будет менять именно эту (уже форкнутую) функцию — конфликт с апстримом
локализован в одном месте.

### 0.3 Premium emoji: mock vs бедолага

- **Mock**: тексты plain (без HTML), premium emoji через `MessageEntity(type=custom_emoji)` —
  `app/utils/entities.py (build_custom_emoji_entities)` сканирует текст по `EMOJI_MAP`
  (`app/design/tokens.py:24`, 59 эмодзи → custom_emoji_id), offsets в UTF-16. Отправка только через
  `safe_send/safe_edit` (`app/utils/safe.py`) с автофоллбэком: при `TelegramBadRequest` про
  custom_emoji/style — повторная отправка без entities и со стрипнутыми `style`/`icon_custom_emoji_id`.
- **Бедолага**: глобальный `parse_mode=HTML`, entities-подход не используется. НО:
  1. premium emoji в **текстах** достижимы через HTML-тег `<tg-emoji emoji-id="ID">⚡</tg-emoji>` —
     эквивалент той же entity, прописывается прямо в locale-JSON, ноль кода
     (`app/utils/markdown_to_telegram.py:30` уже включает `tg-emoji` в whitelist тегов);
  2. premium emoji на **кнопках** (`icon_custom_emoji_id`) и цвета (`style`) уже поддержаны
     системой button styles (см. 0.2) для главного меню; для остальных клавиатур — точечные правки.
- **Ограничение Telegram**: custom emoji в сообщениях бота рендерятся только если бот имеет
  купленный username на Fragment (иначе показывается обычный unicode-фоллбэк — деградация мягкая,
  ошибки нет). `icon_custom_emoji_id`/`style` кнопок — Bot API 9.4: на старых клиентах Telegram
  игнорируются. Проверить на проде до массового переноса.

---

## 1. Карта экранов: ядро (вошло в этап 1)

Формат: эталонный текст — ключ в `app/i18n/texts.py` mock'а; кнопки — `app/keyboards/*.py` mock'а;
бедолага — файл:строка хендлера/клавиатуры + locale-ключ.

### 1.1 Онбординг (§6.1)

| SCR-ID | Эталон (mock) | Бедолага | Способ переноса |
|---|---|---|---|
| SCR-START-NEW | `texts.py:27` «{USERNAME}, добро пожаловать в ВЕРНО VPN! 👋 …✦-список…🎁 3 дня без карты»; кнопки `onboarding.py`: `[Попробовать бесплатно]`(primary), `[Посмотреть тарифы]`, `[Как это работает]` | `app/handlers/start.py` (~1700-2350): welcome из БД (`crud/welcome_text.py`) либо `WELCOME_FALLBACK`; кнопка `POST_REGISTRATION_TRIAL_BUTTON` → `trial_activate` (`inline.py:222 get_post_registration_keyboard`) | Текст — через админку (welcome text, HTML) или locale `WELCOME_FALLBACK`. Кнопки `[Посмотреть тарифы]`/`[Как это работает]` — нет в бедолаге → патч `get_post_registration_keyboard` + 2 новых callback'а (инфо-экраны без состояния) |
| SCR-START-REF | `texts.py:45` + блок «Вас пригласил @…, +15%/+15%» | реф-ссылка обрабатывается в `start.py` (deep-link), отдельного текста нет | Доп. абзац в welcome-text; бонусные проценты — из настроек рефералки (`REFERRAL_*` в config) |
| SCR-CHANNEL-GATE | `texts.py:94` «🎉 Один шаг до активации…»; кнопки `[Наш канал](url)` `[Проверить]` | `app/handlers/channel_member.py` + `get_channel_sub_keyboard` (`inline.py:167`); ключи `CHANNEL_SUBSCRIBE_BUTTON`, `CHANNEL_CHECK_BUTTON` | Чисто локаль: текст гейта + 2 ключа кнопок |
| SCR-TRIAL-ACTIVATED | `texts.py:106` «✅ Пробный период активирован! Осталось: 3 дня…»; кнопки `[Подключиться](url, primary)` `[Инструкция](url)` `[‹ Главное меню]` | `app/handlers/subscription/purchase.py:775 activate_trial`, текст `texts.TRIAL_ACTIVATED` (+конкатенации в purchase.py:1112-1262) | Локаль `TRIAL_ACTIVATED`; состав кнопок после триала — патч в purchase.py (сейчас другие кнопки) |
| SCR-TRIAL-USED | `texts.py:164` «⚠️ Бесплатный период уже был активирован…» | ответ alert'ом в `purchase.py` (trial already used) | Локаль |
| SCR-TARIFFS-INFO | `texts.py:133` (3 тарифа, цены) | нет прямого аналога (тарифы — динамический список `tariff_purchase.py:115 format_tariffs_list_text`) | Статичный инфо-экран → новый callback в нашем модуле `app/handlers/keldari_info.py` (отдельный файл, не трогает ядро) |
| SCR-HOW-IT-WORKS | `texts.py:149` | нет аналога | Тот же новый модуль |

### 1.2 Главное меню (§6.2)

| SCR-ID | Эталон | Бедолага | Способ |
|---|---|---|---|
| SCR-MAIN-MENU A/B/C | `texts.py:196/215/228` — 3 состояния текста (A: триал доступен, B: подписка активна «✅ Подписка активна \| Осталось: {days} дней», C: триал использован). Кнопки `main_menu.py:38 kb_main_menu`: A=`[Попробовать бесплатно]`(primary) / C=`[Выбрать тариф]`(primary); далее всем: `[Открыть приложение](url,primary)`, `[Управление подпиской]`→`mm_account`, `[Реферальная программа]`→`mm_ref`, `[Инструкция](url)+[Поддержка](url)` в ряд, `[Информация о нас]`→`mm_about` | Текст: ключ `MAIN_MENU` (ru.json:1102) + `get_main_menu_text` (`app/handlers/menu.py:1213`, статус подписки подставляется кодом, ключи `SUB_STATUS_*`). Клавиатура: `get_main_menu_keyboard_async` (`app/keyboards/inline.py:28`) — УЖЕ форкнута; callbacks бедолаги: `menu_subscription`, `menu_referrals`, `menu_info`, `menu_balance`, `trial_activate`, `menu_support` | Текст — локаль `MAIN_MENU` + `SUB_STATUS_*` (3 состояния эталона ≈ subscription_status-плейсхолдер; для точного A/B/C-сплита — небольшой патч `get_main_menu_text`). Кнопки — правка уже форкнутой `get_main_menu_keyboard_async` (маппинг: Управление подпиской→`menu_subscription`, Реферальная→`menu_referrals`, Информация о нас→`menu_info`, Поддержка/Инструкция — URL из конфига) |
| SCR-ACCOUNT A/B | `texts.py:247/256` (A: «⚠️ У Вас нет активной подписки… Баланс: {balance}₽», B: «✅ Подписка активна / Тариф / Осталось / Устройств {used}/{max} / Баланс»). Кнопки `main_menu.py:80 kb_account`: B=`[Подключиться](url,primary)`, `[Скопировать ключ]`, `[🔄 Сбросить ключ](danger)`, `[Продлить подписку](primary)`, `[Сменить тариф](primary)`, `[Устройства]`, `[Мои подарки]`, `[Баланс]+[Профиль]`, `[‹ Назад]`; A=`[Приобрести подписку](primary)`, `[Мои подарки]`, `[Баланс]+[Профиль]`, `[‹ Назад]` | Экран «Подписка»: `app/handlers/subscription/summary.py` / `my_subscriptions.py`, текст `SUBSCRIPTION_INFO` (ru.json:1481), клавиатура `get_subscription_keyboard` (`inline.py:1031`): `CONNECT_BUTTON`, `MENU_EXTEND_SUBSCRIPTION`, `AUTOPAY_BUTTON`, devices/traffic/countries-кнопки | Текст — локаль `SUBSCRIPTION_INFO` (плейсхолдеры совместимы: status/end_date/days_left/devices). Кнопки — лейблы через локаль (`CONNECT_BUTTON` и др.); состав/порядок — патч `get_subscription_keyboard`. «Скопировать ключ»/«Сбросить ключ» — есть `subscription/links.py`, `revoke.py` |
| SCR-SUB-EXPIRED | `texts.py:269` «❌ Срок подписки истёк {expire_date}…»; кнопки `[Продлить](primary)` `[Сменить тариф](primary)` `[‹ Главное меню]` | уведомления `app/services/notification_*`, статус в меню `SUB_STATUS_EXPIRED`-ключи | Локаль (ключи статусов + текст пуша истечения) |
| SCR-TARIFF-CHANGE-CONFIRM | `texts.py:279` + 3 варианта price_diff-строки; кнопка `[Подтвердить — {diff}₽](success)` | смена тарифа: `tariff_purchase.py` (extend/switch), конфирм-экраны там же | Локаль для текстов; формат кнопки — патч `get_tariff_confirm_keyboard` (`tariff_purchase.py:270`) |

### 1.3 Покупка подписки (§6.7)

| SCR-ID | Эталон | Бедолага | Способ |
|---|---|---|---|
| SCR-PURCHASE A/B/C | `texts.py:887/895/908` «⚡ Приобрести подписку…»; кнопки `purchase.py` mock: `[Попробовать бесплатно]/[Выбрать тариф]/[Продлить]/[Сменить тариф]/[Подарить подписку]` | вход в покупку: `subscription/purchase.py` + `tariff_purchase.py:549 show_tariffs_list` (callback `tariff_list`) | Локаль для заголовков; «Подарить подписку» — в бедолаге нет user-flow дарения (только активация `gift_activation.py`) → фаза 2 или скрыть |
| SCR-TARIFF | `texts.py:915` «⚡ Выберите тариф + 3 ✦-преимущества»; кнопки-тарифы `Обычный \| 5 устр. \| от 149₽`, `🔥 Семейный…`, `💎 Бизнес…` (🔥/💎 → icon_custom_emoji_id) | `tariff_purchase.py:115 format_tariffs_list_text` + `:179 get_tariffs_keyboard` (динамика из БД-тарифов, callback `tariff_select:{id}`) | Заголовок — локаль; формат строки кнопки тарифа — патч `get_tariffs_keyboard` (шаблон «{name} \| {devices} устр. \| от {price}₽» можно вынести в locale-ключ) |
| SCR-PERIOD | `texts.py:924` «⚡ Тариф: {name} (до {N} устройств) / Выберите период»; кнопки периодов с ценой | `tariff_purchase.py:201 get_tariff_periods_keyboard` (`tariff_period:*`), формат цены `app/utils/price_display.py` | Локаль + при необходимости шаблон лейбла периода |
| SCR-CONFIRM | `texts.py:931` «⚡ Вы выбрали: Тариф/Срок/Устройств»; кнопка `[Приобрести — {total}₽](success)` | `tariff_purchase.py:270 get_tariff_confirm_keyboard` + `:1357 confirm_tariff_purchase` | Локаль для текста; лейбл кнопки с ценой — уже динамический, патч шаблона |

### 1.4 Баланс (§6.4)

| SCR-ID | Эталон | Бедолага | Способ |
|---|---|---|---|
| SCR-BALANCE | `texts.py:425` «💰 Ваш баланс: {balance}₽»; кнопки `balance.py:47 kb_balance`: `[Пополнить](primary)` `[Ввести промокод]` `[История операций]` `[‹ Назад]` | `handlers/balance/main.py:227 show_balance_menu`, текст `BALANCE_INFO` (ru.json:948); `get_balance_keyboard` (`inline.py:1489`): `BALANCE_HISTORY`+`BALANCE_TOP_UP` в ряд, `BACK` | Текст и лейблы — локаль. Промокод в эталоне внутри баланса, в бедолаге — отдельный пункт меню (`menu_promocode`) → патч `get_balance_keyboard` (добавить кнопку `menu_promocode`) + раскладка 1-в-ряд |
| SCR-TOPUP-AMOUNT | `texts.py:428` «Введите сумму от 10₽» + 6 пресетов (50…1500₽) + `[Отмена]` | `balance/main.py:511 process_topup_amount` (FSM ввод суммы), пресеты есть (`topup_amount\|method\|kopeks`, `main.py:869`) | Локаль; суммы пресетов — конфиг бедолаги |
| SCR-TOPUP-METHOD | `texts.py:440` «Пополнение на {amount}₽ / Выберите способ»: `[СБП (QR)]` `[Банковская карта]` `[Криптовалюта]` | `get_payment_methods_keyboard` (`inline.py:1512`) — методы по включённым провайдерам, ключи `PAYMENT_SBP_*`, `PAYMENT_CARD_*`, `PAYMENT_TELEGRAM_STARS`… | Локаль: переименовать лейблы провайдеров в стиль эталона («СБП (QR)» и т.д.) |
| SCR-TOPUP-PENDING / SUCCESS / FAILED | `texts.py:447/457/465`; success-кнопка контекстная `[Приобрести/Продлить подписку]` | вебхуки платёжек (`app/services/payment_service.py`, `handlers/balance/*`), тексты `BALANCE_TOPUP_SUCCESS`-семейство ключей | Локаль; контекстная CTA после пополнения — патч (малый) в месте отправки уведомления об оплате |
| SCR-PROMO / SCR-PROMO-RESULT | `texts.py:491-517` «✍️ Введите промокод» + 4 результата A-D | `handlers/promocode.py:21 show_promocode_menu` (`texts.PROMOCODE_ENTER`), `:110 process_promocode` (`PROMOCODE_SUCCESS/INVALID/EXPIRED/USED`…) | Чисто локаль |
| SCR-HISTORY | `texts.py:520` «📄 История операций:» — операции как noop-кнопки, 5/стр | `balance/main.py:253 show_balance_history` — операции в тексте сообщения, пагинация | Локаль заголовка; вид «операции-как-кнопки» — патч (опционально, фаза 2; визуально не критично) |

### 1.5 Рефералка (§6.8)

| SCR-ID | Эталон | Бедолага | Способ |
|---|---|---|---|
| SCR-REF | `texts.py:987` — большой экран с ├/└-деревом: бонусы, статистика (переходы/триал/платящие/активные), заработок, 2 ссылки. Кнопки `ref.py`: `[Пригласить друга](switch_inline_query)`, `[Скопировать ссылку]`, `[✨ 7 дней за сторис ✨]`, `[✨ 7 дней за пост ✨]`, `[🤖 Создать свой VPN]`, `[👷 Мои рабочие ссылки]`, `[🔥 Платим за TikTok]`, `[‹ Назад]` | `handlers/referral.py:33 show_referral_info`, текст `REFERRAL_INFO` (ru.json:1329, плейсхолдеры: referrals_count/earned_amount/referral_link/referral_code/…); `get_referral_keyboard` (`inline.py:2130`): создать приглашение/QR/список/аналитика/вывод | Текст — локаль `REFERRAL_INFO` (структуру ├/└ перенести, плейсхолдеры частично совпадают; «переходов по ссылке» в бедолаге нет — убрать или патч). Кнопки: лейблы — локаль; сторис/пост/TikTok/white-label — НЕТ бэкенда → фаза 2 (или URL-кнопки на поддержку) |
| SCR-REF-BONUS-NOTICE | `texts.py:1067` пуш «🎉 Друг @… оформил подписку! +дни +₽» | реф-уведомления в `app/services/referral_*` / notification service; в `keldari-ui` уже изменена логика начислений (коммит 3e7736f2) | Локаль ключей реф-уведомлений |

### 1.6 Поддержка, инфо, fallback (§6.14 и пр.)

| SCR-ID | Эталон | Бедолага | Способ |
|---|---|---|---|
| Поддержка | В эталоне — URL-кнопка `[Поддержка]` из главного меню (`url_support`) | Полноценный тикет-центр: `handlers/support.py`, `tickets.py`, `get_support_keyboard` (`inline.py:2168`), `SUPPORT_MENU_ENABLED`, `SupportSettingsService` | Вариант А (минимум): URL-кнопка на `SUPPORT_USERNAME` в главном меню (уже есть `get_support_contact_url`). Вариант Б: оставить тикеты, перекрасить локалью. Решение за продуктом |
| SCR-ABOUT | `texts.py:2159` «⚡️ Информация / Будь в курсе…» + URL-кнопки (канал/сайт/TOS) | `handlers/menu.py:273 show_info_menu` (`MENU_INFO_HEADER/PROMPT`), правила `show_service_rules` (`menu.py:243`, текст из БД), FAQ (`menu.py:464`), privacy (`menu.py:658`) | Локаль + URL-кнопки через MainMenuButtonService или патч инфо-клавиатуры |
| SCR-FALLBACK | `texts.py:2165` «Используйте кнопки меню…» | `handlers/common.py` (unknown message) | Локаль |
| SCR-RATE-LIMIT | `texts.py:2167` | middleware throttling | Локаль (если ключ есть) / патч |

### 1.7 Устройства (§6.3)

| SCR-ID | Эталон | Бедолага | Способ |
|---|---|---|---|
| SCR-DEVICES / -DETAIL / -ADD / -REDUCE (+confirm) | `texts.py:363-417` (список, докупка слотов по ₽/мес, уменьшение) | `subscription/devices.py` (управление устройствами Remnawave), изменение лимита `CHANGE_DEVICES_PROMPT_TARIFF` (ru.json:970) | Тексты — локаль; модель оплаты слотов в бедолаге привязана к тарифу/периоду, не «₽/мес» — тексты адаптировать под факт. логику |

### 1.8 Вне скоупа этапа 1 (нет бэкенда в бедолаге)

- **§6.5 Подарки** (дарение подписки, gift-ссылки) — в бедолаге только активация промокодов
  (`gift_activation.py`, `promocode.py`). Перенос UI без бэкенда бессмыслен → фаза 2/3.
- **§6.6 Профиль** (email, пароль сайта, удаление аккаунта) — в бедолаге это веб-кабинет
  (`app/cabinet/`), не бот. Кнопку `[Профиль]` вести на кабинет (WebApp), как уже сделано в форке.
- **§6.9 TikTok, §6.10-6.12 White-label, §6.13 Работник** — функционала нет вообще. 
  Кнопки скрыть или вести на поддержку/канал.
- **§20 Push (16 шт.)** — у бедолаги своя система уведомлений; тексты пушей переносить локалью
  по мере маппинга событий (sub_expired, low_balance, topup_success, ref_bonus — прямые аналоги есть).

**Итог по покрытию: ~33 экрана эталона маппятся на бедолагу в этапе 1** (онбординг 7, меню 4,
покупка 4, баланс 8, рефералка 2, инфо/поддержка/fallback 4, устройства 4); ~40+ экранов
(подарки/профиль/TikTok/white-label/worker) — вне скоупа из-за отсутствия бэкенда.

---

## 2. Design tokens эталона и их переносимость

| Токен | Эталон | Перенос в бедолагу |
|---|---|---|
| `EMOJI_MAP` (59 unicode→custom_emoji_id) | `app/design/tokens.py:24` — автозамена в текстах через entities (`utils/entities.py`) | В текстах: `<tg-emoji emoji-id="ID">⚡</tg-emoji>` прямо в locale-JSON (HTML parse_mode; тег уже в whitelist `markdown_to_telegram.py:30`). Альтернатива (хуже): порт `safe_send`+entities — инвазивно, не делать |
| `ICON_LABELS` (label→id для кнопок) | `tokens.py:100` | `InlineKeyboardButton(icon_custom_emoji_id=...)` — поддержано aiogram ≥3.25 и уже используется бедолагой (`inline.py:365`, `miniapp_buttons.py:203`); для главного меню — конфигурится через button_styles (админка), для прочих клавиатур — патч |
| Стили `primary/success/danger` | `tokens.py:14-16`, проставлены в kb_* | `InlineKeyboardButton(style=...)` — уже используется бедолагой (`inline.py:205,208`); переносится 1-в-1 в патчах клавиатур |
| `ICON_KEEP_IN_TEXT` (🟢🟡🔴🔔🔕‹✦├└⏎ остаются текстом) | `tokens.py:185` | Соблюдать при простановке icon_custom_emoji_id |
| Декор `✦ ├ └ ‹ ⏎` | plain-символы в текстах | Переносятся как есть в locale-строках |
| Fallback-механизм | `safe_send/safe_edit` (`utils/safe.py`): при ошибке custom_emoji/style — ретрай без premium-полей; конфиг `USE_PREMIUM_DESIGN` | У бедолаги фоллбэка НЕТ. Риск низкий: `<tg-emoji>` деградирует на стороне Telegram сам (показывается unicode), `style`/`icon_custom_emoji_id` старыми клиентами игнорируются. Но: **бот без Fragment-username** — `<tg-emoji>` просто покажет обычный эмодзи (не ошибка). Рекомендация: завести флаг `KELDARI_PREMIUM_EMOJI=true/false` и генерировать locale-оверрайд из шаблона (см. план) |

---

## 3. Сводный реестр callback-маппинга (эталон → бедолага)

| Mock callback | Бедолага callback | Примечание |
|---|---|---|
| `onb_try_free`, `mm_trial` | `trial_activate` | |
| `mm_choose_tariff`, `acc_buy` | `tariff_list` (режим тарифов) / `menu_buy` | |
| `mm_account`, `acc_*` | `menu_subscription` | |
| `mm_ref` | `menu_referrals` | |
| `mm_about` | `menu_info` | |
| `acc_extend`, `se_extend` | `subscription_extend` / `tariff_extend:*` | |
| `acc_change_tariff` | `tariff_list` (switch) | |
| `acc_devices` | devices-callbacks `subscription/devices.py` | |
| `acc_balance` | `menu_balance` | |
| `acc_key_reset` | `subscription_revoke` (`revoke.py`) | |
| `bal_topup` | `balance_topup` | |
| `bal_promo` | `menu_promocode` | в эталоне — внутри баланса |
| `bal_history:{page}` | `balance_history` (+`_page_N`) | |
| `bal_topup_method:*` | `topup_{method}` / `topup_amount\|{method}\|{kopeks}` | |
| `ref_invite` (switch_inline_query) | `referral_create_invite` | бедолага шлёт сообщение, mock — inline-share; патч на switch_inline_query опционален |
| `ref_copy_link` | ссылка в тексте `REFERRAL_INFO` (`<code>`) | |
| `main_menu` / `back` | `back_to_menu` | |
