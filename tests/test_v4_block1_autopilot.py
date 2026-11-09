import asyncio
from datetime import datetime, timezone
import json
import random

from fastapi.testclient import TestClient

from app.db import connect, init_db, utc_now
from app.inbox import apply_incoming, remember_dialogs, sync_inbox
from app.main import create_app
from app.outreach import (
    evaluate_schedule_window,
    expand_spintax,
    is_within_outreach_schedule,
    render_template,
    run_autopilot_tick,
    set_autopilot_enabled,
    update_outreach_schedule,
)
from app.telegram_gateway import FakeGateway


def _seed_connected_account(conn, phone="+79990001122", daily_limit=10):
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at,
            health, updated_at, daily_limit, sent_today, sent_date
        ) VALUES (?, 'data/sessions/demo_test.session', 'connected', 710001, 'sales_acc', 'Сейлз Аккаунт', ?, 'active', ?, ?, 0, NULL)
        """,
        (phone, now, now, daily_limit),
    )
    conn.commit()
    return cur.lastrowid


def test_spintax_flat_nested_and_with_variables():
    rng = random.Random(42)
    variants = {
        expand_spintax("{Привет|Добрый день|Здравствуйте}, коллега!", rng=random.Random(seed))
        for seed in range(30)
    }
    assert variants == {
        "Привет, коллега!",
        "Добрый день, коллега!",
        "Здравствуйте, коллега!",
    }

    nested_variants = {
        expand_spintax("{Привет|{Добрый {день|вечер}|Здравствуйте}}!", rng=random.Random(seed))
        for seed in range(50)
    }
    assert nested_variants.issubset(
        {"Привет!", "Добрый день!", "Добрый вечер!", "Здравствуйте!"}
    )
    assert len(nested_variants) >= 3

    recipient = {
        "name": "Алексей Смирнов",
        "company": "DataFlow",
        "username": "alex_df",
        "hook": "видел ваш релиз v2",
        "custom_vars_json": json.dumps({"city": "Москва", "role": "CTO"}, ensure_ascii=False),
    }
    template = "{Привет|Здравствуйте}, {name}! Я по поводу {company} ({city}, {role}) — {hook|{отличный продукт|классный запуск}}."
    rendered = render_template(template, recipient, rng=rng)
    assert "Алексей Смирнов" in rendered
    assert "DataFlow (Москва, CTO)" in rendered
    assert "{" not in rendered and "}" not in rendered
    assert rendered.startswith(("Привет, Алексей Смирнов!", "Здравствуйте, Алексей Смирнов!"))


def test_outreach_schedule_window_timezones_and_weekdays(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)

    schedule = update_outreach_schedule(
        conn,
        timezone_name="Europe/Moscow",
        days={
            "mon": {"enabled": True, "start": "10:00", "end": "19:00"},
            "tue": {"enabled": True, "start": "10:00", "end": "19:00"},
            "wed": {"enabled": False, "start": "10:00", "end": "19:00"},
            "sat": {"enabled": False, "start": "10:00", "end": "19:00"},
            "sun": {"enabled": False, "start": "10:00", "end": "19:00"},
        },
    )
    # 2026-11-02 is Monday. 09:30 UTC == 12:30 Europe/Moscow (inside 10:00..19:00)
    mon_inside = datetime(2026, 11, 2, 9, 30, tzinfo=timezone.utc)
    info_in = evaluate_schedule_window(schedule, now=mon_inside)
    assert info_in["within_window"] is True
    assert info_in["reason"] == "within_window"
    assert is_within_outreach_schedule(conn, now=mon_inside) is True

    # Monday 05:00 UTC == 08:00 Europe/Moscow (before 10:00 start)
    mon_early = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
    info_early = evaluate_schedule_window(schedule, now=mon_early)
    assert info_early["within_window"] is False
    assert info_early["reason"] == "outside_hours"
    assert "Пн 10:00" in info_early["next_run_label"]

    # Wednesday 2026-11-04 is disabled in schedule above
    wed_noon = datetime(2026, 11, 4, 9, 30, tzinfo=timezone.utc)
    info_wed = evaluate_schedule_window(schedule, now=wed_noon)
    assert info_wed["within_window"] is False
    assert info_wed["reason"] == "day_disabled"


def test_autopilot_tick_respects_toggle_schedule_limits_and_jitter(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    account_id = _seed_connected_account(conn, daily_limit=2)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        camp_resp = client.post(
            "/api/campaigns",
            json={
                "name": "Автопилот Spintax",
                "account_id": account_id,
                "daily_limit": 5,
                "status": "running",
                "steps": [
                    {
                        "step_order": 1,
                        "delay_hours": 0,
                        "template_text": "{Привет|Добрый день}, {name} из {company}!",
                    },
                    {
                        "step_order": 2,
                        "delay_hours": 24,
                        "template_text": "{name}, удалось посмотреть?",
                    },
                ],
            },
        )
        assert camp_resp.status_code == 200
        camp_id = camp_resp.json()["id"]

        for uname, nm, ph, co in [
            ("user_one", "Анна", "+79991000001", "Alpha"),
            ("user_two", "Борис", "+79991000002", "Beta"),
            ("user_three", "Виктор", "+79991000003", "Gamma"),
        ]:
            client.post(
                "/api/leads",
                json={"username": uname, "name": nm, "phone": ph, "company": co},
            )
        csv_text = (
            "username,name,phone,company\n"
            "@user_one,Анна,+79991000001,Alpha\n"
            "@user_two,Борис,+79991000002,Beta\n"
            "@user_three,Виктор,+79991000003,Gamma\n"
        )
        imp = client.post(f"/api/campaigns/{camp_id}/csv", json={"csv_text": csv_text})
        assert imp.status_code == 200
        assert imp.json()["imported"] == 3

        # 1. Disable autopilot -> tick should skip
        toggle_off = client.post("/api/outreach/autopilot", json={"enabled": False})
        assert toggle_off.status_code == 200
        assert toggle_off.json()["autopilot_enabled"] is False

        mon_inside = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        res_disabled = asyncio.run(
            run_autopilot_tick(
                conn,
                now=mon_inside,
                jitter_range=(0.0, 0.0),
            )
        )
        assert res_disabled["ran"] is False
        assert res_disabled["reason"] == "autopilot_disabled"
        assert res_disabled["sent"] == 0

        # 2. Enable autopilot, but test outside schedule window (Sunday)
        set_autopilot_enabled(conn, True)
        sun_outside = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        res_outside = asyncio.run(
            run_autopilot_tick(
                conn,
                now=sun_outside,
                jitter_range=(0.0, 0.0),
            )
        )
        assert res_outside["ran"] is False
        assert res_outside["reason"] == "outside_schedule"
        assert res_outside["sent"] == 0

        # 3. Inside schedule window -> sends 2 messages (capped by account daily_limit=2)
        res_ok = asyncio.run(
            run_autopilot_tick(
                conn,
                now=mon_inside,
                jitter_range=(0.001, 0.002),
                max_per_campaign=5,
            )
        )
        assert res_ok["ran"] is True
        assert res_ok["sent"] == 2
        assert len(res_ok["campaigns"]) == 1
        assert len(res_ok["campaigns"][0]["jitter_delays_ms"]) == 2

        # Verify Spintax was expanded and lead_events has outreach_sent
        msgs = conn.execute("SELECT text FROM messages WHERE direction = 'out'").fetchall()
        assert len(msgs) == 2
        for m in msgs:
            assert "{" not in m["text"] and "}" not in m["text"]
            assert m["text"].startswith(("Привет, ", "Добрый день, "))

        out_events = conn.execute(
            "SELECT * FROM lead_events WHERE event_type = 'outreach_sent'"
        ).fetchall()
        assert len(out_events) == 2


def test_auto_stop_on_reply_updates_recipient_lead_status_and_lead_events(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    account_id = _seed_connected_account(conn, daily_limit=10)
    gateway = FakeGateway(
        code="11111",
        dialogs=[
            {
                "telegram_user_id": 900101,
                "username": "client_sync",
                "name": "Клиент Синк",
                "unread_count": 1,
                "last_from_peer": 1,
                "messages": [
                    {
                        "id": 501,
                        "direction": "in",
                        "text": "Да, давайте созвонимся завтра!",
                        "sent_at": "2026-11-02 12:05:00",
                    }
                ],
            }
        ],
    )
    app = create_app(conn=conn, gateway=gateway)

    with TestClient(app) as client:
        camp_resp = client.post(
            "/api/campaigns",
            json={
                "name": "Проверка Auto-Stop",
                "account_id": account_id,
                "daily_limit": 10,
                "status": "running",
                "steps": [
                    {
                        "step_order": 1,
                        "delay_hours": 0,
                        "template_text": "Привет, {name}!",
                    },
                    {
                        "step_order": 2,
                        "delay_hours": 0,
                        "template_text": "Фоллоу-ап для {name}",
                    },
                ],
            },
        )
        assert camp_resp.status_code == 200
        camp_id = camp_resp.json()["id"]

        for uname, nm, ph, co in [
            ("client_webhook", "Клиент Вебхук", "+79995550101", "WebCorp"),
            ("client_sync", "Клиент Синк", "+79995550102", "SyncCorp"),
            ("client_dialog", "Клиент Диалог", "+79995550103", "DialogCorp"),
        ]:
            client.post(
                "/api/leads",
                json={"username": uname, "name": nm, "phone": ph, "company": co},
            )
        csv_text = (
            "username,name,phone,company\n"
            "@client_webhook,Клиент Вебхук,+79995550101,WebCorp\n"
            "@client_sync,Клиент Синк,+79995550102,SyncCorp\n"
            "@client_dialog,Клиент Диалог,+79995550103,DialogCorp\n"
        )
        imp = client.post(f"/api/campaigns/{camp_id}/csv", json={"csv_text": csv_text})
        assert imp.status_code == 200

        # Send step 1 to all 3 leads; leads start in 'new' status
        mon_inside = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        tick1 = asyncio.run(
            run_autopilot_tick(
                conn,
                now=mon_inside,
                jitter_range=(0.0, 0.0),
                max_per_campaign=10,
            )
        )
        assert tick1["sent"] == 3

        # Set one lead to 'waiting' to verify 'waiting' -> 'talking' transition as well
        conn.execute(
            "UPDATE leads SET status = 'waiting' WHERE username = 'client_sync'"
        )
        conn.commit()

        # Case A: Incoming message via apply_incoming (live Telethon / webhook handler)
        wh_dialog = conn.execute(
            "SELECT id, peer_id FROM dialogs WHERE username = 'client_webhook'"
        ).fetchone()
        assert wh_dialog is not None
        apply_incoming(
            conn,
            account_id,
            {
                "id": 901,
                "telegram_user_id": wh_dialog["peer_id"],
                "username": "client_webhook",
                "name": "Клиент Вебхук",
                "text": "Добрый день, интересно!",
                "sent_at": "2026-11-02 12:03:00",
            },
        )

        lead_wh = conn.execute(
            "SELECT * FROM leads WHERE username = 'client_webhook'"
        ).fetchone()
        rec_wh = conn.execute(
            "SELECT * FROM campaign_recipients WHERE username = 'client_webhook'"
        ).fetchone()
        assert lead_wh["status"] == "talking"
        assert rec_wh["status"] == "replied"

        # Case B: Incoming message via sync_inbox
        sync_inbox(
            conn,
            account_id,
            [
                {
                    "telegram_user_id": 900101,
                    "username": "client_sync",
                    "name": "Клиент Синк",
                    "unread_count": 1,
                    "last_from_peer": 1,
                    "last_message_preview": "Да, давайте созвонимся завтра!",
                }
            ],
        )
        lead_sync = conn.execute(
            "SELECT * FROM leads WHERE username = 'client_sync'"
        ).fetchone()
        rec_sync = conn.execute(
            "SELECT * FROM campaign_recipients WHERE username = 'client_sync'"
        ).fetchone()
        assert lead_sync["status"] == "talking"
        assert rec_sync["status"] == "replied"

        # Case C: Incoming state via remember_dialogs
        remember_dialogs(
            conn,
            account_id,
            [
                {
                    "telegram_user_id": 900103,
                    "username": "client_dialog",
                    "name": "Клиент Диалог",
                    "unread_count": 1,
                    "last_from_peer": 1,
                    "messages": [
                        {
                            "id": 777,
                            "direction": "in",
                            "text": "Пришлите презентацию",
                            "sent_at": "2026-11-02 12:10:00",
                        }
                    ],
                }
            ],
        )
        lead_dlg = conn.execute(
            "SELECT * FROM leads WHERE username = 'client_dialog'"
        ).fetchone()
        rec_dlg = conn.execute(
            "SELECT * FROM campaign_recipients WHERE username = 'client_dialog'"
        ).fetchone()
        assert lead_dlg["status"] == "talking"
        assert rec_dlg["status"] == "replied"

        # Verify reply_received events were logged in lead_events for all 3 leads
        reply_events = conn.execute(
            "SELECT * FROM lead_events WHERE event_type = 'reply_received' ORDER BY id"
        ).fetchall()
        assert len(reply_events) == 3
        event_lead_ids = {row["lead_id"] for row in reply_events}
        assert event_lead_ids == {lead_wh["id"], lead_sync["id"], lead_dlg["id"]}

        # Subsequent autopilot tick must not send Step 2 to any of the replied recipients
        tick2 = asyncio.run(
            run_autopilot_tick(
                conn,
                now=mon_inside,
                jitter_range=(0.0, 0.0),
                max_per_campaign=10,
            )
        )
        assert tick2["sent"] == 0
