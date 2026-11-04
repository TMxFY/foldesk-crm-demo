# PROGRESS

- 2026-11-04 — план v1 записан в `docs/plans/v1-leads.md`. Код ещё не проверен.
- 2026-11-04 — v1 лиды: SQLite, API, FakeGateway, страница. `./scripts/verify.sh`: 5 passed.
- 2026-11-04 — v1: срок в UTC, дубль telegram id без аккаунта, повтор кода/пароля, connect не сбрасывает pending_password. `./scripts/verify.sh`: 9 passed.
- 2026-11-04 — replica-design: `replica/design/` токены, тема, 14 спек, preview. `contrast.py`: 54 пары, 0 AA, код 0. Фича не закрыта.
- 2026-11-04 — replica-build: оболочка и секции S04 S05 S06 S07 S09 и дайджест. `./scripts/verify.sh`: 9 passed. Фича не закрыта.
- 2026-11-04 — replica-test: план и e2e ядра. Playwright 40 passed, 0 failed. S2 двойное напоминание починен в d7e6828. S3 открыты: пустой поиск пишет «Лидов пока нет», «К лиду» при фильтре не открывает карточку. `./scripts/verify.sh`: 9 passed. Фича не закрыта.
- 2026-11-04 — parity gaps: BUG-001/002, postpone, ручной лид, задача=напоминание, одно исходящее, кэш диалогов. `./scripts/verify.sh`: 13 passed. Playwright 45 passed. Фича не закрыта.
- 2026-11-04 — replica-build: лента личного диалога в S05 рядом с S06. `./scripts/verify.sh`: 16 passed. Playwright 46 passed. Фича не закрыта.
- 2026-11-04 — replica-build S04: живые входящие, SSE `/api/inbox/events`. `./scripts/verify.sh`: 20 passed. partial: сокет есть, полный офлайн-догон нет. Фича не закрыта.
- 2026-11-04 — replica-build S09: заметки, задачи без due_at, свои поля. `./scripts/verify.sh`: 22 passed. Свои названия стадий нет. Фича не закрыта.
