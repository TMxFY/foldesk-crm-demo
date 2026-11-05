from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import socket
import subprocess

from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.events import EVENT_TYPES, get_today_focus, list_lead_events, record_lead_event
from app.inbox import apply_incoming
from app.main import create_app

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _make_client(tmp_path):
    conn = connect(tmp_path / "crm_block6.db")
    init_db(conn)
    app = create_app(conn=conn)
    return TestClient(app), conn


def test_lead_events_all_lifecycle_types_and_card_payload(tmp_path):
    client, conn = _make_client(tmp_path)

    # 1. Create a connected demo account for outreach and inbox
    now_dt = datetime.now(timezone.utc)
    now_iso = _iso(now_dt)
    today_str = now_iso[:10]
    acc_cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at,
            health, updated_at, daily_limit, sent_today, sent_date
        ) VALUES ('+79990001122', 'data/sessions/demo_b6.session', 'connected', 710001, 'b6_sales', 'Sales B6', ?, 'active', ?, 20, 0, ?)
        """,
        (now_iso, now_iso, today_str),
    )
    account_id = int(acc_cur.lastrowid)
    conn.commit()

    # 2. Create lead -> records 'created'
    r_create = client.post(
        "/api/leads",
        json={
            "name": "Иван Таймлайнов",
            "username": "@ivan_timeline",
            "phone": "+79991234567",
            "status": "new",
        },
    )
    assert r_create.status_code == 200
    lead = r_create.json()
    lead_id = lead["id"]
    assert any(ev["event_type"] == "created" for ev in lead.get("events", []))

    # 3. Change status -> records 'status_changed'
    r_status = client.patch(f"/api/leads/{lead_id}", json={"status": "talking"})
    assert r_status.status_code == 200

    # 4. Change amount via property and via lead patch -> records 'amount_changed'
    r_prop_def = client.post(
        "/api/property-defs",
        json={"name": "Бюджет контракта", "type": "amount", "object_type": "lead"},
    )
    assert r_prop_def.status_code == 200
    prop_id = r_prop_def.json()["id"]

    r_prop_val = client.put(
        f"/api/leads/{lead_id}/properties/{prop_id}",
        json={"value": "3500"},
    )
    assert r_prop_val.status_code == 200

    # 5. Add note -> records 'note_added'
    r_note = client.post(
        f"/api/leads/{lead_id}/notes",
        json={"body": "Клиент запросил расчёт на 10 аккаунтов"},
    )
    assert r_note.status_code == 200

    # 6. Set reminder -> records 'reminder_set'
    due_iso = _iso(now_dt + timedelta(hours=2))
    r_rem = client.post(
        "/api/reminders",
        json={
            "lead_id": lead_id,
            "due_at": due_iso,
            "text": "Позвонить Ивану по КП",
        },
    )
    assert r_rem.status_code == 200

    # 7. Outreach sent -> records 'outreach_sent'
    r_camp = client.post(
        "/api/campaigns",
        json={
            "name": "Тестовая кампания B6",
            "account_id": account_id,
            "account_ids": [account_id],
            "daily_limit": 20,
            "steps": [{"step_order": 1, "delay_hours": 0, "template_text": "Привет, {name}!"}],
        },
    )
    assert r_camp.status_code == 200
    camp_id = r_camp.json()["id"]

    r_enroll = client.post(
        f"/api/campaigns/{camp_id}/from-leads",
        json={"lead_ids": [lead_id]},
    )
    assert r_enroll.status_code == 200

    r_run = client.post(f"/api/campaigns/{camp_id}/run", json={"max_send": 5})
    assert r_run.status_code == 200
    assert r_run.json()["sent"] == 1

    # 8. Incoming reply -> records 'reply_received'
    apply_incoming(
        conn,
        account_id,
        {
            "telegram_user_id": 900000 + lead_id,
            "username": "ivan_timeline",
            "name": "Иван Таймлайнов",
            "text": "Привет! Давайте созвонимся сегодня",
            "telegram_message_id": 501,
            "direction": "in",
            "peer_type": "user",
        },
    )

    # 9. Merge duplicate lead -> records 'merged' and transfers source lead events
    r_dup = client.post(
        "/api/leads",
        json={
            "name": "Иван Дубль",
            "username": "@ivan_dup_handle",
            "phone": "+79997654321",
            "status": "new",
        },
    )
    assert r_dup.status_code == 200
    dup_id = r_dup.json()["id"]
    client.post(f"/api/leads/{dup_id}/notes", json={"body": "Заметка из дубля"})

    r_merge = client.post(
        "/api/leads/merge",
        json={"source_lead_id": dup_id, "target_lead_id": lead_id},
    )
    assert r_merge.status_code == 200

    # 10. Verify GET /api/leads/{id} and GET /api/leads/{id}/card return all 8 event types
    r_get = client.get(f"/api/leads/{lead_id}")
    assert r_get.status_code == 200
    events_from_lead = r_get.json()["events"]

    r_card = client.get(f"/api/leads/{lead_id}/card")
    assert r_card.status_code == 200
    card = r_card.json()
    events_from_card = card["events"]

    assert len(events_from_lead) == len(events_from_card)
    seen_types = {ev["event_type"] for ev in events_from_card}
    for expected_type in EVENT_TYPES:
        assert expected_type in seen_types, f"Missing event_type {expected_type} in {seen_types}"

    # Verify chronological order (created_at ascending, id ascending)
    ids = [ev["id"] for ev in events_from_card]
    assert ids == sorted(ids)


def test_focus_today_endpoint_filters_and_edge_cases(tmp_path):
    client, conn = _make_client(tmp_path)

    # Empty state check
    r_empty = client.get("/api/focus/today")
    assert r_empty.status_code == 200
    empty_data = r_empty.json()
    assert empty_data["waiting_reply"] == []
    assert empty_data["due_reminders"] == []
    assert empty_data["stale_leads"] == []
    assert empty_data["counts"]["total_focus"] == 0

    now_dt = datetime(2026, 11, 4, 14, 0, 0, tzinfo=timezone.utc)
    now_iso = _iso(now_dt)

    # Create account
    acc_cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at,
            health, updated_at, daily_limit, sent_today, sent_date
        ) VALUES ('+79990009988', 'data/sessions/demo_focus.session', 'connected', 720001, 'focus_acc', 'Focus Sales', ?, 'active', ?, 20, 0, ?)
        """,
        (now_iso, now_iso, now_iso[:10]),
    )
    acc_id = int(acc_cur.lastrowid)

    # Lead 1: Stale talking lead (updated 5 days ago) + waiting reply dialog + overdue reminder
    stale_iso = _iso(now_dt - timedelta(days=5))
    l1_cur = conn.execute(
        """
        INSERT INTO leads (
            account_id, telegram_user_id, username, name, phone,
            status, note, next_step, created_at, updated_at, amount, currency
        ) VALUES (?, 810001, 'stale_client', 'Клиент Без Движения', '+79991110001', 'talking', 'Ждём обратную связь', 'Дожать по пилоту', ?, ?, 2500, 'USD')
        """,
        (acc_id, stale_iso, stale_iso),
    )
    lead_stale_id = int(l1_cur.lastrowid)

    # Lead 2: Fresh waiting lead (updated 1 day ago) -> should NOT be in stale_leads
    fresh_iso = _iso(now_dt - timedelta(days=1))
    l2_cur = conn.execute(
        """
        INSERT INTO leads (
            account_id, telegram_user_id, username, name, phone,
            status, note, next_step, created_at, updated_at, amount, currency
        ) VALUES (?, 810002, 'fresh_client', 'Свежий Клиент', '+79991110002', 'waiting', 'Недавно общались', 'Ждём вторника', ?, ?, 1500, 'USD')
        """,
        (acc_id, fresh_iso, fresh_iso),
    )
    lead_fresh_id = int(l2_cur.lastrowid)

    # Lead 3: Won lead updated 10 days ago -> should NOT be in stale_leads
    old_won_iso = _iso(now_dt - timedelta(days=10))
    conn.execute(
        """
        INSERT INTO leads (
            account_id, telegram_user_id, username, name, phone,
            status, created_at, updated_at, amount, currency
        ) VALUES (?, 810003, 'won_client', 'Закрытый Клиент', '+79991110003', 'won', ?, ?, 5000, 'USD')
        """,
        (acc_id, old_won_iso, old_won_iso),
    )

    # Dialog 1: last_from_peer = 1 -> MUST be in waiting_reply
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 810001, 'stale_client', 'Клиент Без Движения', 'telegram', 0, 2, ?, 'Когда созвонимся?', 1, 0, ?, ?, ?)
        """,
        (acc_id, now_iso, lead_stale_id, now_iso, now_iso),
    )

    # Dialog 2: last_from_peer = 0 -> must NOT be in waiting_reply
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 810002, 'fresh_client', 'Свежий Клиент', 'telegram', 0, 0, ?, 'Отправил презентацию', 0, 0, ?, ?, ?)
        """,
        (acc_id, now_iso, lead_fresh_id, now_iso, now_iso),
    )

    # Reminders including non-UTC timezone offset near midnight (+03:00):
    # - Overdue open reminder (due 3 hours ago) -> included
    # - Today open reminder (due in 2 hours) -> included
    # - Timezone boundary reminder: '2026-11-05T01:30:00+03:00' is '2026-11-04T22:30:00Z' -> included on 2026-11-04 UTC!
    # - Timezone boundary next day: '2026-11-05T04:30:00+03:00' is '2026-11-05T01:30:00Z' -> excluded on 2026-11-04 UTC!
    # - Future open reminder (due in 3 days) -> excluded
    # - Completed reminder (due 1 hour ago, done_at set) -> excluded
    conn.execute(
        """
        INSERT INTO reminders (lead_id, due_at, text, done_at, created_at)
        VALUES
            (?, ?, 'Просроченный фоллоу-ап', NULL, ?),
            (?, ?, 'Сегодняшний звонок', NULL, ?),
            (?, '2026-11-05T01:30:00+03:00', 'Звонок в 01:30 МСК (22:30 UTC)', NULL, ?),
            (?, '2026-11-05T04:30:00+03:00', 'Утренний звонок завтра МСК', NULL, ?),
            (?, ?, 'Напоминание через 3 дня', NULL, ?),
            (?, ?, 'Уже выполненное напоминание', ?, ?)
        """,
        (
            lead_stale_id,
            _iso(now_dt - timedelta(hours=3)),
            now_iso,
            lead_fresh_id,
            _iso(now_dt + timedelta(hours=2)),
            now_iso,
            lead_fresh_id,
            now_iso,
            lead_fresh_id,
            now_iso,
            lead_fresh_id,
            _iso(now_dt + timedelta(days=3)),
            now_iso,
            lead_stale_id,
            _iso(now_dt - timedelta(hours=1)),
            now_iso,
            now_iso,
        ),
    )
    conn.commit()

    resp = client.get("/api/focus/today", params={"now": now_iso, "stale_days": 3})
    assert resp.status_code == 200
    focus = resp.json()

    # 1. Check waiting_reply
    assert len(focus["waiting_reply"]) == 1
    assert focus["waiting_reply"][0]["username"] == "stale_client"
    assert focus["waiting_reply"][0]["last_from_peer"] == 1

    # 2. Check due_reminders
    assert len(focus["due_reminders"]) == 3
    texts = [r["text"] for r in focus["due_reminders"]]
    assert "Просроченный фоллоу-ап" in texts
    assert "Сегодняшний звонок" in texts
    assert "Звонок в 01:30 МСК (22:30 UTC)" in texts
    assert "Утренний звонок завтра МСК" not in texts
    assert "Напоминание через 3 дня" not in texts
    assert "Уже выполненное напоминание" not in texts
    overdue_flags = {r["text"]: r["overdue"] for r in focus["due_reminders"]}
    assert overdue_flags["Просроченный фоллоу-ап"] is True
    assert overdue_flags["Сегодняшний звонок"] is False
    assert overdue_flags["Звонок в 01:30 МСК (22:30 UTC)"] is False

    # 3. Check stale_leads
    assert len(focus["stale_leads"]) == 1
    stale = focus["stale_leads"][0]
    assert stale["id"] == lead_stale_id
    assert stale["username"] == "stale_client"
    assert stale["days_stale"] >= 5
    assert stale["amount"] == 2500

    # 4. Large event history truncation returns the MOST RECENT events in chronological order
    for idx in range(120):
        record_lead_event(
            conn,
            lead_stale_id,
            "note_added",
            f"Событие #{idx + 1}",
            {"seq": idx + 1},
            created_at=_iso(now_dt + timedelta(seconds=idx)),
        )
    conn.commit()
    r_events_lim = client.get(f"/api/leads/{lead_stale_id}/events", params={"limit": 10})
    assert r_events_lim.status_code == 200
    lim_events = r_events_lim.json()
    assert len(lim_events) == 10
    titles = [ev["title"] for ev in lim_events]
    assert titles[0] == "Событие #111"
    assert titles[-1] == "Событие #120"


def test_demo_seed_focus_and_playwright_e2e(tmp_path):
    client, _conn = _make_client(tmp_path)

    r_seed = client.post("/api/demo/seed")
    assert r_seed.status_code == 200

    r_focus = client.get("/api/focus/today")
    assert r_focus.status_code == 200
    focus = r_focus.json()
    assert len(focus["waiting_reply"]) >= 3
    assert len(focus["due_reminders"]) >= 2
    assert len(focus["stale_leads"]) >= 2

    r_leads = client.get("/api/leads")
    assert r_leads.status_code == 200
    leads = r_leads.json()
    assert len(leads) >= 8

    card_resp = client.get(f"/api/leads/{leads[0]['id']}/card")
    assert card_resp.status_code == 200
    card = card_resp.json()
    assert isinstance(card.get("events"), list)
    assert len(card["events"]) >= 2

    html_resp = client.get("/")
    assert html_resp.status_code == 200
    html = html_resp.text
    assert 'id="focus-today-widget"' in html
    assert "Мой фокус на сегодня" in html
    assert 'id="focus-waiting-reply-list"' in html
    assert 'id="focus-due-reminders-list"' in html
    assert 'id="focus-stale-leads-list"' in html

    env = dict(os.environ)
    env["E2E_PORT"] = str(_free_port())
    proc = subprocess.run(
        ["npx", "playwright", "test", "f27-v4-block6-timeline-focus.spec.ts"],
        cwd=str(ROOT / "e2e"),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, f"Playwright failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
