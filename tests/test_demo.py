from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app


def test_demo_seed_populates_workspace_and_is_idempotent(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)
    with TestClient(app) as client:
        first = client.post("/api/demo/seed")
        assert first.status_code == 200
        body = first.json()
        assert body["ok"] is True
        assert body["folders"] == 3
        assert body["leads"] == 11
        assert body["dialogs"] == 9
        assert body["reminders"] == 4
        assert body["views"] == 2

        second = client.post("/api/demo/seed")
        assert second.status_code == 200
        account_id = second.json()["account_id"]

        accounts = client.get("/api/accounts").json()
        assert len(accounts) == 4
        assert accounts[0]["phone"] == "+79995550101"
        assert accounts[0]["status"] == "connected"
        assert accounts[0]["health"] == "active"
        assert accounts[0]["telegram_username"] == "tim_sales"
        assert accounts[0]["telegram_name"] == "Тимофей · Sales"
        assert accounts[0]["daily_limit"] == 20
        assert accounts[0]["sent_today"] == 7
        assert accounts[0]["is_demo"] is True
        assert accounts[0]["connection_label"] == "Демо-профиль (локально)"
        assert accounts[1]["phone"] == "+79995550202"
        assert accounts[1]["telegram_username"] == "anna_outreach"
        assert accounts[1]["daily_limit"] == 10
        assert accounts[1]["sent_today"] == 4
        assert accounts[2]["phone"] == "+79990003344"
        assert accounts[2]["telegram_username"] == "maxim_growth_tg"
        assert accounts[2]["telegram_name"] == "Максим · Growth"
        assert accounts[2]["daily_limit"] == 15
        assert accounts[2]["sent_today"] == 3
        assert accounts[3]["phone"] == "+79990007788"
        assert accounts[3]["telegram_name"] == "Timofei · WhatsApp Business"
        assert accounts[3]["daily_limit"] == 25
        assert accounts[3]["sent_today"] == 5

        campaigns = client.get("/api/campaigns").json()
        assert len(campaigns) == 2
        assert {c["name"] for c in campaigns} == {
            "Web3 Founders · Осень",
            "Партнёрские агентства · Follow-up",
        }
        web3_camp = next(c for c in campaigns if c["name"] == "Web3 Founders · Осень")
        assert len(web3_camp["account_ids"]) == 3
        assert len(web3_camp["accounts"]) == 3
        assert any(r["assigned_account_name"] for r in web3_camp["recipients"])

        folders = client.get("/api/folders", params={"account_id": account_id}).json()
        assert len(folders) == 3
        assert {f["title"] for f in folders} == {
            "VIP Клиенты",
            "Web3 & SaaS",
            "Партнёры и Агентства",
        }

        leads = client.get("/api/leads").json()
        assert len(leads) == 11
        assert {lead["provider"] for lead in leads} == {"telegram", "whatsapp"}
        wa_leads = [lead for lead in leads if lead["provider"] == "whatsapp"]
        assert len(wa_leads) == 3
        assert {lead["status"] for lead in leads} == {
            "new",
            "talking",
            "waiting",
            "won",
            "lost",
        }
        sample_card = client.get(f"/api/leads/{leads[0]['id']}/card").json()
        assert sample_card["folder_name"] in {
            "VIP Клиенты",
            "Web3 & SaaS",
            "Партнёры и Агентства",
        }
        assert len(sample_card["notes"]) >= 1
        assert len(sample_card["tasks"]) >= 1
        prop_names = {p["name"]: p["value_text"] for p in sample_card["properties"]}
        assert "Сумма сделки" in prop_names
        assert "Компания" in prop_names
        assert prop_names["Сумма сделки"].startswith("$")

        inbox = client.get("/api/inbox", params={"account_id": account_id}).json()
        assert len(inbox) == 6
        unreads = sorted(d["unread_count"] for d in inbox if d["unread_count"] > 0)
        assert unreads == [1, 3]

        thread = client.get(f"/api/dialogs/{inbox[0]['id']}/messages")
        assert thread.status_code == 200
        messages = thread.json()
        assert len(messages) >= 2
        assert {m["direction"] for m in messages} == {"in", "out"}

        open_reminders = client.get("/api/reminders", params={"scope": "open"}).json()
        overdue_reminders = client.get("/api/reminders", params={"scope": "overdue"}).json()
        assert len(open_reminders) == 4
        assert len(overdue_reminders) == 1

        views = client.get("/api/views").json()
        assert [v["name"] for v in views] == ["VIP в работе", "Ожидают оплаты"]


def test_whatsapp_leads_inbox_and_outreach_flow(tmp_path):
    import asyncio
    from app.inbox import list_inbox, open_dialog_by_lead, remember_dialogs, send_reply
    from app.leads import add_lead_from_dialog, create_lead, search_leads
    from app.outreach import (
        create_campaign,
        enroll_leads_from_crm,
        import_campaign_csv,
        run_campaign_batch,
    )

    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)
    with TestClient(app) as client:
        client.post("/api/demo/seed")
        wa_acc = conn.execute(
            "SELECT * FROM accounts WHERE provider = 'whatsapp'"
        ).fetchone()
        assert wa_acc is not None
        wa_acc_id = int(wa_acc["id"])

        # 1. Create WhatsApp lead with only phone and name
        wa_lead = create_lead(
            conn,
            account_id=wa_acc_id,
            name="Сергей Логинов · WA Cargo",
            phone="+79994445566",
        )
        assert wa_lead["provider"] == "whatsapp"
        assert wa_lead["phone"] == "+79994445566"
        assert wa_lead["telegram_user_id"] == 79994445566

        # 2. Search leads by provider and phone
        wa_only = search_leads(conn, provider="whatsapp")
        assert len(wa_only) == 4
        by_phone = search_leads(conn, q="9994445566", provider="whatsapp")
        assert [r["id"] for r in by_phone] == [wa_lead["id"]]

        # 3. Open dialog by WhatsApp lead
        dlg = open_dialog_by_lead(conn, wa_lead["id"])
        assert dlg["provider"] == "whatsapp"
        assert dlg["phone"] == "+79994445566"
        assert dlg["account_id"] == wa_acc_id
        assert dlg["telegram_user_id"] == 79994445566

        # 4. Remember dialogs and add_lead_from_dialog for WhatsApp
        remember_dialogs(
            conn,
            wa_acc_id,
            [{"telegram_user_id": 79997778899, "name": "Олег WA", "phone": "+79997778899"}],
        )
        from_dlg = add_lead_from_dialog(conn, wa_acc_id, 79997778899)
        assert from_dlg["provider"] == "whatsapp"
        assert from_dlg["phone"] == "+79997778899"

        # 5. Unified inbox includes WhatsApp dialogs and filters by phone
        wa_inbox = list_inbox(conn, account_id=wa_acc_id, q="4445566")
        assert len(wa_inbox) == 1
        assert wa_inbox[0]["provider"] == "whatsapp"

        # 6. WhatsApp Outreach CSV (wa_phone header) + batch run + daily limit increment
        camp = create_campaign(
            conn,
            name="WhatsApp B2B Outreach",
            account_ids=[wa_acc_id],
            daily_limit=20,
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Здравствуйте, {name}! Пишем в WhatsApp."}],
        )
        csv_res = import_campaign_csv(
            conn,
            camp["id"],
            "name,wa_phone,company\nИгорь Склад,+79998881122,SkladPro\n",
        )
        assert csv_res["imported"] == 1
        en_res = enroll_leads_from_crm(conn, camp["id"], status="new")
        assert en_res["imported_count"] >= 2

        batch_res = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=None, max_send=2))
        assert batch_res["sent"] == 2
        assert batch_res["account"]["sent_today"] == 7  # 5 seeded + 2 sent

        sent_msg = asyncio.run(send_reply(conn, dlg["id"], "Тест ответа в WhatsApp", None))
        assert sent_msg["direction"] == "out"
