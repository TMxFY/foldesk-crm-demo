import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.db import connect, init_db, utc_now
from app.mcp_server import (
    TOOLS_SCHEMA,
    call_mcp_tool_handler,
    handle_jsonrpc_message,
    register_mcp_routes,
)


@pytest.fixture()
def db_conn(tmp_path):
    conn = connect(tmp_path / "test_mcp.db")
    init_db(conn)
    return conn


@pytest.fixture()
def mcp_client(db_conn):
    app = FastAPI()
    app.state.conn = db_conn
    register_mcp_routes(app)
    return TestClient(app)


def test_mcp_initialize_and_tools_list_no_message_sending(db_conn):
    init_resp = handle_jsonrpc_message(
        db_conn,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        },
    )
    assert init_resp["id"] == 1
    assert init_resp["result"]["serverInfo"]["name"] == "foldesk-crm-mcp"

    # Notification returns None
    notif_resp = handle_jsonrpc_message(
        db_conn,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert notif_resp is None

    list_resp = handle_jsonrpc_message(
        db_conn,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    tools = list_resp["result"]["tools"]
    names = {t["name"] for t in tools}

    expected = {
        "get_analytics_summary",
        "get_pipeline_stages",
        "search_leads",
        "get_lead_full_card",
        "create_or_update_lead",
        "add_lead_note",
        "manage_lead_tasks",
        "manage_reminders",
        "get_calendar_schedule",
        "schedule_calendar_event",
        "list_inbox_dialogs",
        "get_focus_today",
        "sync_airtable",
    }
    assert expected.issubset(names)

    # Строго без тулов отправки сообщений в мессенджеры
    forbidden = {"send_message", "send_reply", "run_campaign", "send_telegram", "send_whatsapp"}
    assert names.isdisjoint(forbidden)
    for t in TOOLS_SCHEMA:
        assert "send_" not in t["name"]


def test_mcp_all_tools_lifecycle(db_conn):
    def call_tool(name: str, arguments: dict | None = None) -> dict:
        resp = handle_jsonrpc_message(
            db_conn,
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
        )
        assert "result" in resp, f"Unexpected error for {name}: {resp}"
        res = resp["result"]
        assert res["isError"] is False, f"Tool {name} failed: {res['content'][0]['text']}"
        parsed_text = json.loads(res["content"][0]["text"])
        assert parsed_text == res["structuredContent"]
        return res["structuredContent"]

    # 1. get_pipeline_stages (с 7 стадиями Airtable)
    stages_res = call_tool("get_pipeline_stages", {"ensure_airtable_stages": True})
    stage_keys = [s["key"] for s in stages_res["stages"]]
    for k in ("new", "talking", "call_scheduled", "waiting", "on_hold", "won", "lost"):
        assert k in stage_keys

    # 2. create_or_update_lead со всеми 7 стадиями Airtable и полями company/niche/monthly_value
    created = call_tool(
        "create_or_update_lead",
        {
            "name": "Alex Founder",
            "username": "@alex_founder",
            "phone": "+15550109988",
            "status": "New",
            "amount": 3500,
            "monthly_value": 1500,
            "currency": "USD",
            "company": "Acme AI",
            "niche": "B2B SaaS",
            "offer_type": ["UGC", "Paid Ads"],
            "offer_details": "10 креативов в месяц",
            "next_step": "Провести демо",
            "next_follow_up": "2026-11-10",
            "lead_source": "Telegram Outreach",
        },
    )
    assert created["action"] == "created"
    lead_id = created["lead"]["id"]
    assert created["lead"]["company"] == "Acme AI"
    assert created["lead"]["niche"] == "B2B SaaS"
    assert created["lead"]["monthly_value"] == 1500
    assert created["lead"]["offer_type"] == ["UGC", "Paid Ads"]

    airtable_stages_map = [
        ("New", "new"),
        ("In conversation", "talking"),
        ("Call scheduled", "call_scheduled"),
        ("Offer sent", "waiting"),
        ("On hold", "on_hold"),
        ("Signed", "won"),
        ("Cancelled", "lost"),
    ]
    for airtable_label, expected_key in airtable_stages_map:
        upd = call_tool(
            "create_or_update_lead",
            {"lead_id": lead_id, "status": airtable_label},
        )
        assert upd["action"] == "updated"
        assert upd["lead"]["status"] == expected_key
        assert upd["lead"]["airtable_status"] == airtable_label

    # Вернём в In conversation
    call_tool("create_or_update_lead", {"lead_id": lead_id, "status": "In conversation"})

    # 3. search_leads с фильтрацией по company, niche, min_amount
    search_res = call_tool(
        "search_leads",
        {"company": "Acme", "niche": "SaaS", "min_amount": 3000, "limit": 10},
    )
    assert search_res["count"] == 1
    assert search_res["leads"][0]["id"] == lead_id

    # 4. add_lead_note
    note_res = call_tool(
        "add_lead_note",
        {"lead_id": lead_id, "body": "Клиент заинтересован в пакетном UGC"},
    )
    assert note_res["ok"] is True
    assert "пакетном UGC" in note_res["note"]["body"]

    # 5. manage_lead_tasks (create, list, complete, reopen)
    t_created = call_tool(
        "manage_lead_tasks",
        {"action": "create", "lead_id": lead_id, "title": "Подготовить КП"},
    )
    task_id = t_created["task"]["id"]
    assert t_created["task"]["done_at"] is None

    t_done = call_tool(
        "manage_lead_tasks",
        {"action": "complete", "task_id": task_id},
    )
    assert t_done["task"]["done_at"] is not None

    t_reopen = call_tool(
        "manage_lead_tasks",
        {"action": "reopen", "task_id": task_id},
    )
    assert t_reopen["task"]["done_at"] is None

    t_list = call_tool("manage_lead_tasks", {"action": "list", "lead_id": lead_id})
    assert len(t_list["tasks"]) == 1

    # 6. manage_reminders (create, list, postpone, complete)
    r_created = call_tool(
        "manage_reminders",
        {
            "action": "create",
            "lead_id": lead_id,
            "due_at": "2026-11-06T12:00",
            "text": "Написать по КП",
        },
    )
    rem_id = r_created["reminder"]["id"]

    r_postponed = call_tool(
        "manage_reminders",
        {
            "action": "postpone",
            "reminder_id": rem_id,
            "due_at": "2026-11-07T15:30",
        },
    )
    assert "2026-11-07T15:30" in r_postponed["reminder"]["due_at"]

    r_list = call_tool("manage_reminders", {"action": "list", "scope": "open", "lead_id": lead_id})
    assert r_list["count"] == 1

    # 7. schedule_calendar_event & get_calendar_schedule
    cal_created = call_tool(
        "schedule_calendar_event",
        {
            "action": "create",
            "lead_id": lead_id,
            "title": "Демо-созвон с Acme AI",
            "start_at": "2026-11-08T14:00:00",
            "end_at": "2026-11-08T14:45:00",
            "event_type": "call",
            "meet_link": "https://meet.google.com/abc-defg-hij",
            "notes": "Обсудить 10 креативов",
        },
    )
    event_id = cal_created["event"]["id"]

    cal_rescheduled = call_tool(
        "schedule_calendar_event",
        {
            "action": "reschedule",
            "event_id": event_id,
            "start_at": "2026-11-08T16:00:00",
        },
    )
    assert "2026-11-08T16:00:00" in cal_rescheduled["event"]["start_at"]

    sched = call_tool(
        "get_calendar_schedule",
        {"start_date": "2026-11-01", "end_date": "2026-11-30", "lead_id": lead_id},
    )
    assert sched["count"] >= 1

    # 8. Создадим тестовый аккаунт и диалог с сообщением для проверки list_inbox_dialogs и get_lead_full_card
    now = utc_now()
    cur_acc = db_conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at, provider)
        VALUES ('+15550001111', 'data/sessions/demo.session', 'connected', ?, 'telegram')
        """,
        (now,),
    )
    acc_id = int(cur_acc.lastrowid)
    cur_dlg = db_conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 500100, 'alex_founder', 'Alex Founder', 'telegram', 0, 1, ?, 'Привет! Жду оффер', 1, 0, ?, ?, ?)
        """,
        (acc_id, now, lead_id, now, now),
    )
    dlg_id = int(cur_dlg.lastrowid)
    db_conn.execute(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, 1, 'in', 'Привет! Жду оффер', ?, ?, ?)
        """,
        (dlg_id, now, now, now),
    )
    db_conn.commit()

    # 9. get_lead_full_card по username (без lead_id)
    full_card = call_tool("get_lead_full_card", {"username": "alex_founder"})
    assert full_card["id"] == lead_id
    assert len(full_card["messages"]) == 1
    assert full_card["messages"][0]["text"] == "Привет! Жду оффер"
    assert len(full_card["reminders"]) == 1
    assert len(full_card["calendar_events"]) == 1

    # 10. list_inbox_dialogs (список и конкретный диалог)
    inbox_res = call_tool("list_inbox_dialogs", {"chip": "waiting"})
    assert inbox_res["count"] == 1
    assert inbox_res["counts"]["waiting"] == 1

    dlg_detail = call_tool("list_inbox_dialogs", {"dialog_id": dlg_id})
    assert dlg_detail["messages_count"] == 1

    # 11. get_analytics_summary
    analytics_res = call_tool("get_analytics_summary", {"period": "30d"})
    assert "funnel" in analytics_res
    assert analytics_res["funnel"]["total_leads"] >= 1

    # 12. get_focus_today и sync_airtable
    focus_res = call_tool("get_focus_today", {})
    assert "waiting_reply" in focus_res
    assert focus_res["counts"]["waiting_reply"] >= 1

    sync_res = call_tool("sync_airtable", {"direction": "status"})
    assert isinstance(sync_res, dict)


def test_mcp_http_auth_and_info_endpoint(mcp_client, monkeypatch):
    # Без MCP_API_KEY пускает локальные запросы
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    info_resp = mcp_client.get("/api/mcp/info")
    assert info_resp.status_code == 200
    info_data = info_resp.json()
    assert info_data["status"] == "ready"
    assert "foldesk-crm" in info_data["configs"]["stdio"]["mcpServers"]
    assert "foldesk-crm-remote" in info_data["configs"]["http"]["mcpServers"]

    # С установленным MCP_API_KEY требует заголовок или api_key
    monkeypatch.setenv("MCP_API_KEY", "secret-mcp-token-123")

    unauth = mcp_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert unauth.status_code == 401
    assert "MCP_API_KEY" in unauth.json()["detail"]

    # Авторизация через Authorization: Bearer
    auth_bearer = mcp_client.post(
        "/mcp",
        headers={"Authorization": "Bearer secret-mcp-token-123"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert auth_bearer.status_code == 200
    assert auth_bearer.json()["result"] == {}

    # Авторизация через X-MCP-Key
    auth_header = mcp_client.post(
        "/mcp",
        headers={"X-MCP-Key": "secret-mcp-token-123"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert auth_header.status_code == 200
    assert len(auth_header.json()["result"]["tools"]) >= 12
