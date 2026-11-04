# v1 — лиды, поиск, напоминания

Цель: локальная CRM, в которую подключается аккаунт Telegram, из диалогов и вручную собираются лиды, по ним есть поиск, статус и напоминания.

Образец подключения: `~/.claude/skill-telegram` (Telethon, `send_code_request` + `sign_in`, файл сессии). Сессии и `.env` из того скилла в репозиторий не копировать.

## Внутри версии

- Один локальный оператор, без логина в саму CRM.
- Подключение одного или нескольких аккаунтов Telegram: телефон, код, пароль 2FA.
- Поиск по уже заведённым лидам.
- Поиск по диалогам подключённого аккаунта и добавление человека в лиды. Если запрос начинается с `@`, резолвить этот username.
- Карточка лида: имя, username, telegram id, телефон, статус, заметка, следующий шаг.
- Статусы: `new`, `talking`, `waiting`, `won`, `lost`.
- Напоминание на лида: текст, срок, список открытых и просроченных, отметка «сделано».

## Снаружи версии

- Массовые сообщения, прогрев аккаунтов, парсинг участников групп, цепочки рассылок.
- Командный доступ, роли, деплой.
- Отправка напоминания в Telegram. В v1 напоминание живёт в CRM.

## Проверка каждого этапа

1. Модель и API без Telegram: `pytest` создаёт лид, ищет его по имени и username, меняет статус, ставит напоминание, видит просрочку, закрывает напоминание.
2. Шлюз Telegram подменён в тестах: старт по телефону пишет `pending_code`, верный код переводит в `connected`, поиск диалогов возвращает фикстуру, из неё создаётся лид без дубля.
3. `./scripts/verify.sh` зелёный. `GET /` отдаёт страницу с блоками аккаунта, поиска, лидов и напоминаний.
4. Секретов в git нет: `.env`, `data/`, `*.session` в `.gitignore`.

## Функции

- `app/db.py` — `connect`, `init_db`
- `app/telegram_gateway.py` — `TelegramGateway.request_code`, `confirm_code`, `confirm_password`, `search_dialogs`; `TelethonGateway`; в тестах `FakeGateway`
- `app/accounts.py` — `start_login`, `confirm_login`, `confirm_password`, `list_accounts`
- `app/leads.py` — `create_lead`, `update_lead`, `search_leads`, `add_lead_from_dialog`
- `app/reminders.py` — `create_reminder`, `list_reminders`, `complete_reminder`
- `app/main.py` — маршруты FastAPI и страница `app/templates/index.html`

## Данные

`accounts`: id, phone, session_path, status (`pending_code` | `pending_password` | `connected` | `error`), phone_code_hash, telegram_user_id, telegram_username, telegram_name, created_at.

`leads`: id, account_id, telegram_user_id, username, name, phone, status, note, next_step, created_at, updated_at. Пара `(account_id, telegram_user_id)` уникальна, если id задан.

`reminders`: id, lead_id, due_at, text, done_at, created_at.

Файл сессии: `data/sessions/{account_id}.session`. Каталог `data/` не коммитить.

## API

- `POST /api/accounts/start` `{phone}` → `{id, status}`
- `POST /api/accounts/{id}/code` `{code}` → `{status}` (`connected` или `pending_password`)
- `POST /api/accounts/{id}/password` `{password}` → `{status}`
- `GET /api/accounts`
- `GET /api/accounts/{id}/dialogs?q=` → до 30 диалогов: telegram_user_id, username, name
- `POST /api/leads` ручной лид или `{account_id, telegram_user_id, username, name}`
- `GET /api/leads?q=&status=`
- `PATCH /api/leads/{id}` статус, заметка, next_step, имя
- `POST /api/reminders` `{lead_id, due_at, text}`
- `GET /api/reminders?scope=open|overdue|all`
- `POST /api/reminders/{id}/done`

`TG_API_ID` и `TG_API_HASH` только из окружения. Если их нет, старт входа отвечает 400 с текстом, что нужны ключи с https://my.telegram.org/apps.

## Интерфейс

Одна страница. Фон `#f4f1ea`, текст `#1c1915`, акцент `#0f6e56`. Шрифт: `IBM Plex Sans`. Слева аккаунты и напоминания, справа поиск и таблица лидов. Без героя, градиентов и маркетинговых карточек.

Поиск один: переключатель «В CRM» / «В Telegram». В Telegram результат — кнопка «В лиды». У лида смена статуса и форма напоминания.

## Запуск

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/uvicorn app.main:app --reload
```
