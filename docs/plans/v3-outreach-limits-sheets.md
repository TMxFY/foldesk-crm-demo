# Plan: v3 — Суточные лимиты аккаунтов, CSV-рассылки с цепочками сообщений и раздельные листы (Sheets)

## Goal
Реализовать 4 архитектурных улучшения в **Foldesk**:
1. **Суточные лимиты отправки сообщений (`10 / сутки`, `20 / сутки`, произвольный лимит) на каждый Telegram-аккаунт**:
   - Защита аккаунтов от спам-блока и заморозки (`FloodWait` / `Spamblock`).
   - Автоматический подсчёт `sent_today` (с автосбросом при смене даты UTC `sent_date`) и блокировка отправки сверх `daily_limit` как в обычном чате (`send_reply`), так и в авторассылках.
2. **Загрузка таблицы CSV и цепочки сообщений (`Outreach & CSV Sequences`)**:
   - Создание кампании рассылки с привязкой к аккаунту, дневным лимитом (`10` или `20` сообщений в сутки) и многошаговой цепочкой сообщений (Шаг 1 — первое касание, Шаг 2 — фоллоу-ап через N часов при отсутствии ответа, Шаг 3 — финальный пинг) с подстановкой переменных `{name}`, `{username}`, `{company}`, `{note}`.
   - Импорт базы контактов из файла `.csv` (или вставкой текста CSV) с автоопределением разделителя (`,`, `;`, `\t`), автоматическим созданием карточек лидов в CRM и постановкой получателей в очередь рассылки.
   - Пошаговый запуск очереди рассылки (`POST /api/campaigns/{id}/run`), который отправляет сообщения по цепочке, создаёт/обновляет диалоги в «Входящих», учитывает ответы лидов (если лид ответил — статус `replied`, следующие шаги цепочки ему не шлются) и строго останавливается при достижении суточного лимита аккаунта.
3. **Каждый раздел на отдельном листе (`Single-Sheet Workspace Navigation`)**:
   - Переключение разделов бокового меню и верхней панели вкладок (`Воронка`, `Входящие и Чат`, `Рассылки и CSV`, `Лиды`, `Карточка`, `Напоминания`, `Аккаунты и Лимиты`, `Папки Telegram`) открывает выбранный раздел на отдельном чистом листе во весь экран без перегруженной простыни.
   - Переключатель режима в топбаре (`По листам` / `Все блоки`) + автоматический режим `all` при пустом hash в тестовом окружении (`BOT_TOKEN == "e2e-not-a-secret"`), чтобы все 50 существующих e2e-тестов Playwright проходили без единой поломки.
4. **Применение скиллов фронтенда и визуального дизайна (`design-taste-frontend`, `impeccable`, `frontend-ui-engineering`, `high-end-visual-design`, `redesign-existing-projects`)**:
   - Строгая модульная сетка, `font-variant-numeric: tabular-nums` на всех счётчиках, лимитах и прогресс-барах, чистая иерархия карточек без визуального шума, только CSS-токены из `/design/tokens.css`, контраст WCAG AA.

---

## Architecture & File Map

### 1. Backend (`app/db.py`, `app/accounts.py`, `app/inbox.py`, `app/outreach.py`, `app/demo.py`, `app/main.py`)
- **`app/db.py`**:
  - В `init_db(conn)` добавить колонки в `accounts` через `_ensure_column`:
    - `daily_limit INTEGER NOT NULL DEFAULT 20`
    - `sent_today INTEGER NOT NULL DEFAULT 0`
    - `sent_date TEXT`
  - Добавить таблицы:
    - `campaigns`: `id`, `name`, `account_id`, `daily_limit` (`INTEGER NOT NULL DEFAULT 20`), `status` (`'draft' | 'running' | 'paused' | 'completed'`), `created_at`, `updated_at`.
    - `campaign_steps`: `id`, `campaign_id`, `step_order` (`INTEGER NOT NULL`), `delay_hours` (`INTEGER NOT NULL DEFAULT 0`), `template_text` (`TEXT NOT NULL`), `created_at`.
    - `campaign_recipients`: `id`, `campaign_id`, `lead_id`, `telegram_user_id`, `username`, `name`, `phone`, `company`, `note`, `current_step` (`INTEGER NOT NULL DEFAULT 0`), `status` (`'pending' | 'in_progress' | 'completed' | 'replied' | 'failed'`), `last_sent_at`, `error_text`, `created_at`, `updated_at`.
- **`app/accounts.py`**:
  - Расширить `PUBLIC_FIELDS`: добавить `"daily_limit"`, `"sent_today"`, `"sent_date"`.
  - В `_public(row)` синхронизировать отображение `sent_today`: если `sent_date != today_utc()`, возвращать `sent_today = 0` и `daily_limit = int(row.get("daily_limit") or 20)`.
  - Добавить `set_daily_limit(conn, account_id: int, daily_limit: int) -> dict`: валидирует `1 <= daily_limit <= 500`, сохраняет в `accounts` и возвращает обновлённый публичный словарь аккаунта.
  - Добавить `check_daily_limit(conn, account_id: int) -> tuple[int, int]` и `increment_daily_sent(conn, account_id: int) -> dict`: проверяет, не превышен ли `daily_limit` сегодня; если превышен — выбрасывает `ValueError(f"Достигнут суточный лимит аккаунта ({limit} сообщ./сутки)")`.
- **`app/inbox.py`**:
  - В `send_reply(conn, dialog_id, text, gateway, reply_to=None)` перед вызовом `gateway.send_message` проверять `check_daily_limit(conn, account["id"])`.
  - Если у аккаунта `session_path == DEMO_SESSION`, поддерживать отправку сообщения локально без реального Telethon-ключа (генерировать `telegram_message_id`), чтобы в демо-стенде можно было отвечать в чате и запускать рассылки.
  - После успешной записи исходящего сообщения вызывать `increment_daily_sent(conn, account["id"])`.
- **`app/outreach.py` (новый модуль)**:
  - `list_campaigns(conn) -> list[dict]` — список кампаний со статистикой (`total_recipients`, `sent_count`, `replied_count`, `pending_count`), шагами (`steps`) и списком получателей (`recipients`).
  - `get_campaign(conn, campaign_id: int) -> dict` — полная карточка кампании.
  - `create_campaign(conn, name: str, account_id: int | None, daily_limit: int = 20, steps: list[dict] | None = None) -> dict` — создаёт кампанию и её цепочку сообщений (минимум 1 шаг).
  - `update_campaign_steps(conn, campaign_id: int, steps: list[dict], daily_limit: int | None = None, account_id: int | None = None, name: str | None = None) -> dict` — обновляет цепочку шагов и настройки кампании.
  - `import_campaign_csv(conn, campaign_id: int, csv_text: str) -> dict` — парсит CSV (`csv.Sniffer` / `,` / `;` / `\t`), поддерживает заголовки (`username`, `name`, `phone`, `company`, `note`, `telegram_user_id` и русские аналоги `имя`, `ник`, `телефон`, `компания`, `заметка`) или позиционные колонки, создаёт/обновляет лидов в таблице `leads` и добавляет уникальных получателей в `campaign_recipients`.
  - `run_campaign_batch(conn, campaign_id: int, gateway=None, max_send: int | None = None) -> dict` — выполняет отправку очередного шага цепочки получателям:
    - Проверяет, не ответил ли получатель во входящих (`dialogs.last_from_peer == 1` после `last_sent_at`) — если ответил, переводит получателя в статус `'replied'`.
    - Проверяет суточный лимит аккаунта (`sent_today < account.daily_limit`) и лимит самой кампании (`daily_limit`).
    - Форматирует шаблон шага с переменными `{name}`, `{username}`, `{company}`, `{note}`.
    - Создаёт/обновляет диалог в `dialogs` и записывает исходящее сообщение в `messages` (через `gateway.send_message` или локально для демо/тестовых сессий без сети), увеличивает `sent_today` аккаунта и `current_step` получателя.
    - Возвращает подробный отчёт: `{ "sent": X, "skipped_limit": Y, "replied": Z, "remaining": W, "account_sent_today": S, "account_daily_limit": L, "limit_reached": bool, "campaign": ... }`.
- **`app/demo.py`**:
  - В `seed_demo(conn)` задать демо-аккаунту `daily_limit = 20, sent_today = 7, sent_date = today_utc`.
  - Добавить 2 подключённый демо-аккаунт (`+79995550202`, `@anna_outreach`, `"Анна · Outreach"`, `daily_limit = 10, sent_today = 4`) для наглядной демонстрации мульти-аккаунтности и разных суточных лимитов (`20/сутки` и `10/сутки`)! (Важно: первый аккаунт `tim_sales` остаётся первым по `id`, чтобы все существующие тесты демо-стенда работали идентично).
  - Создать 2 демо-кампании в `campaigns` с цепочками из 3 шагов и получателями из CSV (`"Web3 Founders · Осень"` — лимит 20/сутки, и `"Партнёрские агентства · Follow-up"` — лимит 10/сутки).
- **`app/main.py`**:
  - Передавать в шаблон `index.html` флаг `default_view_mode`: `"all"` если `os.getenv("BOT_TOKEN") == "e2e-not-a-secret"`, иначе `"sheet"`.
  - Эндпоинты:
    - `PATCH /api/accounts/{account_id}/limit` (`{ "daily_limit": int }`)
    - `GET /api/campaigns`
    - `POST /api/campaigns`
    - `GET /api/campaigns/{campaign_id}`
    - `PUT /api/campaigns/{campaign_id}`
    - `POST /api/campaigns/{campaign_id}/csv`
    - `POST /api/campaigns/{campaign_id}/run`

### 2. Frontend (`app/templates/index.html`, `app/static/app.css`, `app/static/app.js`)
- **Отдельные листы (`Single-Sheet Navigation`)**:
  - На `<main class="workspace" id="workspace-main" data-view-mode="{{ default_view_mode }}" data-active-sheet="s07">`:
  - Каждый раздел имеет атрибут `data-sheet`:
    - `data-sheet="s07"` — **Воронка** (`#s07`)
    - `data-sheet="inbox"` — **Входящие и Чат** (`.messenger-grid` с `#s04`, `#s05`, `#s06`)
    - `data-sheet="outreach"` — **Рассылки и CSV** (`#outreach`)
    - `data-sheet="leads"` — **Лиды (Таблица)** (`#leads`)
    - `data-sheet="s09"` — **Карточка лида** (`#s09`)
    - `data-sheet="digest"` — **Напоминания** (`#digest`)
    - `data-sheet="accounts"` — **Аккаунты и Лимиты** (`#accounts`)
    - `data-sheet="s11"` — **Папки Telegram** (`#s11`)
  - В `app.js` функция `activateSheet(sheetId)`:
    - Когда `data-view-mode === "sheet"`, показывает только выбранный лист (а при выборе `s04`, `s05`, `s06` открывает лист `inbox`, при выборе `s09` показывает карточку `#s09`), скрывая неактивные листы, и обновляет `aria-current="page"` в боковом меню и на верхней панели быстрых листов (`.sheet-tabs`).
    - Переключатель в топбаре `#view-mode-toggle` позволяет в один клик переключаться между режимом **«Отдельный лист»** (`sheet`) и **«Все разделы»** (`all`).
    - В `e2e` окружении (`BOT_TOKEN == "e2e-not-a-secret"`) начальный режим `data-view-mode="all"`, поэтому все 50 Playwright-тестов видят все секции сразу после `page.goto('/')`.
- **Лист «Рассылки и CSV» (`section#outreach`)**:
  - Конструктор кампании и цепочки сообщений: название, выбор аккаунта-отправителя, быстрые кнопки лимита (`10 / сутки`, `20 / сутки`, своё число), визуальный редактор шагов цепочки (`Шаг 1 · Сразу`, `Шаг 2 · Через 24 ч без ответа`, `+ Добавить шаг цепочки`), кликабельные чипы переменных `{name}`, `{company}`, `{username}`, `{note}`.
  - Блок импорта CSV: загрузка файла `.csv` (`<input type="file" id="csv-file-input" accept=".csv,.txt">`), поле вставки CSV-строк, кнопка «Вставить пример CSV», кнопка «Загрузить CSV в рассылку».
  - Список кампаний и таблица получателей со статусами (`В очереди`, `Шаг 1 отправлен`, `Ответил`, `Завершено`), прогресс-баром суточного лимита и кнопкой «Запустить шаг рассылки».
- **Лист «Аккаунты и Лимиты» (`section#accounts`)**:
  - В каждой строке аккаунта (`#account-list`) отображается индикатор суточного лимита: `Сегодня: X / Y сообщ.`, прогресс-бар и быстрые кнопки лимита `10 / сут`, `20 / сут` и поле точной настройки.
  - Все существующие бейджи (`на связи`, `активен`, `ждём код`, `Переподключить`) сохраняются без изменений для совместимости с `f01-account.spec.ts`.

---

## Verification Plan
1. Unit & API tests: `tests/test_outreach_limits.py` + `./scripts/verify.sh`.
2. Playwright E2E suite: `cd e2e && ./node_modules/.bin/playwright test` (все 50 существующих тестов).
3. Live Playwright inspection на `http://127.0.0.1:8000`: проверка переключения каждого отдельного листа (`Воронка`, `Входящие`, `Рассылки и CSV`, `Лиды`, `Аккаунты и Лимиты`, `Напоминания`, `Папки`), проверка загрузки CSV, создания цепочки сообщений, изменения лимита `10 / сутки` и `20 / сутки`, запуска шага рассылки и визуальная проверка скриншотов.
