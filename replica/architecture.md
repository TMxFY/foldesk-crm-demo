# Architecture: локальная Telegram-CRM (срез crmchat)

Соло-оператор подключает свой Telegram, ведёт лиды, статусы, заметки и напоминания. Референс паттернов: `replica/recon.md`. Это не копия crmchat и не их сервер.

Отличия от референса, которые нельзя «починить» в сторону оригинала:

- Нет логина в CRM (ни Google, ни Apple, ни бот). Вход — открыть локальную страницу.
- Нет Telegram-бота. Дайджест и кнопки «готово / отложить / открыть» живут в вебе. `scan_due` в Telegram ничего не шлёт.
- Telegram только через Telethon и ключи оператора `TG_API_ID` / `TG_API_HASH` с https://my.telegram.org/apps. Никаких запросов на crmchat.ai, app.crmchat.ai, developers.crmchat.ai.
- Нет рассылок, прогрева, парсинга участников, покупки аккаунтов, Session+JSON, lookalike и phone→username.

Вертикальный срез уже описан в `docs/plans/v1-leads.md` и реализован в `app/`. Имена функций и маршрутов оттуда не переименовывать.

## Stack

| layer | choice | why |
| --- | --- | --- |
| web | FastAPI + Jinja2 + ванильный JS | один процесс, без сборщика; страница уже в `app/templates` |
| database | SQLite, файл `data/crm.db`, raw SQL через `sqlite3` | один оператор, одна база; ORM не нужен |
| auth | нет учётки CRM | процесс и есть оператор; слушать только `127.0.0.1` |
| payments | нет | в срезе нет тарифов и Stripe |
| email | нет | уведомления только на странице |
| jobs | `asyncio` в том же процессе | срок напоминания проверяет приложение, без очереди-SaaS |
| hosting | локальный uvicorn | не Vercel и не мультиарендный хост |
| telegram | Telethon, файл `data/sessions/{account_id}.session` | официальный клиентский API и ключи пользователя |

Стиль среза уже задан: фон `#f4f1ea`, текст `#1c1915`, акцент `#0f6e56`, шрифт IBM Plex Sans. Новые экраны не вводят второй визуальный язык; токены уточняет `/replica-design`.

## Schema

Таблиц: 14. Плюс view `lead_timeline`. SQL: `replica/schema.sql`.

Доступ: не RLS. Проверка в слое данных. Функции берут `conn` локального процесса и не принимают `user_id`. `list_accounts` отдаёт только `PUBLIC_FIELDS` (`app/accounts.py`): никогда `phone_code_hash` и `session_path`. Пароль 2FA не пишется в базу. На `connected` слой данных обнуляет `phone_code_hash`.

Время: UTC, текст `YYYY-MM-DDTHH:MM:SS` без суффикса, как `app/db.py:utc_now` и `app/reminders.py:normalize_due`. Не `timestamptz` и не строка Telethon по умолчанию. Деньги только в `property_values.value_int` (центы) + `currency`, и только для кастомного поля `amount`.

Почему нет колонки владельца: один оператор, а `create_lead` / `start_login` уже без `user_id`. Фиктивный `operator_id` разошёлся бы со срезом.

Жёсткие ограничения в базе, не в UI:

- `leads_account_tg_unique` — один лид на пару `(account_id, telegram_user_id)`, если id задан. Имя индекса не менять.
- `accounts.id` только `AUTOINCREMENT`. Иначе новый аккаунт получит старый id и чужой файл сессии.
- `dialogs (account_id, peer_type, peer_id)` — id пользователя и id чата пересекаются.
- `messages (dialog_id, telegram_message_id)` — повторный апдейт Telethon не плодит строки.
- `pipeline_views`: не больше одного `is_default = 1`.
- `settings`: одна строка `id = 1`.
- Статусы лида и логина — `CHECK`, те же множества, что `STATUSES` и `ACCOUNT_STATUSES` в `app/db.py`.

`accounts.status` — вход: `pending_code | pending_password | connected | error`. `accounts.health` — уже подключённая сессия: `active | offline | unauthorized | banned | frozen` или NULL, пока вход не кончился. В одну колонку их не сливать: сломается ответ `confirm_login`.

Ключи воронки остаются `new | talking | waiting | won | lost`. Подписи в `pipeline_stages`: Лид, Разговор, Ждём, Выигран, Проигран. Отдельной стадии Proposal нет: шестой ключ пришлось бы добавить в `CHECK` и в `STATUSES`. `lost` остаётся, хотя в дефолте референса его нет. Свои ключи стадий — не этот срез; пока жив `CHECK`, строки пяти ключей не удалять.

`leads.note` — короткое поле среза. Лента заметок — таблица `notes`. Одну в другую сами не копируем. `leads.status` не дублируется строкой в `property_defs`.

Удаление:

- аккаунт → диалоги, сообщения, папки каскадом; у лидов `account_id` становится NULL (история CRM остаётся); файл сессии стирает слой данных;
- лид → заметки, напоминания, задачи, события, значения свойств каскадом; у диалога `lead_id` NULL;
- определение свойства → его значения каскадом.

На базе, которую уже создал `init_db`, у трёх старых внешних ключей нет `ON DELETE` (SQLite так не дописывает). Пока нет пересборки таблиц, `disconnect_account` сначала делает `UPDATE leads SET account_id = NULL`, потом `DELETE FROM accounts`. Новые таблицы создаются сразу с `ON DELETE` из `schema.sql`.

| table | when | holds |
| --- | --- | --- |
| accounts | m1 | телефон, статус входа, путь сессии, профиль; m2: `health`, `last_error` |
| leads | m1 | карточка и статус; m2: `description`, `email`, `url`; m3: `folder_id` |
| reminders | m1 | `due_at`, `text`, `done_at`; m2: необязательный `title` |
| settings | m2 | один часовой пояс, по умолчанию UTC |
| pipeline_stages | m2 | подписи и порядок пяти ключей |
| notes | m2 | заметки карточки |
| lead_events | m2 | смена статуса и системная строка таймлайна |
| dialogs | m2 | кэш входящих, не архив |
| messages | m2 | кэш ленты; вложения файлами не качаем |
| folders | m3 | свои папки Telegram |
| tasks | m3 | чекбокс без даты |
| property_defs | m3 | кастомные поля, не Status |
| property_values | m3 | значение на лиде или на аккаунте |
| pipeline_views | m3 | сохранённый фильтр воронки |

Новые колонки на трёх живых таблицах — только NULL, без обязательности. Иначе текущие `INSERT` в `start_login`, `create_lead`, `create_reminder` упадут. Эскиз `ALTER` — комментарий в конце `schema.sql`.

## API

Кто вызывает всё ниже: локальный оператор. Чужих ключей и вебхуков нет. Вебхуки внутрь: нет. Вебхуки наружу: нет.

`GET /api/leads/lookup` регистрировать раньше `GET /api/leads/{lead_id}`. `POST /api/folders/pull` — раньше пути с `{folder_id}`.

Маршруты m1 уже в `app/main.py`. Пути не менять.

| method path | does | who | input | output | flow |
| --- | --- | --- | --- | --- | --- |
| GET / | страница: аккаунт, поиск, лиды, напоминания | локальный оператор | — | HTML | F01–F05 m1 |
| POST /api/accounts/start | код на телефон | локальный оператор | `{phone}` | `{id, status}` | F01 m1 |
| POST /api/accounts/{account_id}/code | код; `connected` или `pending_password` | локальный оператор | `{code}` | `{status}` | F01 m1 |
| POST /api/accounts/{account_id}/password | пароль 2FA | локальный оператор | `{password}` | `{status}` | F01 m1 |
| GET /api/accounts | список без секретов | локальный оператор | — | аккаунты | F01 m1 |
| GET /api/accounts/{account_id}/dialogs | до 30 людей; `q` с `@` резолвит username | локальный оператор | `q` | `telegram_user_id, username, name` | F02 m1 |
| POST /api/leads | ручной лид или поля диалога | локальный оператор | поля лида | lead | F03 m1 |
| GET /api/leads | поиск по имени, @username, телефону, id | локальный оператор | `q`, `status` | лиды | F02 m1 |
| PATCH /api/leads/{lead_id} | статус, заметка, next_step, имя | локальный оператор | эти поля | lead | F03 m1 |
| POST /api/reminders | напоминание в CRM, не в Telegram | локальный оператор | `{lead_id, due_at, text}` | reminder | F05 m1 |
| GET /api/reminders | `open`, `overdue` или `all` | локальный оператор | `scope` | напоминания | F05 m1 |
| POST /api/reminders/{reminder_id}/done | отметить сделанным | локальный оператор | — | reminder | F05 m1 |
| POST /api/accounts/{account_id}/reconnect | опросить сессию, записать `health` | локальный оператор | — | `{status, health}` | F01 m2 |
| DELETE /api/accounts/{account_id} | отвязать, стереть файл сессии, лиды оставить | локальный оператор | — | 204 | F01 m2 |
| GET /api/inbox | личные чаты, сначала новые | локальный оператор | `account_id`, `q`, `unread`, `waiting` | диалоги | F02 m2 |
| GET /api/inbox/stream | SSE: новое сообщение или смена health | локальный оператор | — | `text/event-stream` | S04 m2 |
| GET /api/dialogs/{dialog_id}/messages | лента из кэша, дыры добирает Telethon | локальный оператор | `before` | сообщения | F07 m2 |
| POST /api/dialogs/{dialog_id}/messages | ответ, опционально reply | локальный оператор | `{text, reply_to}` | message | F07 m2 |
| POST /api/dialogs/{dialog_id}/read | прочитать в Telegram | локальный оператор | — | `{unread_count: 0}` | F07 m2 |
| DELETE /api/messages/{message_id} | удалить в Telegram и в кэше | локальный оператор | — | 204 | F07 m2 |
| POST /api/messages/{message_id}/reaction | эмодзи | локальный оператор | `{emoji}` | 204 | F07 m2 |
| GET /api/dialogs/{dialog_id}/lead | карточка рядом или пусто | локальный оператор | — | lead или null | F03 m2 |
| POST /api/dialogs/{dialog_id}/lead | в CRM без второго лида | локальный оператор | — | lead | F03 m2 |
| GET /api/leads/lookup | предупреждение о дубле, без слияния | локальный оператор | `telegram_user_id`, `username`, `phone` | лиды | F03 m2 |
| GET /api/leads/{lead_id} | карточка | локальный оператор | — | lead | F03 m2 |
| GET /api/pipeline | колонки по пяти статусам | локальный оператор | — | stages + columns | F03 m2 |
| GET /api/stages | подписи колонок | локальный оператор | — | stages | F03 m2 |
| GET /api/leads/{lead_id}/notes | заметки | локальный оператор | — | notes | F04 m2 |
| POST /api/leads/{lead_id}/notes | новая заметка, источник `manual` | локальный оператор | `{body}` | note | F04 m2 |
| GET /api/leads/{lead_id}/timeline | view `lead_timeline` | локальный оператор | — | события | F04 m2 |
| POST /api/reminders/{reminder_id}/postpone | новый `due_at`, `done_at` не ставить | локальный оператор | `{due_at}` | reminder | F05 m2 |
| GET /api/digest | задачи на сегодня и просрочка, не бот | локальный оператор | — | `{timezone, today, overdue}` | F05 m2 |
| GET /api/settings | часовой пояс | локальный оператор | — | `{timezone}` | F05 m2 |
| PATCH /api/settings | пояс из `zoneinfo`, иначе 400 | локальный оператор | `{timezone}` | `{timezone}` | F05 m2 |
| GET /api/folders | папки аккаунта | локальный оператор | `account_id` | folders | F06 m3 |
| POST /api/folders/pull | список своих папок из Telegram | локальный оператор | `{account_id}` | folders | F06 m3 |
| PATCH /api/folders/{folder_id} | тумблер синка | локальный оператор | `{sync_enabled}` | folder | F06 m3 |
| POST /api/folders/{folder_id}/sync | подтянуть людей из папки в лиды | локальный оператор | — | сколько добавлено | F06 m3 |
| GET /api/properties | определения полей | локальный оператор | `object_type` | defs | S14 m3 |
| POST /api/properties | новое поле | локальный оператор | def | def | S14 m3 |
| PATCH /api/properties/{property_id} | имя, опции, порядок | локальный оператор | поля def | def | S14 m3 |
| DELETE /api/properties/{property_id} | удалить поле и значения | локальный оператор | — | 204 | S14 m3 |
| PUT /api/leads/{lead_id}/properties/{key} | записать значение | локальный оператор | `{value_text, value_int, currency}` | value | S14 m3 |
| GET /api/views | сохранённые виды | локальный оператор | — | views | S15 m3 |
| POST /api/views | сохранить фильтр | локальный оператор | `{name, filters_json}` | view | S15 m3 |
| PATCH /api/views/{view_id} | переименовать или сделать дефолтом | локальный оператор | поля view | view | S15 m3 |
| DELETE /api/views/{view_id} | удалить вид | локальный оператор | — | 204 | S15 m3 |
| POST /api/dialogs/{dialog_id}/unread | пометить непрочитанным в Telegram | локальный оператор | — | dialog | S04 m3 |
| GET /api/leads/{lead_id}/tasks | чекбоксы | локальный оператор | — | tasks | S09 m3 |
| POST /api/leads/{lead_id}/tasks | новый чекбокс | локальный оператор | `{title}` | task | S09 m3 |
| POST /api/tasks/{task_id}/done | отметить чекбокс | локальный оператор | — | task | S09 m3 |
| GET /api/leads/{lead_id}/dialog | открыть чат с карточки | локальный оператор | — | dialog или 404 | S09 m3 |

Итого маршрутов: 52. Must (m1+m2): 34, из них 12 уже есть. Should (m3): 18.

`GET /api/inbox` в m2 принимает `unread` и `waiting`, но чипы на экране — m3. `include_groups` не делаем: список по умолчанию `peer_type = user`. Группы в кэш может положить синк папок, во входящие они не попадают.

Фильтры `q` на входящих: без букв — SQL по `last_message_at`; с буквами — уже существующий `search_dialogs`, не второй поиск. `search_leads` не переписывать на `LIKE`.

Напоминание фразой в чате маршрута не имеет. Форвард боту (S19) — `apply_forward_update` на `POST /api/operator/update` и в `poll_bot_login`: лид и заметка, без ответа автору. Захват из чата — `add_lead_from_dialog` и ручная форма.

### Jobs

| name | schedule | function | does |
| --- | --- | --- | --- |
| scan_due | каждые 30 с | `app/jobs.py:scan_due` | тот же предикат, что `list_reminders(scope="overdue")`; один раз за жизнь процесса шлёт событие в SSE; Telegram не вызывает |
| refresh_accounts | каждые 60 с | `app/jobs.py:refresh_accounts` | `session_health`; `offline` — один `connect`; `unauthorized` / `banned` / `frozen` сами не логинят |
| watch_account | пока процесс жив, на каждый `connected` | `app/inbox.py:watch_account` | NewMessage → upsert диалога и сообщения → SSE |

`app/jobs.py:start_background` стартует их из lifespan `create_app`. Отдельного воркера нет. FloodWait останавливает повтор, а не крутит новый.

## Функции

Уже есть, не переименовывать:

- `app/db.py` — `connect`, `init_db`, `utc_now`, `as_dict`, `resolve_session`
- `app/telegram_gateway.py` — `TelegramGateway.request_code`, `confirm_code`, `confirm_password`, `search_dialogs`; классы `TelethonGateway`, `FakeGateway`; `GatewayError`
- `app/accounts.py` — `start_login`, `confirm_login`, `confirm_password`, `list_accounts`
- `app/leads.py` — `create_lead`, `update_lead`, `search_leads`, `add_lead_from_dialog`
- `app/reminders.py` — `normalize_due`, `create_reminder`, `list_reminders`, `complete_reminder`
- `app/main.py` — `create_app`

Новые методы шлюза добавлять обычными, не `@abstractmethod`, и сразу на `FakeGateway`. Иначе упадёт текущий тестовый шлюз.

m2, новые имена:

- `TelegramGateway` — `load_inbox`, `fetch_messages`, `send_message`, `delete_message`, `send_reaction`, `mark_read`, `session_health`
- `app/accounts.py` — `reconnect_account`, `disconnect_account`; в `PUBLIC_FIELDS` добавить `health`
- `app/inbox.py` — `sync_inbox`, `list_inbox`, `get_thread`, `send_reply`, `remove_message`, `react`, `mark_dialog_read`, `watch_account`
- `app/leads.py` — `get_lead`, `find_duplicates`; у `create_lead` только новые необязательные kwargs `description`, `email`, `url`; в `update_lead` множество `allowed` можно расширить этими тремя, четыре старых поля не убирать
- `app/notes.py` — `create_note`, `list_notes`
- `app/pipeline.py` — `list_stages`, `pipeline_board`
- `app/reminders.py` — `postpone_reminder`, `digest_today`; `SCOPES` не расширять, дайджест — отдельная функция
- `app/timeline.py` — `lead_timeline`
- `app/settings.py` — `get_settings`, `update_settings`
- `app/jobs.py` — `scan_due`, `refresh_accounts`, `start_background`
- `update_lead` при смене статуса в той же транзакции пишет `lead_events` и по-прежнему возвращает словарь лида

m3:

- `TelegramGateway.list_folders`, `folder_peers`, `mark_unread`
- `app/folders.py` — `list_folders`, `pull_folders`, `update_folder`, `sync_folder`
- `app/properties.py` — `list_property_defs`, `create_property_def`, `update_property_def`, `delete_property_def`, `set_lead_property`
- `app/views.py` — `list_views`, `create_view`, `update_view`, `delete_view`
- `app/tasks.py` — `list_tasks`, `create_task`, `complete_task`
- `app/inbox.py` — `mark_dialog_unread`, `dialog_for_lead`

`sync_folder` зовёт `add_lead_from_dialog` на людей. `GetParticipants` и обход участников групп не вызывать. Папка с типами чатов Contacts/Groups или с invite link не синкается: `blocked_reason`, `sync_enabled = 0`.

`set_lead_property` проверяет, что `object_type` определения равен `lead`. Для `amount` пишет центы в `value_int` и код валюты, не строку с запятой.

## The parts that bite

- Часовые пояса. В базе всегда UTC без офсета. m1 так и шлёт `due_at`. Экран m2 переводит локальное время оператора (`settings.timezone`, `zoneinfo`) в UTC до `POST /api/reminders` и обратно в `digest_today`. Летнее время не лечить фиксированным смещением. Неизвестный пояс — 400, в базу не писать.
- Идемпотентность. Вебхуков нет, дважды приходить нечему. Повтор NewMessage гасит уникальный `(dialog_id, telegram_message_id)`. `add_lead_from_dialog` при гонке ловит `UNIQUE` и возвращает уже созданного лида. `scan_due` помнит id в памяти процесса; после рестарта просрочка снова всплывёт в SSE, в Telegram она не уходит. `complete_reminder` второй раз не меняет `done_at`.
- Гонки. Два «в лиды» на одного человека держатся индексом, не кнопкой. Повторный `confirm_login` у уже `connected` не вызывает `sign_in` ещё раз. Вход и фоновые задачи делят один `conn`: включить WAL в `connect` на m2 и не писать SQLite из чужого потока без лока. `check_same_thread=False` само по себе порядок не даёт.
- Лимиты Telegram. FloodWait наружу как `GatewayError` с паузой, без автоматической серии.retry. Нет цикла рассылки. Переподключение — один `connect` на аккаунт за тик.
- Файлы. Медиа переписки на диск не качаем: в кэше текст, пустой текст показываем как вложение. Сессия только под `data/sessions/`. Каталог `data/` не коммитить.
- Поиск. Лиды — текущий разбор в `search_leads` (подстрока, `@` снимается). Диалоги с текстом запроса — `search_dialogs`. Пустой инбокс — сортировка кэша по `last_message_at`.
- Realtime. Без брокера: обработчик Telethon в том же процессе и `GET /api/inbox/stream`. Если поток оборвался, страница бьёт `GET /api/inbox`. Кэш можно выкинуть и собрать снова.
- Офлайн. `health = offline` показывает последний кэш. Отправка и логин падают с `GatewayError`, локальные поля лида при этом сохраняются.
- Мультиарендность. Нет. Несколько аккаунтов Telegram — строки `accounts`, не воркспейсы.
- Удаление. Снятие аккаунта и удаление лида делают каскады из раздела Schema. Чужой сервер нечего уведомлять.
- Почта, платежи, офлайн-приложение, QR и скрин LinkedIn — нет.

## Build order

Проверка каждого этапа, когда до него дойдёт код: `./scripts/verify.sh` без `VERIFY_FULL=1`. Новые экраны — Playwright из `terminal/frontend`, сценарий со действием, не голый скриншот.

### 1. Vertical slice

Уже есть: `docs/plans/v1-leads.md`, код `app/`, тесты `tests/test_v1.py`. Заново не начинать и контракт не ломать.

- Экраны: одна страница, куски S02, S03, S08, S10, S13. Не инбокс и не канбан.
- Таблицы: `accounts`, `leads`, `reminders` в том виде, в каком их создаёт `init_db`.
- Маршруты: 12 штук m1.
- Функции: список «уже есть» выше.

### 2. Must-haves

Из `replica/features.csv`, кроме бота и чужого логина. S01 закрыт локальной страницей. S17 и S18 — веб, не бот.

- Экраны: S03 (бейдж `health`), S04, S05, S06, S07, S09, поля S10 (описание, URL, email), S12, панель дайджеста вместо S17, кнопки сделано / отложить / открыть лида вместо S18.
- Таблицы: дописать NULL-колонки на трёх старых; создать `settings`, `pipeline_stages`, `notes`, `lead_events`, `dialogs`, `messages`. Папки и свойства можно создать пустыми тем же `schema.sql`, но экранов у них ещё нет.
- Маршруты: 22 штуки m2.
- Функции: список m2 выше. `search_dialogs` и `add_lead_from_dialog` остаются входными точками поиска и дедупа.

Порядок внутри среза: здоровье сессии → кэш входящих и лента → ответ → карточка у чата и «в лиды» → канбан поверх тех же статусов → заметка и таймлайн → дайджест и отложить. Каждый шаг сам по себе открывается с уже подключённого аккаунта.

### 3. Should-haves

- Экраны: S11, S14, S15, чипы «ждёт ответа» и непрочитанные, чекбокс на S09, Open Chat.
- Таблицы: `folders`, `tasks`, `property_defs`, `property_values`, `pipeline_views`, колонка `leads.folder_id`.
- Маршруты: 18 штук m3.
- Связка лид↔аккаунт отдельной таблицей не делается: это `leads.account_id` со среза.
- Форвард в бота и фраза-напоминание в чате не входят. Синк папки не обходит участников групп.

### 4. Fixes from replica-entrepreneur

Ещё не запускался. Сюда не подмешивать чужой бэклог, пока отчёта нет.

## Out of scope

Массовые рассылки и цепочки, прогрев, парсинг участников, покупка аккаунтов, Session+JSON, lookalike, phone→username, готовые базы, прокси их TL API, AI-агент, синк подписчиков канала, командный round-robin, воркспейсы и роли, публичные ключи `sk_`, вебхуки контактов, QR мини-аппа, скрин LinkedIn, тексты и стикерпак crmchat.

Следующий шаг: `/replica-design` — экраны S04, S05, S06, S07, S09 и панель дайджеста. Не лендинг и не их маркетинг.
