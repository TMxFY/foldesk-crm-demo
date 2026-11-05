"""
Deterministic reproduction suite (Phase 1 feedback loop for /diagnosing-bugs)
proving the operational bugs found during the CRM audit.
"""
import asyncio
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.inbox import get_thread
from app.main import create_app
from app.telegram_gateway import TelegramGateway


class SpyGateway(TelegramGateway):
    def __init__(self):
        self.fetch_calls = []
        self.sent_calls = []

    async def request_code(self, phone: str, session_path: str) -> str:
        return "hash123"

    async def confirm_code(self, phone: str, code: str, phone_code_hash: str, session_path: str) -> dict:
        return {"status": "connected", "telegram_user_id": 5680938518, "telegram_username": "real_op", "telegram_name": "Real Op"}

    async def confirm_password(self, password: str, session_path: str) -> dict:
        return {"status": "connected"}

    async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
        return []

    async def fetch_messages(self, session_path: str, peer_id: int, limit: int = 50) -> list:
        self.fetch_calls.append((session_path, peer_id, limit))
        return [
            {"id": 101, "direction": "in", "text": "Real message from Telegram", "sent_at": "2026-11-05T10:00:00Z"}
        ]

    async def send_message(self, session_path: str, peer_id: int, text: str, reply_to=None) -> dict:
        self.sent_calls.append((session_path, peer_id, text))
        return {"telegram_message_id": 202, "sent_at": "2026-11-05T10:05:00Z"}


def test_bug_real_telegram_peer_id_blocks_fetch_messages(tmp_path):
    """
    BUG-01: In app/inbox.py:1208, `if peer_id >= 900000: return list_messages(...)`
    blocks `gateway.fetch_messages` for ALL real Telegram users (whose IDs are 7-10 digits, e.g. 5680938518).
    """
    conn = connect(tmp_path / "repro1.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO accounts (id, phone, session_path, status, health, provider, created_at) VALUES (1, '+79991112233', 'data/sessions/1.session', 'connected', 'active', 'telegram', '2026-11-05T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO dialogs (id, account_id, peer_id, username, title, created_at, updated_at) VALUES (1, 1, 5680938518, 'client_real', 'Real Client', '2026-11-05T10:00:00Z', '2026-11-05T10:00:00Z')"
    )
    conn.commit()

    gw = SpyGateway()
    msgs = asyncio.run(get_thread(conn, 1, gw))
    assert len(gw.fetch_calls) == 1, f"Expected gateway.fetch_messages to be called for real peer_id=5680938518, got {len(gw.fetch_calls)} calls"
    assert len(msgs) == 1


def test_bug_old_incoming_message_falsely_stops_new_outreach_campaign(tmp_path):
    """
    BUG-02: In app/outreach.py:398-408 & app/inbox.py:1128, any historical incoming message
    in `messages` causes `_dialog_has_client_reply` / `_cache_messages` to immediately mark
    a newly started campaign recipient as 'replied' right after Step 1 is sent.
    """
    conn = connect(tmp_path / "repro2.db")
    init_db(conn)
    gw = SpyGateway()
    app = create_app(conn=conn, gateway=gw)

    with TestClient(app) as client:
        conn.execute(
            "INSERT INTO accounts (id, phone, session_path, status, health, daily_limit, provider, created_at) VALUES (1, '+79991112233', 'data/sessions/1.session', 'connected', 'active', 20, 'telegram', '2026-11-05T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO leads (id, account_id, telegram_user_id, username, name, status, created_at, updated_at) VALUES (1, 1, 123456, 'old_contact', 'Old Contact', 'new', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO dialogs (id, account_id, peer_id, lead_id, username, title, last_from_peer, created_at, updated_at) VALUES (1, 1, 123456, 1, 'old_contact', 'Old Contact', 0, '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at) VALUES (1, 10, 'in', 'Old hello from Jan', '2026-01-01T10:00:00Z', '2026-01-01T10:00:00Z', '2026-01-01T10:00:00Z')"
        )
        conn.execute(
            "INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at) VALUES (1, 11, 'out', 'Our reply in Jan', '2026-01-02T10:00:00Z', '2026-01-02T10:00:00Z', '2026-01-02T10:00:00Z')"
        )
        conn.commit()

        camp = client.post(
            "/api/campaigns",
            json={
                "name": "November Promo",
                "account_id": 1,
                "steps": [
                    {"step_number": 1, "delay_hours": 0, "template_text": "Step 1 hello"},
                    {"step_number": 2, "delay_hours": 24, "template_text": "Step 2 followup"},
                ],
            },
        ).json()
        cid = camp["id"]
        client.post(f"/api/campaigns/{cid}/csv", json={"csv_text": "username,name\nold_contact,Old Contact"})
        res = client.post(f"/api/campaigns/{cid}/run", json={"max_send": 5, "respect_delays": False, "force": True}).json()
        rec = res["campaign"]["recipients"][0]
        assert rec["status"] == "in_progress", (
            f"Recipient should remain 'in_progress' waiting for Step 2, but became '{rec['status']}' due to old message from January!"
        )


def test_bug_merge_leads_drops_airtable_fields_and_calendar_events(tmp_path):
    """
    BUG-03: In app/leads.py:1283-1437, `merge_leads` drops `company`, `niche`, `offer_type`,
    `monthly_value`, `airtable_record_id` from source lead and unlinks `calendar_events`.
    """
    conn = connect(tmp_path / "repro3.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        target = client.post("/api/leads", json={"name": "Ivan", "username": "ivan_tg"}).json()
        source = client.post(
            "/api/leads",
            json={
                "name": "Ivan",
                "company": "Acme Corp",
                "niche": "SaaS",
                "monthly_value": 4500,
                "airtable_record_id": "recABC123",
            },
        ).json()
        ev = client.post(
            "/api/calendar",
            json={
                "title": "Demo Call with Ivan",
                "event_type": "call",
                "start_at": "2026-11-10T15:00:00",
                "lead_id": source["id"],
            },
        ).json()

        merged = client.post(
            "/api/leads/merge",
            json={"source_lead_id": source["id"], "target_lead_id": target["id"]},
        ).json()

        assert merged["company"] == "Acme Corp", f"Lost company on merge: {merged.get('company')}"
        assert merged["monthly_value"] == 4500, f"Lost monthly_value on merge: {merged.get('monthly_value')}"
        assert merged["airtable_record_id"] == "recABC123", f"Lost airtable_record_id on merge: {merged.get('airtable_record_id')}"

        cal_row = conn.execute("SELECT lead_id FROM calendar_events WHERE id = ?", (ev["id"],)).fetchone()
        assert cal_row["lead_id"] == target["id"], f"Calendar event lost lead_id after merge: {cal_row['lead_id']}"


def test_bug_calendar_reschedule_campaign_step_fails_400(tmp_path):
    """
    BUG-04: Drag-and-drop of a campaign step chip (`uid='camp:1'`) in Calendar calls
    `POST /api/calendar/reschedule` which raises ValueError('Неподдерживаемый тип элемента календаря: campaign_step').
    """
    conn = connect(tmp_path / "repro4.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        camp = client.post(
            "/api/campaigns",
            json={
                "name": "Test",
                "steps": [
                    {"step_number": 1, "delay_hours": 0, "template_text": "Hi"},
                    {"step_number": 2, "delay_hours": 24, "template_text": "Followup"},
                ],
            },
        ).json()
        cid = camp["id"]
        conn.execute(
            "INSERT INTO campaign_recipients (id, campaign_id, username, name, status, current_step, last_sent_at, created_at, updated_at) VALUES (1, ?, 'user1', 'User 1', 'in_progress', 1, '2026-11-05T10:00:00Z', '2026-11-05T10:00:00Z', '2026-11-05T10:00:00Z')",
            (cid,),
        )
        conn.commit()

        resp = client.post("/api/calendar/reschedule", json={"uid": "camp:1", "new_start_at": "2026-11-12T14:00:00"})
        assert resp.status_code == 200, f"Expected 200 when rescheduling campaign step, got {resp.status_code}: {resp.text}"


def test_bug_accounts_start_ignores_frontend_proxy_and_access_fields(tmp_path):
    """
    BUG-05: Frontend (`app.js:4220-4228`) sends `{phone, proxy, access}` to `POST /api/accounts/start`,
    but `StartIn` (`main.py:106-112`) expects `{phone, proxy_url, access_scope}`, silently dropping
    both proxy and access scope!
    """
    conn = connect(tmp_path / "repro5.db")
    init_db(conn)
    gw = SpyGateway()
    app = create_app(conn=conn, gateway=gw)

    with TestClient(app) as client:
        resp = client.post(
            "/api/accounts/start",
            json={"phone": "+79995554433", "proxy": "socks5://1.2.3.4:1080", "access": "personal"},
        ).json()
        assert resp["proxy_url"] == "socks5://1.2.3.4:1080", f"Expected proxy_url to be saved from frontend payload, got {resp['proxy_url']}"
        assert resp["access_scope"] == "personal", f"Expected access_scope='personal', got {resp['access_scope']}"
