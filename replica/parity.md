# Parity: clone vs CRMchat (срез соло-CRM)

Date: 2026-11-04  
Build: `aaed4fb`  
Source: `replica/features.csv`, `replica/recon.md`, `replica/build-log.md`, `replica/bugs.md`  
Tools: `python3 .claude/skills/replica-diff/parity.py replica/features.csv`  
Checks: parity.py this pass; `./scripts/verify.sh` и Playwright заново не гоняли  
Original cabinet screens: none (`replica/screens/` нет; в app.crmchat.ai не логинились; лендинги не подставляли)

## Scores

| | |
| --- | --- |
| overall | 81.4 / 100 (только features; layout не усредняли) |
| feature | **81.4** (38 counted, skip 12 не в счёте) |
| must done | **18 of 18** |
| layout | нет пар — балл не выдуман |
| verdict | feature ≥ 80; layout без пар |

Все must `yes`. Открытых S1 нет. BUG-001, BUG-002, BUG-003 — **fixed**. Feature score 81.4. «Better than the original» не ставим: пар экранов нет.

`features.csv` `clone` сверен с `build-log.md`. Новый yes — S15: фильтр воронки по стадии и папке, вид сохраняется локально, выбор применяет фильтр, «Сбросить» возвращает всех лидов. «Save for everyone» нет. Другие partial не поднимались. NL не делался.

## parity.py (stdout)

```
Parity: 81.4 / 100

features 81.4  (38 counted, must-haves 18 of 18 done)

By area, weakest first
- team                           0.0  (2 features)
- integrations                   0.0  (2 features)
- inbox                         73.9  (10 features)
- pipeline                      81.8  (5 features)
- reminders                     84.6  (5 features)
- leads                         91.3  (10 features)
- onboarding                   100.0  (3 features)
- notes                        100.0  (1 features)

Missing, in build order
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

imgdiff не запускался: пар оригинал↔клон нет. `replica/screens/` и `replica/diffs/` нет. Layout-балл не ставили и в overall не мешали.

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

Must: все 18 `yes`. Блокера must нет. Score 81.4 (70/86 веса). S15 закрыт: `no`→`yes`.

Первые should:

- [should] pipeline: Фильтры и сохранённые виды воронки, yes (S15, локально; Save for everyone нет)
- [should] reminders: Напоминание фразой в чате, no (NL + confirm Update)

Дальше should partial: Open Chat, unread-маршрут, связка лид↔аккаунт, чипы inbox, свои названия стадий. Could — как в stdout.

## Behaviour

Формат: flow · original (публично) · clone · fix/keep. Только recon + build-log + bugs. Живой кабинет не открывали.

| flow | original | clone | fix or keep |
| --- | --- | --- | --- |
| F01 подключить TG | вход app или бот (Google/Apple/Telegram), телефон+код, ~5 кликов; статусы active/offline/unauthorized/banned/frozen; reconnect | веб: телефон+код+2FA; кнопка «Войти через бота»: пустой `BOT_TOKEN` — ошибка; `/start` из `BOT_CHAT_ID` помечает локальную сессию, чужой chat id не открывает; повторный заход видит вход; Google/Apple нет; бейдж health; «Переподключить» пишет health, мёртвая сессия не «активен» | keep веб+`/start`; Google/Apple — later, строку не откатывать |
| F02 найти лида | поиск имени/@ в inbox или список/воронка, 2 клика; пустая выдача ≠ пустая база | поиск «В CRM» / «В Telegram» есть; пустой запрос пишет «По запросу пусто» (BUG-001 fixed); список входящих грузится без «Найти» (до 30 личных) | keep |
| F03 карточка и статус | 3 клика; Status = свойство; «добавить в CRM»; дубли | «В лиды» через POST; статус на карточке и 5 колонок S07; drag нет (карточка → S09); дубль = тост API, слияния нет; свои поля `property_defs`/`property_values` пишутся, названия стадий нет | keep воронку без drag; названия стадий — should later |
| F04 заметка | Add note → Create note → таймлайн; форвард в бота тоже пишет note | лента `notes` со временем; непустое `leads.note` видно, если строк `notes` нет, в таблицу само не копируется; форвард из `BOT_CHAT_ID` пишет `notes.source=forwarded` | keep ленту и S19 |
| F05 напоминание | 4 клика; бот complete / postpone / View lead; NL в чате; чекбокс на карточке | дата+время есть; утренний дайджест одним сообщением в чат оператора, кнопки «Сделано» и «К лиду»; postpone на странице; `tasks.title`/`done_at` по схеме, колонки `due_at` нет; BUG-001/002/003 fixed | keep дайджест и postpone; NL — later |
| F06 захват | синк папки / форвард / форма Имя+Описание+URL+Email+Phone+Telegram | синк выбранной своей папки: личные диалоги становятся лидами с `folder_id`, повтор не плодит дубли; на карточке имя папки, смена или очистка; папка с Chat Types или инвайтом не синкается; группы и каналы не импортируются; форвард из `BOT_CHAT_ID` создаёт лид и заметку, повтор того же id не плодит лид; вручную имя ник телефон заметка url email (S10 done) | keep синк, форму и форвард |
| F07 чат+сделка | 3 клика; лента+карточка (у них с Pro); Open Chat с карточки | лента личного чата до 50 сообщений рядом с карточкой; ответ остаётся в ленте; не connected / не личный чат — ошибка; unread и эмодзи в Telegram не ходят; Open Chat только если чат уже в поиске | keep честные ошибки; Open Chat — should later |

Клики ядра у оригинала ~12–14 (F01 раз + F02–F05). У клона F01 веб и `/start` есть, F04 лента notes есть, health и reconnect есть, S17 дайджест уходит оператору, S11 синк своих папок и папка на карточке есть, S19 форвард пишет лид и заметку, S15 сохраняет вид воронки локально. Must закрыты. Score 81.4 из-за остальных should.

### Bugs

- S1: нет
- S2 BUG-003 (двойной «Поставить»): **fixed** in `d7e6828`
- S3 BUG-001: пустой поиск → «По запросу пусто» — **fixed**
- S3 BUG-002: «К лиду» при фильтре CRM открывает карточку через `GET /api/leads/{id}` — **fixed**

## Next five

1. S15 закрыт: фильтры и сохранённые виды воронки (`yes`, локально).
2. NL-напоминание в чате.
3. Open Chat с карточки, если чата нет в поиске.
4. Отметить чат непрочитанным: маршрута нет.
5. Связка лид↔аккаунт с которого писали.
