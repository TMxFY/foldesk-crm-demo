---
name: crmtg-ops-and-sync
description: >-
  Use when working in the crmtg repository (Telegram CRM, folder synchronization, Web
  Telegram session on server, MCP server for agent analytics, Airtable lead sync, and
  Contabo VPS production deployment).
---

# Telegram CRM (`crmtg`) — Folders, Server Session, MCP & Airtable Skill

## 1. Синхронизация реальных папок Telegram (Dialog Filters)
- Папки с клиентами в CRM соответствуют **реальным папкам (Dialog Filters) в Telegram**.
- Поддерживай синхронизацию папок **как по одной выбранной папке, так и по нескольким сразу**.
- **Защита от мусорного импорта**: при проверке синхронизации папок **не выкачивай автоматически все чаты из личных папок владельца в таблицу лидов** и не заливай демо-базу («300 тестовых лидов») на прод. В базе прода должны лежать только реальные целевые лиды/папки.

## 2. Серверный рантайм Web Telegram и деплой на VPS Contabo (169.58.145.85)
- Web Telegram и клиентская сессия должны работать **на сервере VPS Contabo (`/opt/crmtg`, `crmtg.service`)**, а не опираться на локальный процесс или локальную БД Мака.
- Никогда не отдавай для командного тестирования временную публичную ссылку (туннель) на локальную базу — выкатывай изменения через GitHub Actions (`deploy-vps.yml`) на VPS (`https://crm.softugc.com` / `https://crm-169-58-145-85.sslip.io`) и проверяй, что Web Telegram и API открываются на серверном URL без ошибок.

## 3. MCP-сервер и интеграция с Airtable
- Поддерживай MCP-интерфейс проекта, чтобы внешние агенты могли запрашивать сквозную статистику по клиентам, папкам и конверсиям.
- Обеспечивай чистую синхронизацию целевых лидов и статусов с Airtable без дублей.

## 4. Mandatory Adversarial Skeptic & Self-Hardening Gate
- Перед любым финальным отчётом владельцу агент обязан полностью прогнать `./scripts/verify.sh` (статические инварианты AST/Sacred Rules, запрет `PYTEST_CURRENT_TEST` split-brain в `app/` и `leadgen/`, Systemd Hollow-Pipe Gate на `deploy/systemd/*.service`, Gate H на запрет фейковых данных/заглушек сессий и наличие антибан-защиты WhatsApp, проверка фронтенда и полный `pytest`).
- Обязательно вызывай свежего независимого субагента `adversarial-skeptic` с прямым вопросом: *"Ты уверен в своей работе? Где пустые трубы, скрытые мутации и слепые зоны harness?"* — и закрывай все найденные им дыры до отчёта.
- После каждого деплоя на VPS Contabo проверяй живой эндпоинт `/api/health?deep=1` (`db_ok`, `tg_lease_ready`, `autopilot_running`, `healthy_accounts`, `running_campaigns`, `pending_step1_total`).
- Любой обнаруженный в проде или на ревью новый класс бага зашивай обратно в статический или регрессионный гейт `scripts/verify.sh`, чтобы станок больше никогда не пропускал этот паттерн.

## 5. Mandatory Bug-to-Harness Ratchet & Pre-Report Self-Verification
- Для каждой новой задачи или найденного в проде бага:
  1. **Сначала зафиксируй падение (`exit 1`):** добавь регрессионный тест в `tests/`, новый статический инвариант в `scripts/verify.sh`, и — для инвариантов данных/БД/статистики — проверку в `scripts/proactive_audit.py` (+ негативный кейс в `--self-test`, который реально падает до фикса).
  2. **Проверь реальные Git-хуки (`scripts/install_hooks.sh`):** перед коммитом и пушем должны быть активны `githooks/pre-commit` (`verify.sh --fast-checks-only`) и `githooks/pre-push` (`verify.sh` + `proactive_audit.py --self-test`).
  3. **Сам проверь до отчёта владельцу:** прогони `bash scripts/verify.sh`, `proactive_audit.py --self-test`, выкати на VPS, выполни живой прогон на боевой БД (`proactive_audit.py --db /opt/crmtg/data/crm.db`) и в живом Chrome через Playwright на `https://crm.softugc.com` со сквозным чтением скриншотов через `read_file`. Никогда не заявляй «готово», пока собственные проверки на локале и проде не зелёные.


