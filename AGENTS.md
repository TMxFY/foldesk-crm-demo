# AGENTS.md — Telegram CRM (`crmtg`)

Карта репозитория. Проектные правила и инварианты лежат в `.agents/skills/crmtg-ops-and-sync/SKILL.md`.

## Сначала
1. Проверь `pwd` и `git status --short`.
2. Прочитай `.agents/skills/crmtg-ops-and-sync/SKILL.md`.
3. Создай отдельную ветку и `git worktree` перед изменениями кода.

## Ключевые контуры
- `app/` — бэкенд, синхронизация реальных папок Telegram, Web Telegram прокси/сессия, MCP (`ingest_discovered_leads`) и интеграция с Airtable.
- `leadgen/` — пайплайн авто-поиска лидов (`tg_pipeline.sh`, `tg_channel_graph.py`, `tg_ad_spy.py`, `yc_ph_leads.py`, `meta_ad_leads.py`, `treg_social_leads.py`), монитор и отклики на TG-вакансии (`job_monitor.py`, `job_reply.py`, `job_jev.py`, `job_cases_bank.md`) и прямая заливка в CRM через MCP (`crm_mcp_push.py`).
- `tests/` и `e2e/` — автотесты (`pytest` и Playwright).
- `deploy/` и `.github/workflows/deploy-vps.yml` — продакшен-деплой и CI/CD на VPS Contabo (`169.58.145.85`, `/opt/crmtg`, `crmtg.service` на `127.0.0.1:18084`, Caddy для `crm.softugc.com` и `crm-169-58-145-85.sslip.io`).

## Железные правила
- Не заливать демо-сиды (например, 300 фейковых лидов) и не импортировать личные чаты из папок Telegram в прод-БД без явной команды.
- Web Telegram и синхронизация папок должны работать автономно на сервере VPS Contabo (`169.58.145.85`), а не через локальный туннель с Мака.
