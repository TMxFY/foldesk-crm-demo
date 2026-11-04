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
        assert body["leads"] == 8
        assert body["dialogs"] == 6
        assert body["reminders"] == 4
        assert body["views"] == 2

        second = client.post("/api/demo/seed")
        assert second.status_code == 200
        account_id = second.json()["account_id"]

        accounts = client.get("/api/accounts").json()
        assert len(accounts) == 3
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
        assert len(leads) == 8
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
