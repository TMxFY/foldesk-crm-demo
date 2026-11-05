"""
Comprehensive regression suite for the 21 operational bugs fixed in feat/bug-audit.
"""
import asyncio
from fastapi.testclient import TestClient

from app.airtable_sync import map_airtable_record_to_lead_fields
from app.db import connect, init_db
from app.inbox import get_thread
from app.main import create_app
from app.telegram_gateway import TelegramGateway


class SpyGateway(TelegramGateway):
    def __init__(self):
        self.fetch_calls = []
        self.sent_calls = []
        self.proxies = {}

    def set_proxy(self, session_path: str, proxy_url: str | None) -> None:
        self.proxies[session_path] = proxy_url

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
            {
                "telegram_message_id": 101,
                "direction": "in",
                "text": "Real message from Telegram",
                "sent_at": "2026-11-05T10:00:00Z",
            }
        ]

    async def send_message(self, session_path: str, peer_id: int, text: str, reply_to=None) -> dict:
        self.sent_calls.append((session_path, peer_id, text))
        return {"telegram_message_id": 202, "sent_at": "2026-11-05T10:05:00Z"}

    async def resolve_phones(self, session_path: str, phones: list[str]) -> list[dict]:
        # Simulate only one phone existing in Telegram
        return [
            {
                "phone": "+79991112233",
                "telegram_user_id": 555666777,
                "username": "real_found_user",
                "name": "Real Found User",
            }
        ]


def test_bug_01_real_telegram_peer_id_fetches_messages(tmp_path):
    """Real Telegram user IDs (>= 1000000, e.g. 5680938518) must call gateway.fetch_messages."""
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
    assert len(gw.fetch_calls) == 1
    assert gw.fetch_calls[0][1] == 5680938518
    assert len(msgs) == 1
    assert msgs[0]["text"] == "Real message from Telegram"


def test_bug_02_old_incoming_message_does_not_falsely_stop_new_outreach_campaign(tmp_path):
    """Historical incoming messages before campaign send must not mark recipient as 'replied'."""
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
        assert rec["status"] == "in_progress"


def test_bug_03_merge_leads_preserves_airtable_fields_and_calendar_events(tmp_path):
    """merge_leads must copy V5/Airtable fields and re-link calendar_events to target lead."""
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
            "/api/calendar/events",
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

        assert merged["company"] == "Acme Corp"
        assert merged["monthly_value"] == 4500
        assert merged["airtable_record_id"] == "recABC123"

        cal_row = conn.execute("SELECT lead_id FROM calendar_events WHERE id = ?", (ev["id"],)).fetchone()
        assert cal_row["lead_id"] == target["id"]


def test_bug_04_calendar_reschedule_campaign_step_and_shift_end_at(tmp_path):
    """Rescheduling a campaign step (`camp:1`) and shifting event `end_at` must succeed."""
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
        assert resp.status_code == 200
        assert resp.json()["uid"] == "camp:1"

        ev = client.post(
            "/api/calendar/events",
            json={
                "title": "1h Sync",
                "event_type": "call",
                "start_at": "2026-11-10T14:00:00",
                "end_at": "2026-11-10T15:00:00",
            },
        ).json()
        moved = client.post(
            "/api/calendar/reschedule",
            json={"uid": f"ev:{ev['id']}", "new_start_at": "2026-11-15"},
        ).json()
        assert moved["start_at"].startswith("2026-11-15T14:00:00")
        assert moved["end_at"].startswith("2026-11-15T15:00:00")


def test_bug_05_accounts_start_and_ai_bot_config_and_open_by_lead(tmp_path):
    """Frontend payloads for `/api/accounts/start`, `/api/ai-bot/config`, and `/api/dialogs/open-by-lead` without lead_id."""
    conn = connect(tmp_path / "repro5.db")
    init_db(conn)
    gw = SpyGateway()
    app = create_app(conn=conn, gateway=gw)

    with TestClient(app) as client:
        resp = client.post(
            "/api/accounts/start",
            json={"phone": "+79995554433", "proxy": "socks5://1.2.3.4:1080", "access": "personal"},
        ).json()
        assert resp["proxy_url"] == "socks5://1.2.3.4:1080"
        assert resp["access_scope"] == "personal"

        ai_save = client.post(
            "/api/ai-bot/config",
            json={"copilot_enabled": True, "auto_qualify": False, "goal": "Book 15m call"},
        )
        assert ai_save.status_code == 200
        ai_get = client.get("/api/ai-bot/config").json()
        assert ai_get["config"]["goal"] == "Book 15m call"

        conn.execute("UPDATE accounts SET status = 'connected', health = 'active' WHERE id = ?", (resp["id"],))
        conn.commit()

        dlg_resp = client.post(
            "/api/dialogs/open-by-lead",
            json={"lead_id": None, "account_id": resp["id"], "username": "new_prospect", "name": "New Prospect"},
        )
        assert dlg_resp.status_code == 200
        assert dlg_resp.json()["username"] == "new_prospect"


def test_bug_06_tools_do_not_fabricate_fake_contacts_on_real_account(tmp_path):
    """With a real gateway and non-demo account, unfound phones must not create fake 'Алексей Соколов' leads."""
    conn = connect(tmp_path / "repro6.db")
    init_db(conn)
    gw = SpyGateway()
    app = create_app(conn=conn, gateway=gw)

    with TestClient(app) as client:
        conn.execute(
            "INSERT INTO accounts (id, phone, session_path, status, health, provider, created_at) VALUES (1, '+79991112233', 'data/sessions/1.session', 'connected', 'active', 'telegram', '2026-11-05T00:00:00Z')"
        )
        conn.commit()

        res = client.post(
            "/api/tools/phone-converter",
            json={
                "phones_text": "+79991112233\n+79990009999",
                "account_id": 1,
                "add_to_leads": True,
            },
        ).json()
        assert res["found_count"] == 1
        assert res["added_to_leads"] == 1
        leads = client.get("/api/leads").json()
        assert len(leads) == 1
        assert leads[0]["username"] == "real_found_user"


def test_bug_07_airtable_list_fields_and_mcp_ev_uid_and_property_thousands(tmp_path):
    """Airtable list fields, MCP `ev:1` UID, and `$1,000` amount parsing must work cleanly."""
    mapped = map_airtable_record_to_lead_fields(
        {
            "id": "recList1",
            "fields": {
                "Lead Name": ["Founder Name"],
                "Company": ["Acme", "Labs"],
                "Niche": ["AI", "UGC"],
                "Status": ["In conversation"],
                "Value": "TBD",
                "Monthly Value": "$3,200",
            },
        }
    )
    assert mapped["name"] == "Founder Name"
    assert mapped["company"] == "Acme, Labs"
    assert mapped["status"] == "talking"
    assert mapped["monthly_value"] == 3200

    conn = connect(tmp_path / "repro7.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        lead = client.post("/api/leads", json={"name": "Prop Lead"}).json()
        pdef = client.post("/api/property-defs", json={"name": "Deal Size", "type": "amount", "object_type": "lead"}).json()
        pval = client.put(f"/api/leads/{lead['id']}/properties/{pdef['id']}", json={"value": "$1,000"}).json()
        assert pval["value_int"] == 100000

        ev = client.post(
            "/api/calendar/events",
            json={"title": "MCP Call", "event_type": "call", "start_at": "2026-11-12T16:00:00", "lead_id": lead["id"]},
        ).json()
        mcp_resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "schedule_calendar_event",
                    "arguments": {"action": "complete", "uid": f"ev:{ev['id']}"},
                },
            },
        ).json()
        assert mcp_resp["result"]["isError"] is False
        assert mcp_resp["result"]["structuredContent"]["event"]["status"] == "completed"
