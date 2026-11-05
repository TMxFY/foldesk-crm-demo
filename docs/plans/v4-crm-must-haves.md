# План v4: Ключевые боевые функции Foldesk CRM

## Пояснение по пункту 2 (Веб-Telegram `/a/` и Веб-WhatsApp)
Так как в `#inbox` уже встроен оригинальный клиент Telegram Web A (`/a/` через [`app/tg_web_proxy.py`](file:///Users/timofei/dev/Python/new era/crmtg/app/tg_web_proxy.py)) и WhatsApp-шлюз, **голосовые, кружочки, медиафайлы, галочки прочтения и редактирование сообщений уже работают нативно внутри веб-клиента**. Дублировать собственный медиа-плеер в БД не нужно.

Что реально нужно оставить из пункта 2 для связки «Веб-мессенджер + CRM»:
- **Панель быстрых шаблонов (Snippets / Быстрые ответы)** прямо в правой колонке рядом с чатом (`#s06` и под полем ввода `#reply-form`): 1 клик по плашке `/кейсы`, `/прайс`, `/реквизиты` копирует готовый текст (с подстановкой `{name}`, `{company}` открытого лида) в буфер обмена для вставки в `iframe` Веб-Telegram и сразу подставляет в CRM-поле ответа.

---

## Блок 1. Фоновый автопилот рассылок, Spintax и Auto-Stop
**Вход → Система → Выход:**
1. **Spintax-движок в [`app/outreach.py`](file:///Users/timofei/dev/Python/new era/crmtg/app/outreach.py):**
   - В `render_template()` добавить раскрытие конструкций `{Привет|Добрый день|Здравствуйте}` (включая вложенные варианты) после подстановки переменных `{name}`, `{company}`, `{hook}`.
2. **Фоновый планировщик (`autopilot_loop` в [`app/main.py`](file:///Users/timofei/dev/Python/new era/crmtg/app/main.py) / [`app/outreach.py`](file:///Users/timofei/dev/Python/new era/crmtg/app/outreach.py)):**
   - Фоновая asyncio-задача проверяет каждые 30 секунд активные кампании (`status = 'running'`).
   - Сверяет текущее время с расписанием [`outreach_schedule`](file:///Users/timofei/dev/Python/new era/crmtg/app/db.py#L324-L329) (день недели + часовой пояс + окно `start`..`end`).
   - Проверяет суточный остаток `daily_limit - sent_today` на привязанных аккаунтах и отправляет очередное сообщение с рандомным джиттером, обновляя статус получателя.
   - Тумблер в UI (`#outreach`): «Автопилот по расписанию: ВКЛ / ВЫКЛ» + статус следующего запуска.
3. **Авто-остановка цепочки и перевод в `talking` при ответе:**
   - Усилить [`stop_outreach_on_reply()`](file:///Users/timofei/dev/Python/new era/crmtg/app/inbox.py#L69-L174): при любом входящем сообщении (`apply_incoming`, `sync_inbox`, `remember_dialogs`) писать событие в таймлайн карточки (`lead_events`), переводить `campaign_recipients.status = 'replied'`, а `leads.status` из `new`/`waiting` в `talking`.

---

## Блок 2 (адаптированный). Быстрые ответы (Snippets) рядом с Веб-Telegram и чатом
**Вход → Система → Выход:**
1. **Таблица `snippets` в [`app/db.py`](file:///Users/timofei/dev/Python/new era/crmtg/app/db.py):**
   - Поля: `id`, `shortcut` (например, `/price`), `title`, `body` (с поддержкой `{name}`, `{company}`), `created_at`.
2. **API в [`app/main.py`](file:///Users/timofei/dev/Python/new era/crmtg/app/main.py):**
   - `GET /api/snippets`, `POST /api/snippets`, `DELETE /api/snippets/{id}`.
3. **UI в Инбоксе (`#s05` и `#s06`):**
   - Компактная полоса чипов быстрых ответов над/под полем ввода и в инспекторе карточки рядом с `iframe` Telegram Web: клик по шаблону рендерит `{name}`/`{company}` текущего лида, копирует в буфер (с тостом «Скопировано для вставки в Telegram Web») и вставляет в `#reply-text`, плюс автодополнение по вводу `/` в поле ответа.

---

## Блок 3. Честные фильтры Инбокса и статус «Непрочитанное»
**Вход → Система → Выход:**
1. **Серверная фильтрация `GET /api/inbox` в [`app/inbox.py`](file:///Users/timofei/dev/Python/new era/crmtg/app/inbox.py):**
   - Параметры `chip` (`waiting` → `last_from_peer = 1`, `our_turn` → `last_from_peer = 0`, `unread` → `unread_count > 0`, `groups` → `is_group = 1`, `muted` → `muted = 1`), `account_id`, `provider` (`telegram` / `whatsapp`), `status` (стадия лида).
2. **Маршрут «Отметить непрочитанным / прочитанным»:**
   - `POST /api/inbox/dialogs/{dialog_id}/unread` (устанавливает `unread_count = MAX(1, unread_count)` и `last_from_peer = 1`) и сброс `unread_count = 0` при открытии диалога (`POST /api/inbox/dialogs/{dialog_id}/read`).
3. **Счётчики на чипах в `#s04`:**
   - Показывать реальное число диалогов на кнопках «Ждёт ответа (N)» и «Непрочитанные (M)».

---

## Блок 4. Деньги в воронке (`amount`), Drag-and-Drop Канбана и свои стадии
**Вход → Система → Выход:**
1. **Бюджет сделки в таблице `leads` ([`app/db.py`](file:///Users/timofei/dev/Python/new era/crmtg/app/db.py)):**
   - Добавить колонки `amount INTEGER NOT NULL DEFAULT 0` и `currency TEXT NOT NULL DEFAULT 'USD'` в `leads`.
   - Показывать бюджет на карточке Канбана (`$2,500`) и выводить **общую сумму колонки** в шапке каждой стадии (например: `Разговор · 8 · $14,200`).
2. **HTML5 Drag-and-Drop карточек в [`app/static/app.js`](file:///Users/timofei/dev/Python/new era/crmtg/app/static/app.js):**
   - Перетаскивание карточки `.lead-card` между колонками `.col` вызывает `PATCH /api/leads/{id}` со сменой `status`, мгновенно пересчитывает счётчики и суммы колонок и пишет запись в историю сделки.
3. **Кастомные названия и цвета стадий воронки (`pipeline_stages`):**
   - Таблица `pipeline_stages (key, label, position, color)` с дефолтными 5 стадиями и возможностью переименовать любую стадию или добавить свою (сняв жёсткий `CHECK` на фиксированные 5 строк или расширив справочник стадий).

---

## Блок 5. Массовые операции (Bulk Actions), Слияние дублей и Экспорт в CSV
**Вход → Система → Выход:**
1. **Массовые действия (`POST /api/leads/bulk`):**
   - Чекбоксы на карточках/строках лидов + кнопка «Выбрать все в фильтре».
   - Плавающая панель действий при выборе ≥1 лида:
     - Сменить стадию (`status`)
     - Назначить папку (`folder_id`)
     - Зачислить в кампанию рассылки (`campaign_id`)
     - Массово удалить (`DELETE`)
2. **Слияние дублей (`POST /api/leads/merge`):**
   - Объединяет `source_lead_id` в `target_lead_id`: переносит все `notes`, `tasks`, `reminders`, `messages`/`dialogs`, `property_values`, `campaign_recipients`, дозаполняет пустые `phone`/`username`/`email`/`amount` и удаляет дубль.
   - Кнопка «Найти и слить дубли» в разделе Лиды.
3. **Экспорт в CSV (`GET /api/leads/export.csv`):**
   - Выгружает текущую выборку лидов (с учётом всех фильтров стадии, папки, канала, бюджета и кастомных полей) в готовый CSV-файл в 1 клик.

---

## Блок 6. Единый таймлайн событий (`Activity Log`) и экран «Фокус на сегодня»
**Вход → Система → Выход:**
1. **Таблица `lead_events` в [`app/db.py`](file:///Users/timofei/dev/Python/new era/crmtg/app/db.py):**
   - Поля: `id`, `lead_id`, `event_type` (`created`, `status_changed`, `outreach_sent`, `reply_received`, `note_added`, `reminder_set`, `merged`, `amount_changed`), `title`, `meta_json`, `created_at`.
   - Автоматическая запись событий при смене статуса, отправке шага кампании, получении ответа, создании заметки и слиянии.
   - Отрисовка хронологической ленты на карточке лида (`#s09` / `#s06`).
2. **Виджет «Мой фокус на сегодня» в разделе Напоминания / Дайджест (`#digest`):**
   - Сводка в 3 колонки:
     1. **Горящие ответы** (диалоги, где `last_from_peer = 1` и клиент ждёт ответа).
     2. **Напоминания и задачи на сегодня / просроченные**.
     3. **Пора дожать** (лиды в стадии `waiting` без касаний > 3 дней).

---

## Блок 7. Сквозная аналитика воронки, кампаний и аккаунтов
**Вход → Система → Выход:**
1. **Эндпоинт `GET /api/analytics/summary`:**
   - **Деньги и воронка:** количество лидов, конверсия из стадии в стадию (`new → talking → waiting → won`), общий Pipeline Value (`$` в работе) и Won Revenue (`$` закрытых сделок).
   - **Эффективность рассылок:** Всего контактов → Отправлено → Ответили (`Reply Rate %`) → Перешли в `talking`/`won`.
   - **Здоровье аккаунтов:** сколько отправлено сегодня каждым аккаунтом (`sent_today / daily_limit`), сколько ответов получено на каждый аккаунт (`Reply Rate` по номеру), статус здоровья.
2. **UI-секция аналитики / сводная панель над Воронкой и в Рассылках:**
   - Наглядные метрики конверсии, Reply Rate и выручки в реальном времени.
