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
