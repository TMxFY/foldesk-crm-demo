# Parity: clone vs CRMchat (срез соло-CRM)

Date: 2026-11-04  
Build: `ffa2f23`  
Source: `replica/features.csv`, `replica/recon.md`, `replica/build-log.md`, `replica/bugs.md`  
Tools: `python3 .claude/skills/replica-diff/parity.py replica/features.csv`  
Checks: `./scripts/verify.sh` и Playwright заново не гоняли  
Original cabinet screens: none (`replica/screens/` отсутствует; в app.crmchat.ai не логинились, лендинги не подставляли)

## Scores

| | |
| --- | --- |
| overall | 70.3 / 100 (только features; layout не усредняли) |
| feature | **70.3** (38 counted, skip 12 не в счёте) |
| must done | **17 of 18** |
| layout | нет пар — балл не выдуман |
| verdict | **not shippable** |

Не shippable: 1 must не `yes` (partial ≠ done) — вход через бота. Открытых S1 нет. BUG-001, BUG-002, BUG-003 — **fixed**. «Better than the original» не ставим: публичных жалоб на оригинал не было, клон ядро не закрывает.

`features.csv` `clone` сверен с `build-log.md`. «Статусы аккаунта и переподключение» — yes. S17 дайджест — yes: одно сообщение в чат оператора. Google, Apple и вход через бота не трогались.

## parity.py (stdout)

```
Parity: 70.3 / 100

features 70.3  (38 counted, must-haves 17 of 18 done)

Not shippable yet: 1 must-have features are not done.

By area, weakest first
- team                           0.0  (2 features)
- integrations                   0.0  (2 features)
- pipeline                      63.6  (5 features)
- leads                         65.2  (10 features)
- inbox                         73.9  (10 features)
- onboarding                    83.3  (3 features)
- reminders                     84.6  (5 features)
- notes                        100.0  (1 features)

Missing, in build order
- [must] onboarding: Вход через веб или Telegram-бота, partial  (S01; локальная страница без бота и без Google/Apple)
- [should] leads: Папка Telegram как свойство лида, no  (changelog 2.4.5)
- [should] leads: Синк своих папок Telegram в CRM, no  (S11 F06; ошибка Chat Types)
- [should] leads: Форвард сообщения боту = лид + заметка, no  (S19)
- [should] pipeline: Фильтры и сохранённые виды воронки, no  (S15)
- [should] reminders: Напоминание фразой в чате, no  (NL + confirm Update)
- [should] inbox: Open Chat с карточки, partial  (открывает чат только если он уже в списке поиска)
- [should] inbox: Отметить чат непрочитанным, partial  (кнопка есть; маршрута нет)
- [should] inbox: Связка лид↔аккаунт с которого писали, partial  (account_id пишется при «В лиды»)
- [should] inbox: Фильтр «ждёт ответа», partial  (чип есть; признака в поиске диалогов нет)
- [should] inbox: Фильтр непрочитанных, partial  (чип есть; признака в поиске диалогов нет)
- [should] pipeline: Кастомные поля и свои названия стадий, partial  (определение и значение на лиде пишутся; свои названия стадий нет)
- [could] inbox: Группы в инбоксе вкл/выкл, no  (лендинг unified inbox)
- [could] integrations: Вебхуки contact.created/updated/deleted, no  (API docs)
- [could] integrations: Публичный REST и ключи sk_, no  (не блокер среза)
- [could] leads: QR с мини-аппа, no  (S20 mobile)
- [could] leads: Скрин LinkedIn в бота, no  (вне ядра)
- [could] pipeline: Массовое редактирование сделок, no  (changelog 2.2.2)
- [could] team: Инвайт по Telegram username и роли, no  (Admin/Member; Chatter на inbox)
- [could] team: Несколько воркспейсов, no  (соло не must)
```

## Layout per screen

imgdiff не запускался: пар оригинал↔клон нет, размеры сравнивать не с чем. `replica/diffs/` пуст.

| screen | original | clone | layout |
| --- | --- | --- | --- |
| S04 inbox 1440 | нет | `replica/clone-screens/S04.png` | нет снимка оригинала |
| S04 inbox 390 | нет | `replica/clone-screens/S04-390.png` | нет снимка оригинала |
| S05 chat | нет | `replica/clone-screens/S05.png` | нет снимка оригинала |
| S06 card+chat | нет | `replica/clone-screens/S06.png` | нет снимка оригинала |
| S07 pipeline | нет | `replica/clone-screens/S07.png` | нет снимка оригинала |
| S09 lead card | нет | `replica/clone-screens/S09.png` | нет снимка оригинала |
| digest | нет | `replica/clone-screens/digest.png` | нет снимка оригинала |
| shell 1440 | нет | `replica/clone-screens/shell-1440.png` | нет снимка оригинала |
| shell 390 | нет | `replica/clone-screens/shell-390.png` | нет снимка оригинала |

## Missing, in build order

Must (блокер сдачи):

- [must] onboarding: Вход через веб или Telegram-бота, partial (S01; локальная страница без бота и без Google/Apple)

Should / could — как в stdout parity.py (синк папок, форвард в бота, свои названия стадий, виды воронки, NL-напоминание, Open Chat, unread, фильтры inbox, REST/webhooks, team).

## Behaviour

Формат: flow · original (публично) · clone · fix/keep. Только recon + build-log + bugs. Живой кабинет не открывали.

| flow | original | clone | fix or keep |
| --- | --- | --- | --- |
| F01 подключить TG | вход app или бот (Google/Apple/Telegram), телефон+код, ~5 кликов; статусы active/offline/unauthorized/banned/frozen; reconnect | локальная страница без бота и соцлогина; телефон, код, 2FA есть; бейдж входа и бейдж health; «Переподключить» пишет health, мёртвая сессия не «активен» | keep health/reconnect; бот/соцлогин — later |
| F02 найти лида | поиск имени/@ в inbox или список/воронка, 2 клика; пустая выдача ≠ пустая база | поиск «В CRM» / «В Telegram» есть; пустой запрос пишет «По запросу пусто» (BUG-001 fixed); список входящих грузится без «Найти» (до 30 личных) | keep |
| F03 карточка и статус | 3 клика; Status = свойство; «добавить в CRM»; дубли | «В лиды» через POST; статус на карточке и 5 колонок S07; drag нет (карточка → S09); дубль = тост API, слияния нет; свои поля `property_defs`/`property_values` пишутся, названия стадий нет | keep воронку без drag; названия стадий — should later |
| F04 заметка | Add note → Create note → таймлайн; форвард в бота тоже пишет note | лента `notes` со временем; непустое `leads.note` видно, если строк `notes` нет, в таблицу само не копируется | keep ленту; S19 should later |
| F05 напоминание | 4 клика; бот complete / postpone / View lead; NL в чате; чекбокс на карточке | дата+время есть; утренний дайджест одним сообщением в чат оператора, кнопки «Сделано» и «К лиду»; postpone остаётся на странице; `tasks.title`/`done_at` по схеме, колонки `due_at` нет; BUG-001/002/003 fixed | keep дайджест и postpone; NL и пуш в срок — later |
| F06 захват | синк папки / форвард / форма Имя+Описание+URL+Email+Phone+Telegram | синка и форварда нет; вручную имя ник телефон заметка url email (S10 done) | keep форму; sync/forward — should later |
| F07 чат+сделка | 3 клика; лента+карточка (у них с Pro); Open Chat с карточки | лента личного чата до 50 сообщений рядом с карточкой; ответ остаётся в ленте; не connected / не личный чат — ошибка; unread и эмодзи в Telegram не ходят; Open Chat только если чат уже в поиске | keep честные ошибки; Open Chat — should later |

Клики ядра у оригинала ~12–14 (F01 раз + F02–F05). У клона F04 лента notes есть, health и reconnect есть, S17 дайджест уходит оператору. Ядро всё ещё не закрыто: вход через бота partial.

### Bugs

- S1: нет
- S2 BUG-003 (двойной «Поставить»): **fixed** in `d7e6828`
- S3 BUG-001: пустой поиск → «По запросу пусто» — **fixed**
- S3 BUG-002: «К лиду» при фильтре CRM открывает карточку через `GET /api/leads/{id}` — **fixed**

## Next five

1. S01: вход через бота / Google / Apple (локальная страница ≠ оригинал). Не делалось вместе с S17.
2. S11/S19: синк папок и форвард в бота (первые should после must).
3. S14: свои названия стадий (поля на лиде пишутся, имена колонок нет).
4. NL-напоминание / Open Chat / unread — should-дыры inbox и reminders.
5. S15: фильтры и сохранённые виды воронки.

S17 дайджест оператору — yes: одно сообщение, пустой день честный, «Сделано» / «К лиду».

S02 health + reconnect — yes в `ffa2f23` (бейдж health и кнопка пишут `accounts.health`).
