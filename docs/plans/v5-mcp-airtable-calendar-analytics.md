# План v5: MCP-сервер для AI-агентов, Двусторонняя интеграция с Airtable (SoftUGC Suite), Внутренний Календарь и Расширенная Аналитика (5 графиков)

## Архитектурная цепочка (Вход → Система → Выход)

### 1. Схема БД и 7 стадий воронки (`app/db.py`, `app/leads.py`)
- **Вход:** миграция SQLite при старте (`init_db`) и запросы к API лидов/воронки.
- **Система:**
  - Расширение таблицы `leads` полями живой базы Airtable **SoftUGC Suite (`appqqHX4ZpgK7WIfN`)**:
    - `company TEXT` (`Company`)
    - `niche TEXT` (`Niche`)
    - `offer_type TEXT` (JSON-массив `Offer Type`: `Performance`, `Per-video`, `Custom project`, `White label`)
    - `offer_details TEXT` (`Offer Details`)
    - `monthly_value INTEGER NOT NULL DEFAULT 0` (`Monthly Value`)
    - `lead_source TEXT` (`Lead Source`: `Cold outreach`, `Website`, `Referral`, `Event`, `Social Media`, `Email Campaign`, `Other`)
    - `next_follow_up TEXT` (`Next Follow-up`, `YYYY-MM-DD`)
    - `last_contacted TEXT` (`Last Contacted`, `YYYY-MM-DD`)
    - `airtable_record_id TEXT` (уникальный ID записи `rec...` в таблице `Leads`)
    - `airtable_status TEXT` (`New`, `In conversation`, `Call scheduled`, `Offer sent`, `On hold`, `Signed`, `Cancelled`)
    - `airtable_synced_at TEXT`
  - Таблица `calendar_events` для внутреннего календаря и синхронизации с `Tasks` / `Projects` из Airtable:
    - `id INTEGER PRIMARY KEY AUTOINCREMENT`
    - `title TEXT NOT NULL`
    - `event_type TEXT NOT NULL DEFAULT 'call'` (`call`, `meeting`, `followup`, `task_deadline`, `project_deadline`)
    - `start_at TEXT NOT NULL` (ISO UTC `YYYY-MM-DDTHH:MM:SS` или `YYYY-MM-DD`)
    - `end_at TEXT`
    - `all_day INTEGER NOT NULL DEFAULT 0`
    - `lead_id INTEGER REFERENCES leads(id) ON DELETE SET NULL`
    - `meet_link TEXT`
    - `notes TEXT`
    - `status TEXT NOT NULL DEFAULT 'scheduled'` (`scheduled`, `completed`, `cancelled`)
    - `source TEXT NOT NULL DEFAULT 'crm'` (`crm`, `airtable_lead`, `airtable_task`, `airtable_project`)
    - `external_id TEXT` (`rec...` для задач/проектов Airtable)
    - `created_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`
  - 7 стадий воронки (синхронизированных с `Leads.Status` в Airtable):
    1. `new` — `Лид` (`New`)
    2. `talking` — `Разговор` (`In conversation`)
    3. `call_scheduled` — `Созвон назначен` (`Call scheduled`)
    4. `waiting` — `Оффер отправлен` / `Ждём` (`Offer sent`)
    5. `on_hold` — `На паузе` (`On hold`)
    6. `won` — `Выигран` (`Signed`)
    7. `lost` — `Проигран` (`Cancelled`)
  - Двусторонний нормализатор статусов в `app/leads.py`: принимает как ключи (`new`, `talking`, `call_scheduled`, `waiting`, `on_hold`, `won`, `lost`), так и прямые названия из Airtable (`New`, `In conversation`, `Call scheduled`, `Offer sent`, `On hold`, `Signed`, `Cancelled`).
- **Выход:** обратная совместимость со всеми существующими тестами + поддержка всех 7 стадий и полей `SoftUGC Suite`.

---

### 2. Двусторонняя синхронизация с Airtable (`app/airtable_sync.py`)
- **Вход:** переменные `AIRTABLE_API_KEY` и `AIRTABLE_BASE_ID` из `.env` + кнопка **«Синхронизировать с Airtable»** в шапке Лидов (`POST /api/airtable/sync`) + авто-пуш при создании/изменении лида в CRM.
- **Система:**
  - Асинхронный/потокобезопасный клиент с rate-limit (token bucket 5 rps), обработкой `429 Retry-After` и экспоненциальным ретраем `5xx`.
  - **Pull из Airtable (`Leads`, `Tasks`, `Projects`):**
    - Забирает записи из `Leads` (`tblk9OXsIxTTlXi8u`), сопоставляет по `airtable_record_id`, затем по `@username` (из поля `Contact`), телефону или паре `(Lead Name, Company)`.
    - Обновляет/создаёт лидов в CRM со всеми полями (`amount` ← `Value`, `monthly_value` ← `Monthly Value`, `status` ← `Status`, `company` ← `Company`, `niche` ← `Niche`, `offer_type` ← `Offer Type`, `offer_details` ← `Offer Details`, `next_step` ← `Next Step`, `next_follow_up` ← `Next Follow-up`, `note` ← `Notes`, `url` ← `Website`, `lead_source` ← `Lead Source`).
    - Забирает активные задачи с `Due Date` из таблицы `Tasks` и проекты с `End Date` из таблицы `Projects` и синхронизирует их в `calendar_events` (`source = 'airtable_task'` / `'airtable_project'`), чтобы они отображались в Календаре CRM.
  - **Push в Airtable (`Leads`, `Tasks`):**
    - При создании/обновлении лида в CRM (или переносе `Next Follow-up` в календаре) формирует поля для Airtable `Leads` и выполняет `update` (если есть `airtable_record_id`) или `create` (сохраняя полученный `rec...` в `leads.airtable_record_id`).
    - Удаление лида в одной системе не удаляет запись в другой (защита от потери данных).
- **Выход:** эндпоинты `GET /api/airtable/status`, `POST /api/airtable/sync`, `POST /api/airtable/push/{lead_id}` + автоматическая фоновая синхронизация.

---

### 3. Внутренний Календарь (`app/calendar_events.py` + лист `#calendar`)
- **Вход:** лист «Календарь» (`data-sheet="calendar"`) с видами **Месяц / Неделя / День / Повестка**, фильтрами по типу события и Drag-and-Drop карточек по датам.
- **Система:**
  - Единая выборка `GET /api/calendar` за диапазон `[start, end]` объединяет:
    1. События из `calendar_events` (созвоны `call`, встречи `meeting`, дедлайны задач `Tasks` и проектов `Projects` из Airtable).
    2. Даты `next_follow_up` из таблицы `leads` (`source = 'lead_followup'`).
    3. Открытые напоминания из `reminders` (`source = 'reminder'`).
    4. Запланированные фоллоу-апы рассылок (`campaign_recipients` в статусе `in_progress`).
  - Создание/редактирование/перенос (`POST /api/calendar/events`, `PATCH /api/calendar/events/{id}`, `DELETE /api/calendar/events/{id}`, `POST /api/calendar/reschedule`):
    - Перетаскивание любого элемента на другую дату в сетке календаря обновляет его дату: для `calendar_events` — `start_at`/`end_at` (и `Due Date` в Airtable `Tasks`, если это задача Airtable), для `reminder` — `due_at`, для `lead_followup` — `leads.next_follow_up` (с пушем `Next Follow-up` в Airtable!).
    - При создании/изменении созвона пишется событие в `lead_events` (таймлайн карточки лида).
    - Созвоны на сегодня автоматически включаются в `GET /api/focus/today` («Мой фокус на сегодня») и утренний Telegram-дайджест (`app/digest.py`).
- **Выход:** полноценный интерактивный календарь внутри CRM, связанный с карточками лидов, дайджестом и Airtable.

---

### 4. Расширенная Аналитика с 5 графиками (`app/analytics.py` + лист `#analytics`)
- **Вход:** параметр `period` (`7d`, `30d`, `90d`, `all`) в `GET /api/analytics/summary?period=...` и переключатель периода в шапке `#analytics`.
- **Система:**
  - Расширение `get_analytics_summary(conn, period="30d")` полем `charts`:
    1. `daily_flow`: массив по дням `[{date, new_leads, messages_sent, replies_received}]` (Line / Area SVG Chart).
    2. `revenue_timeline`: массив по дням/неделям `[{date, won_amount, pipeline_amount, monthly_recurring}]` (Bar + Line SVG Chart).
    3. `funnel_dropoff`: массив по 7 стадиям `[{key, label, count, amount, conversion_pct, dropped_count, dropped_amount}]` (Горизонтальная воронка + потери).
    4. `channels_and_sources`: разбивка по каналам (`telegram`, `whatsapp`) и источникам (`Cold outreach`, `Website`, `Referral` и т.д.) с `count`, `won_count`, `amount`, `reply_rate_pct` (Grouped Bar + Donut SVG Chart).
    5. `reply_heatmap`: матрица `7 дней недели × 24 часа` (или 6 часовых слотов) по времени входящих ответов `messages (direction='in')` и событий `reply_received` (SVG/CSS Heatmap Grid).
- **Выход:** 5 интерактивных графиков с тултипами, легендой и `tabular-nums` в `#analytics`.

---

### 5. MCP-сервер для AI-агентов (`app/mcp_server.py` + UI-модалка «MCP для агентов»)
- **Вход:**
  - Локальный запуск по `stdio`: `python -m app.mcp_server` (JSON-RPC 2.0 по стандарту Model Context Protocol).
  - Удалённое подключение по HTTP/SSE: `POST /mcp` и `GET /mcp/sse` внутри FastAPI с проверкой `Authorization: Bearer <MCP_API_KEY>` (или `X-MCP-Key`).
  - Публичный справочник конфигурации для UI: `GET /api/mcp/info`.
- **Система (12 инструментов без отправки сообщений в мессенджеры):**
  1. `get_analytics_summary(period="30d")` — вся сквозная аналитика, KPI, воронка, 5 графиков, здоровье аккаунтов и рассылки.
  2. `search_leads(query="", status="", folder_id=None, provider="", limit=50)` — поиск и фильтрация лидов со всеми полями SoftUGC Suite.
  3. `get_lead_full_card(lead_id)` — полный профиль лида: поля, бюджет, история сообщений в чате, заметки, задачи, напоминания, созвоны и таймлайн `lead_events`.
  4. `create_or_update_lead(...)` — создание нового лида или обновление существующего (статус из 7 стадий, `amount`, `monthly_value`, `company`, `niche`, `offer_type`, `offer_details`, `next_step`, `next_follow_up`, `note`).
  5. `add_lead_note(lead_id, body)` — добавление заметки в карточку лида.
  6. `manage_lead_tasks(action, lead_id=None, task_id=None, title="")` — список, создание или закрытие чекбокс-задачи по лиду.
  7. `manage_reminders(action, lead_id=None, reminder_id=None, due_at="", text="")` — создание, перенос или закрытие напоминания.
  8. `get_calendar_schedule(start_date=None, end_date=None, event_type="")` — получение расписания из внутреннего календаря (созвоны, фоллоу-апы, дедлайны задач и проектов).
  9. `schedule_calendar_event(...)` — создание, перенос или завершение созвона/события в календаре CRM.
  10. `list_inbox_dialogs(chip="all", account_id=None, limit=50)` — список диалогов во Входящих (включая фильтр `waiting` — кто ждёт ответа) и чтение истории переписки без отправки сообщений.
  11. `get_focus_today()` — сводка «Мой фокус на сегодня» (горящие ответы, сегодняшние созвоны и напоминания, зависшие сделки).
  12. `sync_airtable(direction="both")` — запуск двусторонней синхронизации CRM ↔ Airtable (`SoftUGC Suite`) и получение отчёта.
- **Выход:** агент подключается за 1 клик (кнопка «MCP для агентов» в топбаре CRM копирует готовый JSON для Claude Desktop / Cursor / Antigravity) и имеет полный аналитический и операционный доступ к CRM.
