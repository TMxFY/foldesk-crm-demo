# Оркестрация

Цель: v1 CRM — подключение аккаунта Telegram, поиск, лиды, напоминания.

Решения:

- Telethon по образцу `~/.claude/skill-telegram`, свои сессии в `data/sessions/`.
- Без рассылок, прогрева и парсинга групп.
- План: `docs/plans/v1-leads.md`.

Статус:

- План записан: `docs/plans/v1-leads.md`.
- Одиночный implementer остановлен: пользователь попросил workflow.
- Workflow `crm-v1`: сборка, четыре сравнения, скептики, правка только подтверждённого, `verify.sh`.
- Скиллы стоят в `.claude/skills/`: replica-* и web-clone. Порядок replica: recon → architect → design → build.
- replica-recon готов: `replica/recon.md`, `replica/features.csv` (20 экранов, 7 потоков).
- replica-architect готов: `replica/architecture.md`, `replica/schema.sql`. 14 таблиц, 52 маршрута. Стек FastAPI + SQLite + Telethon.
- replica-design готов: `replica/design/`. accent-плейсхолдер `#3f3f46`, contrast.py 54 пары, 0 провалов AA. `app/` не менялся.
- replica-build готов, коммит `3c036d8`. S07 done. S04, S05, S06, S09 и дайджест partial. Повторный `./scripts/verify.sh`: 9 passed.
- replica-test готов. Playwright 40 passed. S2 двойное напоминание починен в `d7e6828`. Открыты S3: BUG-001, BUG-002. S1 нет. `./scripts/verify.sh`: 9 passed.
- replica-diff готов: feature score 52.9, must 9/18, вердикт not shippable. Отчёт `replica/parity.md`. Layout без снимка кабинета не считался.
- Дыры закрыты в `795c176`. Повторный `./scripts/verify.sh`: 13 passed. BUG-001, BUG-002, BUG-003: fixed.
- replica-diff обновлён: feature 56.4, must 11/18, not shippable. Семь must ещё partial.
- Лента диалога в `3ce554c`. Повторный `./scripts/verify.sh`: 16 passed. Playwright по отчёту сборки: 46 passed.
- replica-diff после ленты: feature 59.9, must 13/18, not shippable. Пять must ещё partial.
- S04 живой список: partial, коммит `b17146d`. Повторный `./scripts/verify.sh`: 20 passed. Офлайн-догон не закрыт, yes не ставим.
- Карточка S09 в `61e349e`: заметки, задачи без срока, свои поля. Повторный `./scripts/verify.sh`: 22 passed. Playwright на этом шаге не гоняли.
- replica-diff после карточки: feature 65.1, must 14/18, not shippable. Остались список входящих, health/reconnect, бот и соцлогин.
- Список входящих yes в `40b6e60`. Повторный `./scripts/verify.sh`: 23 passed.
- replica-diff после списка: feature 66.9, must 15/18, not shippable. Остались health/reconnect, дайджест в боте, вход через бота или Google.
- health и reconnect yes в `ffa2f23`. Повторный `./scripts/verify.sh`: 25 passed.
- replica-diff после health: feature 68.6, must 16/18, not shippable. Остались S17 дайджест в боте и S01 вход через бота или Google.
- Дайджест ботом yes в `1e13aab`. Повторный `./scripts/verify.sh`: 32 passed.
- replica-diff после дайджеста: feature 70.3, must 17/18, not shippable. Последний must — S01.
- Вход через бота yes в `dee83a0`. Повторный `./scripts/verify.sh`: 39 passed. Google и Apple нет.
- replica-diff после входа: feature 72.1, must 18/18, not shippable потому что score < 80.
- Синк своих папок yes в `9c8eb20`. Повторный `./scripts/verify.sh`: 41 passed.
- replica-diff после папок: feature 76.7, must 18/18, not shippable, score < 80.
- Форвард боту yes в `aaed4fb`. Повторный `./scripts/verify.sh`: 47 passed.
- replica-diff после форварда: feature 79.1, must 18/18, not shippable, до 80 не хватает одного should.
- Виды воронки yes в `df7c893`. Повторный `./scripts/verify.sh`: 48 passed.
- replica-diff после видов: feature 81.4, must 18/18, shippable по скиллу. Layout-балла нет.
- replica-brand готов в `332e70d`. Рабочее имя Foldesk. Знаки и домены не проверялись. sweep: хвосты только в `web-clone-recon/`.
- replica-launch готов в `54ce0b7`. `/landing` отдельно от кабинета. `listing.py` код 0. Повторный `./scripts/verify.sh`: 49 passed.
- Preflight в `replica/deploy.md`: вердикт stop. verify.sh 49 passed, must 18/18, listing зелёный. Sweep красный из-за `web-clone-recon/` и `tests/test_landing.py`. Политики, удаления аккаунта, favicon и OG нет. В прод не выкладывали.
- web-clone recon готов: скриншоты 1440/768/390, 8 маршрутов, `web-clone-recon/RECON/design-dna.json`. Исходников продукта нет.
- REA 6.8.0 зарегистрирован для grok_build. В этой сессии MCP нет, пока Grok Build не перезапущен. Доказательство главной: `replica/rea-public.json`.
- replica-entrepreneur готов: 4 отзыва Product Hunt, все 5★, жалоб нет. `replica/feedback.md`, `replica/fixes.md`.
- Workflow `crm-v1` завершён. Повторный `./scripts/verify.sh`: 9 passed, 1 warning Starlette/anyio. Коммита нет.
- `app/` снова можно менять после `replica-design`.
- Workflow `crm-v1` не останавливать, пока он сам не допишет код.
