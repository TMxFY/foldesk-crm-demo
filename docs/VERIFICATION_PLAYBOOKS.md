# Verification Playbooks — Telegram CRM (`crmtg`)

Исполняемые методики независимой верификации (синхронизированы со `scripts/verify.sh` и `scripts/proactive_audit.py`).

## Playbook 1: Colleague Bot Reply-Alert Eligibility & Anti-Echo Gate
Когда личный или рабочий аккаунт подключён в `accounts` по MTProto, `crmtg` слушает `events.NewMessage(incoming=True)`. Любая дыра в фильтрации превращает входящие личные чаты, команды и ответы самого `@softugccollegabot` в бесконечное самоэхо алертов `You received a response from ...`.

### Обязательные независимые перепроверки (3-of-3):
1. **Static AST & Boundary Gate (`./scripts/verify.sh`)**:
   - В `app/inbox.py` запрещён триггер `dlg_row is not None` внутри `base_notify_trigger` (т.к. `apply_incoming` создаёт `dialogs` строку до вызова `stop_outreach_on_reply`).
   - В `app/inbox.py`, `app/digest.py` (`send_lead_reply_notification`) и `app/telegram_gateway.py` (`_load_private`, `_private_payload`) обязаны стоять гарды против:
     - `bot_token` user_id (`extract_bot_user_id_from_token`) и `@softugccollegabot`
     - любых Telegram-ботов (`User.bot == True`, `is_bot`, `*_bot`)
     - `User.is_self == True` / `self_id` / `bot_chat_id` (Saved Messages и сам оператор)
     - подключённых аккаунтов `accounts` и участников команды (`INTERNAL_TEAM_USERNAMES`)
     - закрытых сделок (`leads.status IN ('won', 'lost')`, `Signed` / `Cancelled`)
     - не-лидов (`lead_id IS NULL` и `0` совпадений в `leads` и `campaign_recipients`).
2. **DB State & Self-Healing Audit (`python3 scripts/proactive_audit.py --db <crm.db>`)**:
   - Проверяет все записи в `bot_reply_notifications` и `leads` реальной БД, возвращая `exit 1` при `false_notification_count > 0` или `bot_username_lead_count > 0`.
3. **Live Deep Health Gate (`/api/health?deep=1`)**:
   - На проде (`http://127.0.0.1:18084/api/health?deep=1`) проверяет `"db_ok": true`, `"reply_alert_guard_ok": true` и `"false_reply_alerts_24h": 0`.

## Playbook 2: Sniper ICP Hygiene & Server Leadgen Zero-Hollow-Pipe Gate
Гарантирует, что автопилот никогда не стреляет по не-ICP мусору (агентства, фрилансеры, пустые бесконтактные карточки, битые заголовки), а серверный лидген на VPS Contabo не превращается в «пустую трубу» из-за пропавших `SCRAPER_TG_SESSION`, `/usr/local/bin/treg`, LLM-ключей или зависших стадий `leadgen_stage_errors`.

### Обязательные независимые перепроверки (3-of-3):
1. **Static Harness & Systemd Environment Gate (`./scripts/verify.sh` — Gate E)**:
   - В `app/main.py` функция `api_health` обязана возвращать `icp_hygiene_ok` и `leadgen_ready`, а `heal_crm_hygiene(conn)` вызывается при старте `create_app` и перед каждым тиком `autopilot_loop`.
   - В `scripts/proactive_audit.py` и `.github/workflows/deploy-vps.yml` обязана стоять проверка `icp_hygiene_ok` и `leadgen_ready` (деплой откатывается при `False`).
   - В `deploy/systemd/crmtg.service` и `deploy/systemd/crmtg-leadgen.service` обязаны быть прописаны `Environment="HOME=/root"` и `Environment="PATH=/opt/crmtg/.venv/bin:/usr/local/bin:/usr/bin:/bin"`, чтобы воркеры всегда находили `/usr/local/bin/treg` и `/root/.treg/config.json`.
   - В `app/hygiene.py` обязаны присутствовать контрактные функции `audit_crm_hygiene`, `heal_crm_hygiene` и `audit_server_leadgen_readiness`.
2. **DB ICP Hygiene & VPS Readiness CLI Audit (`python3 scripts/proactive_audit.py --db <crm.db> --enforce-vps-readiness`)**:
   - Запускает `audit_crm_hygiene(conn)` и `audit_server_leadgen_readiness(conn, enforce_vps_checks=True)`; при `active_junk_recipient_count > 0`, `active_junk_lead_count > 0`, `contactless_lead_count > 0` или непустом `missing_components` завершается с `exit 1` и списком причин в `stderr`.
   - Режим `--heal` автоматически карантинит не-ICP получателей/лидов и очищает битые заголовки через `heal_crm_hygiene(conn)`.
3. **Live Deep Health & Deploy Rollback Gate (`python3 scripts/proactive_audit.py --health-url http://127.0.0.1:18084/api/health?deep=1`)**:
   - На живом сервере проверяет одновременно 5 инвариантов: `"db_ok": true`, `"reply_alert_guard_ok": true`, `"false_reply_alerts_24h": 0`, `"icp_hygiene_ok": true` и `"leadgen_ready": true`. Любое отклонение немедленно даёт `exit 1` с распечаткой `missing_components` и блокирует выкатку в `deploy-vps.yml`.
