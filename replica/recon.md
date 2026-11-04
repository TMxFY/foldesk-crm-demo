# Recon map: свой Telegram-CRM (веб)

Scope: срез, не весь crmchat. Ядро: подключить свой аккаунт Telegram, найти лид, карточка и статусы, напоминание, передать заметку. Платформа: веб. Не клонируем рассылки, прогрев и парсинг групп.
For: соло-продавец, который ведёт сделки в Telegram.
Date: 2026-11-04
Референс: публичные страницы crmchat.ai (Hintsflow Inc., d/b/a CRMchat; раньше Hints). Это карта чужих паттернов, не бриф копировать их тексты.

## Sources

| # | source | URL | notes |
| --- | --- | --- | --- |
| 1 | marketing EN | https://crmchat.ai/ | позиционирование: CRM + outreach + research; вход app.crmchat.ai и бот |
| 2 | marketing RU | https://crmchat.ai/ru/ | те же блоки по-русски; ядро для соло — CRM, не рассылки |
| 3 | pricing | https://crmchat.ai/pricing | карточки тарифов в HTML не отдались (JS). Meta: free→enterprise, trial 1 неделя. Changelog: гибкая цена с 1–2 аккаунтов; раньше $499/мес. Help: чат+карточка «с Pro» |
| 4 | Telegram CRM | https://crmchat.ai/telegram-crm | воронка, карточка, заметки, напоминания, дайджест, кастом-поля, дубли, папки, forward, QR, ручной ввод |
| 5 | unified inbox | https://crmchat.ai/telegram-unified-inbox | один список чатов, фильтры, карточка рядом с перепиской |
| 6 | changelog | https://crmchat.ai/changelog | inbox v3, статусы аккаунта, папки как свойство, связь лид↔аккаунт, bulk edit, API |
| 7 | help index | https://crmchat.ai/help-center | оглавление; дальше только статьи про CRM-срез, не outreach |
| 8 | help intro | https://crmchat.ai/help-center/intro-to-crmchat | цикл prospect→outreach→optimize; для нас только capture + optimize |
| 9 | help add leads | https://crmchat.ai/help-center/adding-crm-leads | 5 способов добавить лид; кнопки «+», My Leads, поля формы |
| 10 | help pipeline | https://crmchat.ai/help-center/customizing-pipeline-filters | стадии по умолчанию, Status как свойство, фильтры |
| 11 | help views | https://crmchat.ai/help-center/custom-pipeline-views-telegram-crm | сохранённые виды воронки |
| 12 | help notes | https://crmchat.ai/help-center/reminders-notes-collaboration | заметка, напоминание, дайджест, Open Chat |
| 13 | help workspaces | https://crmchat.ai/help-center/managing-workspaces | воркспейс, инвайт по @username, роли Member/Admin |
| 14 | help TG accounts | https://crmchat.ai/help-center/faq-telegram-accounts | покупка чужих аккаунтов — skip; путь Outreach > Telegram Accounts |
| 15 | developers marketing | https://crmchat.ai/developers | REST, webhooks, telegram.username / telegram.id как поля |
| 16 | API index | https://developers.crmchat.ai/llms.txt | сущности: org, workspace, contact, telegram-account, properties, dialogs; campaigns — skip |
| 17 | API auth | https://developers.crmchat.ai/docs/authentication | Bearer `sk_…`, ключи в app.crmchat.ai/mini-app/settings/api-keys |
| 18 | API accounts | https://developers.crmchat.ai/docs/api/telegram-accounts | статусы active/offline/unauthorized/banned/frozen |
| 19 | API properties | https://developers.crmchat.ai/docs/custom-properties | типы полей, ключи `custom.*`, scope org/workspace |
| 20 | lead research | https://crmchat.ai/telegram-lead-research | парсер/lookalike/phone→username — skip для клона |
| 21 | privacy | https://crmchat.ai/privacy | Hintsflow Inc.; логин Google/Apple/Telegram; Stripe. Отдельный ToS в навигации не найден |

Не открывали: app.crmchat.ai (логин), JS-бандлы, приватные API. HTML `developers.crmchat.ai/docs` и `contacts.get` почти пустые; схема контакта — из help + llms.txt, confidence ниже.

Terms: публичные страницы не запрещают изучать UX. Аккаунт продукта для сборки конкурента не использовали. Покупка чужих TG-аккаунтов и парсинг участников — вне среза.

## Core loop

Соло-продавец подключает свой Telegram, видит чаты, находит человека, ставит статус на карточке, пишет заметку и ставит напоминание — без ухода в таблицу.

## Screens

Имена — наши, на русском. Маршруты оригинала — как описаны в help/changelog, не из бандлов.

| ID | screen | route / how to reach | purpose | key components | states seen |
| --- | --- | --- | --- | --- | --- |
| S01 | Вход | app.crmchat.ai или бот `@crmchat_crm_bot` / `@hints_crm_bot` | начать сессию | кнопка Start у бота, веб-логин (Google/Apple/Telegram по privacy) | публично: CTA Try for Free. Пустой/ошибка входа не видели |
| S02 | Подключить Telegram | настройки / список аккаунтов (у них: Outreach > Telegram Accounts) | привязать свой аккаунт | телефон+код; в оригинале ещё Session+JSON (не для соло) | guess: ввод телефона, код, 2FA, ошибка сессии. Статусы API: active, offline, unauthorized, banned, frozen |
| S03 | Список аккаунтов | после S02 | один свой аккаунт, статус, отключить | строка аккаунта, бейдж статуса, доступ private/workspace | filled: список. empty: нет аккаунта. error: unauthorized — перелогин |
| S04 | Входящие | левое меню / «чат» | все лички с подключённого аккаунта | список чатов newest-first, поиск имени/@, фильтры «ждёт ответа» / непрочитанные / группы / без звука | empty, loading (changelog: своя инфра, «в 10 раз быстрее»), filled, realtime. Classic Chat vs Unified — у них переключатель |
| S05 | Переписка | клик по чату в S04 | читать и ответить | лента, ответ, удаление, эмодзи, копирование; шапка: с какого аккаунта | filled; mark unread; opening may mark read (FAQ inbox, без ответа в HTML) |
| S06 | Карточка рядом с чатом | «Open Lead in CRM» / «ещё не лид — добавить» | контекст сделки не теряя чат | профиль, свойства, заметки, задачи; кнопка в CRM | нет карточки / есть карточка. Help: чат+карточка с Pro |
| S07 | Воронка | Pipeline, dropdown вида | статусы колонок | канбан, стадии, фильтр, шестерёнка вида | empty колонки, filled, filter applied. Default: Lead, Conversation, Proposal, Negotiation, Won |
| S08 | Список лидов | тот же раздел, вид list | найти лида по полям | таблица/список, «Display in list view», поиск | empty, filled, filtered |
| S09 | Карточка лида | клик по карточке / Open Chat | статус, заметка, напоминание, чат | шапка имени, статус, свойства, таймлайн, Add note, Add reminder, Open Chat, чекбокс задачи | empty notes, filled timeline |
| S10 | Добавить лида | «+» справа внизу CRM | создать карточку | меню: синк папки / вручную / QR (мини-апп) | форма: Имя, Описание, URL, Email, Phone, Telegram; Create lead |
| S11 | Синк папок Telegram | S10 → Telegram Folder Sync | затянуть свои деловые чаты | список папок, тумблер, выбор воркспейса, Enable Sync | ошибка: в папке стоят Chat Types (Contacts/Groups/…) или invite links — help просит убрать |
| S12 | Заметка | S09 → Add note | передать контекст себе (или коллеге позже) | текст, Create note, появляется в таймлайне | empty field, saved. @mentions в оригинале «to be released» |
| S13 | Напоминание | S09 → Add reminder | не забыть следующий шаг | поля REMINDER (заголовок), DATE, TIME, DESCRIPTION, Create reminder | due / done / postpone |
| S14 | Свойства и стадии | Settings → Custom properties → Status | свои колонки и поля | типы: text, textarea, single-select, multi-select, user-select, url, email, tel, amount; опции с цветом | список свойств org vs workspace |
| S15 | Сохранённый вид | Filter → view settings → Save as new view | несколько воронок без второй БД | имя вида, Save for everyone, Reset | default pipeline с залипшим фильтром — tip в help |
| S16 | Настройки | шестерёнка / workspace name | воркспейс, инвайт, API-ключи | переключатель воркспейса, Invite team, Role Member/Admin | соло: один воркспейс. Инвайт падает, если человек не нажал Start у бота |
| S17 | Дайджест в Telegram | утро, бот | список задач на день | сообщение бота | empty day / list of tasks |
| S18 | Пуш задачи | в срок напоминания | complete / postpone / View lead | кнопки бота | due now |
| S19 | Форвард в бота | из обычного Telegram | быстрый захват лида | Forward → бот; сообщение становится заметкой | duplicate? не описано |
| S20 | QR-захват | мини-апп «+» → Scan QR | офлайн-встреча | камера | только mobile mini-app; веб-срез — could |

Не снимали `replica/screens/`: продукта за логином нет, маркетинговые лендинги в клон не кладём.

## Flows

```
F01 Подключить свой Telegram
    S01 -> S02 -> S03 -> S04
    happy path clicks: ~5 (вход, телефон, код, подтверждение, список чатов)
    edge: 2FA, код истёк, unauthorized/frozen/banned, аккаунт offline и сам переподключается

F02 Найти лида
    S04 поиск по имени/@  ИЛИ  S08 список  ИЛИ  S07 воронка
    happy path clicks: 2 (поиск, открыть)
    edge: нет совпадений; один человек писал на несколько аккаунтов — у них «одна строка»; группы vs лички

F03 Карточка и статус
    S04/S08 -> S09 -> смена Status -> колонка S07 обновляется
    happy path clicks: 3
    edge: нет карточки (сначала «добавить в CRM»); дубль; статус — это custom property, не отдельная сущность

F04 Передать заметку
    S09 -> S12 -> таймлайн
    happy path clicks: 3 (Add note, текст, Create note)
    edge: пустой текст; форвард в бота тоже пишет заметку (S19)

F05 Напоминание
    S09 -> S13 -> S18 (в срок) -> Complete | postpone | View lead
    alt: NL-команда в чате «… напомни в четверг в 15:00» -> confirm Update
    happy path clicks: 4
    edge: без даты; день без задач (S17 пустой); чекбокс на карточке

F06 Захват из своих чатов
    S10 -> S11 синк папки  ИЛИ  S19 форвард  ИЛИ  S10 вручную
    happy path clicks: 4 (папка) / 3 (форма)
    edge: папка с dynamic Chat Types не синкается; дубли при повторном синке

F07 Чат и сделка на одном экране
    S04 -> S05 -> S06 -> правка статуса/заметки
    happy path clicks: 3
    edge: чат без лида; Open Chat с карточки; у них фича за Pro
```

Клики happy path ядра (F01 один раз + F02–F05): примерно 12–14. Это число, которое клон должен бить.

## Components

| component | variants | states | used on |
| --- | --- | --- | --- |
| Кнопка | primary, secondary, ghost, danger | default, disabled, loading | все |
| Поиск | чаты / лиды | empty, typing, no results | S04, S08 |
| Список чатов | inbox, classic | empty, loading, realtime, unread badge | S04 |
| Фильтр-чипы | ждёт ответа, непрочитанные, аккаунт, группы, без звука | on/off | S04 |
| Канбан-колонка | стадия | empty, filled, drop | S07 |
| Карточка лида | боковая панель у чата / полный экран | нет лида, заполнена | S06, S09 |
| Поле свойства | text, textarea, select, url, email, tel, amount, user | empty, invalid, required | S09, S14 |
| Таймлайн | заметка, задача, системное | empty, filled | S09 |
| Модалка формы | лид / заметка / напоминание / вид | open, validation | S10–S13, S15 |
| FAB «+» | меню способов добавить | — | S07, S08, S10 |
| Тумблер папки | sync on/off | disabled если папка «грязная» | S11 |
| Бейдж статуса аккаунта | active, offline, unauthorized, banned, frozen | — | S03 |
| Сообщение бота | дайджест, due, инвайт, «кто ответил» | кнопки Complete / View lead / Accept | S17, S18 |
| Переключатель воркспейса | header | один / много | S16 |
| Тост | успех, ошибка, дубль | — | guess, S10 |

## Inferred data model

```
User            id, name, email?, telegram_username, auth_providers (google|apple|telegram)
                evidence: privacy §2, help workspaces
                confidence: high

Workspace       id, org_id, name
                evidence: API workspaces.*, help managing-workspaces
                confidence: high

WorkspaceMember workspace_id, user_id, role (admin | member | chatter)
                evidence: help Invite team; inbox: роль Chatter не удаляет сообщения
                confidence: medium (chatter — только inbox-страница)

TelegramAccount id, workspace_id, phone?, username?, telegram_user_id,
                status (active | offline | unauthorized | banned | frozen),
                access (workspace | private | selected_members),
                custom_properties
                evidence: API telegram-accounts; changelog private accounts, tagging
                confidence: high
                guess: session storage на их стороне; клон — своя сессия, не их Session+JSON

Contact         id, workspace_id, name, description, telegram_username, telegram_id,
                email, phone, url, owner_user_id, associated_account_id,
                folder_ref?, duplicate_of_id?, properties (map)
                evidence: help adding-crm-leads; developers: telegram.username/id first-class;
                changelog Lead-Account Association, folder as custom property, duplicate checks
                confidence: high на идентификаторах и ручных полях; folder_ref medium

PropertyDef     key (custom.* или built-in Status), object_type (contacts | telegram-accounts),
                type, name, options[{label,value,color}], required, display_in_list,
                scope (organization | workspace)
                evidence: API custom-properties; help pipeline Status
                confidence: high
                built-in Status options default: Lead, Conversation, Proposal, Negotiation, Won

PipelineView    id, workspace_id, name, filters, visibility (everyone)
                evidence: help custom-pipeline-views
                confidence: high

Note            id, contact_id, author_id, body, source (manual | forwarded_message), created_at
                evidence: help notes; adding-crm-leads forward→note
                confidence: high

Reminder        id, contact_id, title, due_at, description, status (open | done | postponed)
                evidence: help reminders; bot Complete / postpone / View lead
                confidence: high

Dialog          id, telegram_account_id, peer_id, username, last_message_at,
                unread_count, last_from_other_side, muted, is_group, contact_id?
                evidence: inbox landing; API dialogs.find
                confidence: medium (поля фильтров с лендинга; схема API HTML пустая)

Message         id, dialog_id, telegram_message_id, direction, text, sent_at
                evidence: changelog native replies/delete/emoji/copy; не логировали API
                confidence: guess

Activity        id, contact_id, type (note|reminder|status|system), created_at
                evidence: «timeline» на карточке
                confidence: medium
```

Relationships: User n-n Workspace через Member. Workspace 1-n TelegramAccount, Contact, PropertyDef, PipelineView. Contact n-1 owner User, n-1 associated TelegramAccount, 1-n Note, 1-n Reminder. Dialog n-1 TelegramAccount, 0-1 Contact. Contact.Status → PropertyDef option (колонка воронки).

## Feature matrix

See `features.csv`. Must: 18, should: 12, could: 8, skip: 12.

## Out of scope (cannot or should not be cloned)

- Массовые рассылки, цепочки, spintax, мультиаккаунт-отправка, аналитика кампаний — skip, не ядро соло-CRM и риск ToS Telegram.
- Прогрев аккаунтов и покупка чужих номеров — skip, не «свой аккаунт».
- Парсинг участников групп, Chrome scraper, lookalike, phone→username, готовые Web3-базы — skip, чужие данные и их research-продукт.
- Их сеть клиентов, кейсы, стикерпак, тексты лендинга — не копировать в продукт.
- Прокси Telegram TL methods через их API — это их платформа, не UX среза.
- Командные round-robin, агентства, PPV-бот, AI sales agent, sync подписчиков канала — вне соло-среза.

## Size

Screens 20 (из них 16 веб-ядро), flows 7, entities ~12.  
Hard parts: (1) сессия Telegram user-account (OTP/2FA, reconnect, статусы banned/frozen); (2) realtime входящие + карточка на одном экране; (3) синк папок и дедуп лид↔диалог.  
Size: **M** (несколько недель) на срез соло-продавца. Весь crmchat — XL, не берём.

Следующий шаг: `/replica-architect` — схема и экраны клона на русском, без outreach.
