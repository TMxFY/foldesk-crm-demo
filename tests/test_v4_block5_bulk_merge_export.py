import csv
import io

from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app
from app.telegram_gateway import FakeGateway


def _make_app(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=FakeGateway())
    return conn, app


def test_bulk_actions_status_folder_amount_campaign_and_delete(tmp_path):
    conn, app = _make_app(tmp_path)
    now = "2026-01-01T00:00:00"
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, created_at)
        VALUES (1, '+79001112233', 'data/sessions/demo.session', 'connected', ?)
        """,
        (now,),
    )
    conn.execute(
        """
        INSERT INTO folders (id, account_id, telegram_folder_id, title, sync_enabled, created_at, updated_at)
        VALUES (10, 1, 100, 'VIP Клиенты', 1, ?, ?)
        """,
        (now, now),
    )
    conn.commit()

    with TestClient(app) as client:
        l1 = client.post(
            "/api/leads",
            json={"name": "Алексей", "username": "alex_b5", "phone": "+79990001111"},
        ).json()
        l2 = client.post(
            "/api/leads",
            json={"name": "Борис", "username": "boris_b5", "phone": "+79990002222"},
        ).json()
        l3 = client.post(
            "/api/leads",
            json={"name": "Виктор", "username": "victor_b5", "phone": "+79990003333"},
        ).json()

        ids = [l1["id"], l2["id"]]

        # 1. set_status
        res_status = client.post(
            "/api/leads/bulk",
            json={"lead_ids": ids, "action": "set_status", "status": "talking"},
        )
        assert res_status.status_code == 200
        body = res_status.json()
        assert body["ok"] is True
        assert body["updated"] == 2
        assert client.get(f"/api/leads/{l1['id']}").json()["status"] == "talking"
        assert client.get(f"/api/leads/{l2['id']}").json()["status"] == "talking"
        assert client.get(f"/api/leads/{l3['id']}").json()["status"] == "new"

        # 2. set_folder
        res_folder = client.post(
            "/api/leads/bulk",
            json={"lead_ids": ids, "action": "set_folder", "folder_id": 10},
        )
        assert res_folder.status_code == 200
        assert res_folder.json()["folder_id"] == 10
        assert client.get(f"/api/leads/{l1['id']}/card").json()["folder_name"] == "VIP Клиенты"
        assert client.get(f"/api/leads/{l2['id']}").json()["folder_id"] == 10

        # Clear folder
        res_clear_folder = client.post(
            "/api/leads/bulk",
            json={"lead_ids": [l2["id"]], "action": "set_folder", "folder_id": None},
        )
        assert res_clear_folder.status_code == 200
        assert client.get(f"/api/leads/{l2['id']}").json()["folder_id"] is None

        # 3. set_amount
        res_amount = client.post(
            "/api/leads/bulk",
            json={"lead_ids": ids, "action": "set_amount", "amount": 3500, "currency": "USD"},
        )
        assert res_amount.status_code == 200
        assert res_amount.json()["amount"] == 3500
        assert client.get(f"/api/leads/{l1['id']}").json()["amount"] == 3500
        assert client.get(f"/api/leads/{l2['id']}").json()["amount"] == 3500

        # 4. enroll_campaign
        camp = client.post(
            "/api/campaigns",
            json={
                "name": "Кампания Блок 5",
                "account_id": 1,
                "daily_limit": 20,
                "steps": [{"step_order": 1, "delay_hours": 0, "template_text": "Привет, {name}!"}],
            },
        ).json()
        res_enroll = client.post(
            "/api/leads/bulk",
            json={"lead_ids": ids, "action": "enroll_campaign", "campaign_id": camp["id"]},
        )
        assert res_enroll.status_code == 200
        assert res_enroll.json()["imported"] == 2
        camp_detail = client.get(f"/api/campaigns/{camp['id']}").json()
        assert camp_detail["total_recipients"] == 2

        # 5. delete (including lead with reminder, note, task, property)
        client.post(f"/api/leads/{l3['id']}/notes", json={"body": "Заметка перед удалением"})
        client.post(f"/api/leads/{l3['id']}/tasks", json={"title": "Задача перед удалением"})
        client.post(
            "/api/reminders",
            json={"lead_id": l3["id"], "due_at": "2099-01-01T10:00:00", "text": "Напомнить"},
        )
        res_del = client.post(
            "/api/leads/bulk",
            json={"lead_ids": [l3["id"]], "action": "delete"},
        )
        assert res_del.status_code == 200
        assert res_del.json()["deleted"] == 1
        assert client.get(f"/api/leads/{l3['id']}").status_code == 404

        # Validation errors
        assert client.post("/api/leads/bulk", json={"lead_ids": [], "action": "delete"}).status_code == 400
        assert client.post("/api/leads/bulk", json={"lead_ids": ids, "action": "unknown"}).status_code == 400
        assert client.post(
            "/api/leads/bulk",
            json={"lead_ids": ids, "action": "set_amount", "amount": -10},
        ).status_code == 400


def test_find_duplicates_and_merge_leads_full_transfer(tmp_path):
    conn, app = _make_app(tmp_path)
    now = "2026-01-01T00:00:00"
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, created_at)
        VALUES (1, '+79005556677', 'data/sessions/demo.session', 'connected', ?)
        """,
        (now,),
    )
    conn.commit()

    with TestClient(app) as client:
        # Target lead: has name, username, city property, 1 note
        target = client.post(
            "/api/leads",
            json={
                "name": "Дмитрий Основной",
                "username": "dmitry_dup",
                "status": "talking",
            },
        ).json()

        # Source duplicate lead: same username (case-insensitive) + has phone, email, url, description, amount, note, task, reminder, dialog, campaign_recipient, custom properties
        source = client.post(
            "/api/leads",
            json={
                "name": "",
                "username": "@DMITRY_DUP",
                "phone": "+79998887766",
                "email": "dmitry@example.com",
                "url": "https://dmitry.dev",
                "description": "Финтех проект",
                "note": "Важный комментарий из дубля",
                "next_step": "Отправить КП",
            },
        ).json()

        # Also a second duplicate group by phone
        p1 = client.post(
            "/api/leads",
            json={"name": "Ольга 1", "phone": "+7 (916) 123-45-67"},
        ).json()
        p2 = client.post(
            "/api/leads",
            json={"name": "Ольга 2", "phone": "89161234567"},
        ).json()

        # Set amount on source lead via bulk
        client.post(
            "/api/leads/bulk",
            json={"lead_ids": [source["id"]], "action": "set_amount", "amount": 5000},
        )

        # Add notes, tasks, reminders to both
        client.post(f"/api/leads/{target['id']}/notes", json={"body": "Заметка в таргете"})
        client.post(f"/api/leads/{source['id']}/notes", json={"body": "Заметка из дубля"})
        client.post(f"/api/leads/{source['id']}/tasks", json={"title": "Задача из дубля"})
        client.post(
            "/api/reminders",
            json={"lead_id": source["id"], "due_at": "2099-05-01T12:00:00", "text": "Позвонить из дубля"},
        )

        # Custom properties: one on target, one on source
        prop_city = client.post("/api/property-defs", json={"name": "Город", "type": "text"}).json()
        prop_inn = client.post("/api/property-defs", json={"name": "ИНН", "type": "text"}).json()
        client.put(f"/api/leads/{target['id']}/properties/{prop_city['id']}", json={"value": "Москва"})
        client.put(f"/api/leads/{source['id']}/properties/{prop_city['id']}", json={"value": "СПб"})
        client.put(f"/api/leads/{source['id']}/properties/{prop_inn['id']}", json={"value": "7701234567"})

        # Dialog & campaign recipient linked to source
        conn.execute(
            """
            INSERT INTO dialogs (id, account_id, peer_type, peer_id, username, title, lead_id, created_at, updated_at)
            VALUES (50, 1, 'user', 777888, 'dmitry_dup', 'Дмитрий', ?, ?, ?)
            """,
            (source["id"], now, now),
        )
        conn.commit()
        camp = client.post(
            "/api/campaigns",
            json={"name": "Тест Слияния", "account_id": 1, "steps": [{"step_order": 1, "delay_hours": 0, "template_text": "Тест"}]},
        ).json()
        client.post(f"/api/campaigns/{camp['id']}/from-leads", json={"lead_ids": [source["id"]]})

        # Check GET /api/leads/duplicates
        dups_resp = client.get("/api/leads/duplicates")
        assert dups_resp.status_code == 200
        dups = dups_resp.json()
        assert len(dups) == 2
        dmitry_group = next(g for g in dups if target["id"] in g["lead_ids"])
        assert source["id"] in dmitry_group["lead_ids"]
        assert "username" in dmitry_group["fields"]

        olga_group = next(g for g in dups if p1["id"] in g["lead_ids"])
        assert p2["id"] in olga_group["lead_ids"]
        assert "phone" in olga_group["fields"]

        # Merge source into target
        merge_resp = client.post(
            "/api/leads/merge",
            json={"source_lead_id": source["id"], "target_lead_id": target["id"]},
        )
        assert merge_resp.status_code == 200
        merged = merge_resp.json()
        assert merged["id"] == target["id"]
        assert merged["name"] == "Дмитрий Основной"
        assert merged["phone"] == "+79998887766"
        assert merged["email"] == "dmitry@example.com"
        assert merged["url"] == "https://dmitry.dev"
        assert merged["description"] == "Финтех проект"
        assert merged["note"] == "Важный комментарий из дубля"
        assert merged["next_step"] == "Отправить КП"
        assert merged["amount"] == 5000

        # Source lead is deleted
        assert client.get(f"/api/leads/{source['id']}").status_code == 404

        # Notes, tasks, reminders, properties, dialogs, campaign_recipients transferred
        note_bodies = [n["body"] for n in merged["notes"]]
        assert "Заметка в таргете" in note_bodies
        assert "Заметка из дубля" in note_bodies
        assert [t["title"] for t in merged["tasks"]] == ["Задача из дубля"]

        rems = client.get("/api/reminders").json()
        assert len(rems) == 1
        assert rems[0]["lead_id"] == target["id"]
        assert rems[0]["text"] == "Позвонить из дубля"

        props_by_name = {p["name"]: p["value_text"] for p in merged["properties"]}
        assert props_by_name["Город"] == "Москва"  # kept non-empty target value
        assert props_by_name["ИНН"] == "7701234567"  # backfilled from source

        dlg_lead = conn.execute("SELECT lead_id FROM dialogs WHERE id = 50").fetchone()["lead_id"]
        assert dlg_lead == target["id"]
        rec_lead = conn.execute(
            "SELECT lead_id FROM campaign_recipients WHERE campaign_id = ?", (camp["id"],)
        ).fetchone()["lead_id"]
        assert rec_lead == target["id"]

        # Now only 1 duplicate group (Olga) remains
        remaining_dups = client.get("/api/leads/duplicates").json()
        assert len(remaining_dups) == 1
        assert remaining_dups[0]["lead_ids"] == [p1["id"], p2["id"]]


def test_export_leads_csv_with_filters_amount_and_custom_properties(tmp_path):
    conn, app = _make_app(tmp_path)
    with TestClient(app) as client:
        prop_niche = client.post("/api/property-defs", json={"name": "Ниша", "type": "text"}).json()

        l1 = client.post(
            "/api/leads",
            json={
                "name": "Сергей Тех",
                "username": "sergey_tech",
                "phone": "+79001110001",
                "status": "won",
                "email": "sergey@tech.io",
                "provider": "telegram",
            },
        ).json()
        l2 = client.post(
            "/api/leads",
            json={
                "name": "Мария Ритейл",
                "phone": "+79001110002",
                "status": "new",
                "provider": "whatsapp",
            },
        ).json()

        client.post(
            "/api/leads/bulk",
            json={"lead_ids": [l1["id"]], "action": "set_amount", "amount": 12000},
        )
        client.put(
            f"/api/leads/{l1['id']}/properties/{prop_niche['id']}",
            json={"value": "SaaS B2B"},
        )

        # Full export
        resp_all = client.get("/api/leads/export.csv")
        assert resp_all.status_code == 200
        assert "text/csv" in resp_all.headers["content-type"]
        rows_all = list(csv.DictReader(io.StringIO(resp_all.text)))
        assert len(rows_all) == 2

        row_sergey = next(r for r in rows_all if r["name"] == "Сергей Тех")
        assert row_sergey["username"] == "sergey_tech"
        assert row_sergey["status"] == "won"
        assert row_sergey["amount"] == "12000"
        assert row_sergey["Ниша"] == "SaaS B2B"

        # Filtered export by status=won
        resp_won = client.get("/api/leads/export.csv?status=won")
        rows_won = list(csv.DictReader(io.StringIO(resp_won.text)))
        assert len(rows_won) == 1
        assert rows_won[0]["name"] == "Сергей Тех"

        # Filtered export by provider=whatsapp
        resp_wa = client.get("/api/leads/export.csv?provider=whatsapp")
        rows_wa = list(csv.DictReader(io.StringIO(resp_wa.text)))
        assert len(rows_wa) == 1
        assert rows_wa[0]["name"] == "Мария Ритейл"

        # Filtered export by search query q
        resp_q = client.get("/api/leads/export.csv?q=sergey_tech")
        rows_q = list(csv.DictReader(io.StringIO(resp_q.text)))
        assert len(rows_q) == 1
        assert rows_q[0]["email"] == "sergey@tech.io"


def test_merge_edge_cases_and_ui_elements(tmp_path):
    conn, app = _make_app(tmp_path)
    now = "2026-01-01T00:00:00"
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, created_at)
        VALUES
            (1, '+79001110001', 'data/sessions/demo1.session', 'connected', ?),
            (2, '+79001110002', 'data/sessions/demo2.session', 'connected', ?)
        """,
        (now, now),
    )
    conn.commit()

    with TestClient(app) as client:
        # Duplicate by telegram_user_id across two accounts
        l1 = client.post(
            "/api/leads",
            json={"account_id": 1, "telegram_user_id": 555666, "name": "Павел Акк 1"},
        ).json()
        l2 = client.post(
            "/api/leads",
            json={"account_id": 2, "telegram_user_id": 555666, "name": "Павел Акк 2", "phone": "+79995556666"},
        ).json()

        dups = client.get("/api/leads/duplicates").json()
        assert len(dups) == 1
        assert "telegram_user_id" in dups[0]["fields"]
        assert set(dups[0]["lead_ids"]) == {l1["id"], l2["id"]}

        # Cannot merge lead with itself
        self_merge = client.post(
            "/api/leads/merge",
            json={"source_lead_id": l1["id"], "target_lead_id": l1["id"]},
        )
        assert self_merge.status_code == 400

        # Cannot merge non-existent lead
        missing_merge = client.post(
            "/api/leads/merge",
            json={"source_lead_id": 999999, "target_lead_id": l1["id"]},
        )
        assert missing_merge.status_code == 404

        # Merge l2 into l1 succeeds
        merged = client.post(
            "/api/leads/merge",
            json={"source_lead_id": l2["id"], "target_lead_id": l1["id"]},
        )
        assert merged.status_code == 200
        assert merged.json()["phone"] == "+79995556666"

        # Verify UI elements exist in index.html
        html = client.get("/").text
        for element_id in (
            "leads-bulk-bar",
            "leads-duplicates-modal",
            "leads-export-csv-btn",
            "board-export-csv-btn",
            "leads-merge-dups-btn",
            "board-merge-dups-btn",
            "leads-select-all-checkbox",
            "bulk-apply-status-btn",
            "bulk-apply-folder-btn",
            "bulk-apply-amount-btn",
            "bulk-apply-campaign-btn",
            "bulk-delete-btn",
        ):
            assert f'id="{element_id}"' in html


def test_chain_merge_3_duplicates_with_conflicting_campaign_recipients_and_constraints(tmp_path):
    conn, app = _make_app(tmp_path)
    now = "2026-01-01T00:00:00"
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, created_at)
        VALUES (1, '+79007778899', 'data/sessions/demo1.session', 'connected', ?)
        """,
        (now,),
    )
    conn.commit()

    with TestClient(app) as client:
        # 1. Create a chain of 3 duplicates:
        # c1 matches c2 by username; c2 matches c3 by phone
        c1 = client.post(
            "/api/leads",
            json={
                "name": "Игорь Основатель",
                "username": "igor_chain",
                "note": "Инлайн заметка 1",
            },
        ).json()
        c2 = client.post(
            "/api/leads",
            json={
                "name": "",
                "username": "@IGOR_CHAIN",
                "phone": "+7 (999) 444-55-66",
                "note": "Инлайн заметка 2",
            },
        ).json()
        c3 = client.post(
            "/api/leads",
            json={
                "name": "Игорь Тел",
                "phone": "89994445566",
                "telegram_user_id": 777111,
                "email": "igor@chain.io",
                "note": "Инлайн заметка 3",
            },
        ).json()

        # Verify all 3 are grouped into a single transitive duplicate group
        dups = client.get("/api/leads/duplicates").json()
        assert len(dups) == 1
        assert dups[0]["lead_ids"] == [c1["id"], c2["id"], c3["id"]]
        assert set(dups[0]["fields"]) == {"username", "phone"}

        # Enroll c1 into a campaign (gives c1 synthetic telegram_user_id = 900000 + c1["id"])
        camp = client.post(
            "/api/campaigns",
            json={
                "name": "Цепочка 3 дубля",
                "account_id": 1,
                "steps": [{"step_order": 1, "delay_hours": 0, "template_text": "Привет!"}],
            },
        ).json()
        client.post(
            "/api/leads/bulk",
            json={"lead_ids": [c1["id"]], "action": "enroll_campaign", "campaign_id": camp["id"]},
        )

        # Also insert conflicting campaign_recipients rows in the SAME campaign for c2 and c3
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, lead_id, username, phone, status, current_step,
                hook, last_sent_at, created_at, updated_at
            ) VALUES
                (?, ?, 'igor_chain', '+79994445566', 'in_progress', 1, 'Хук из c2', '2026-01-02T10:00:00', ?, ?),
                (?, ?, 'igor_chain', '+79994445566', 'replied', 2, NULL, '2026-01-03T12:00:00', ?, ?)
            """,
            (camp["id"], c2["id"], now, now, camp["id"], c3["id"], now, now),
        )
        conn.commit()

        # Merge c2 -> c1, then c3 -> c1
        m1 = client.post(
            "/api/leads/merge",
            json={"source_lead_id": c2["id"], "target_lead_id": c1["id"]},
        )
        assert m1.status_code == 200
        m2 = client.post(
            "/api/leads/merge",
            json={"source_lead_id": c3["id"], "target_lead_id": c1["id"]},
        )
        assert m2.status_code == 200
        final_card = m2.json()

        # Real telegram_user_id (777111) from c3 replaced synthetic 900000 + c1["id"]
        assert final_card["telegram_user_id"] == 777111
        assert final_card["email"] == "igor@chain.io"

        # All 3 distinct inline notes were preserved in notes list
        note_bodies = [n["body"] for n in final_card["notes"]]
        assert "Инлайн заметка 1" in note_bodies
        assert "Инлайн заметка 2" in note_bodies
        assert "Инлайн заметка 3" in note_bodies

        # Campaign recipients for camp["id"] deduplicated into 1 row with status='replied', step=2, hook='Хук из c2'
        rec_rows = conn.execute(
            "SELECT * FROM campaign_recipients WHERE campaign_id = ?",
            (camp["id"],),
        ).fetchall()
        assert len(rec_rows) == 1
        assert rec_rows[0]["lead_id"] == c1["id"]
        assert rec_rows[0]["status"] == "replied"
        assert rec_rows[0]["current_step"] == 2
        assert rec_rows[0]["hook"] == "Хук из c2"
        assert rec_rows[0]["last_sent_at"] == "2026-01-03T12:00:00"

        # 2. Test (account_id, telegram_user_id) conflict when target has (None, 888999),
        # source has (1, 999111), and another lead already has (1, 888999)
        other_lead = client.post(
            "/api/leads",
            json={"account_id": 1, "telegram_user_id": 888999, "name": "Чужой с тем же TG"},
        ).json()
        assert other_lead["id"] > 0
        t_conflict = client.post(
            "/api/leads",
            json={"telegram_user_id": 888999, "name": "Таргет без аккаунта"},
        ).json()
        s_conflict = client.post(
            "/api/leads",
            json={"account_id": 1, "telegram_user_id": 999111, "name": "Сорс с аккаунтом 1", "phone": "+79990009999"},
        ).json()
        res_conflict_merge = client.post(
            "/api/leads/merge",
            json={"source_lead_id": s_conflict["id"], "target_lead_id": t_conflict["id"]},
        )
        assert res_conflict_merge.status_code == 200
        assert res_conflict_merge.json()["phone"] == "+79990009999"

        # 3. Formatted money string in bulk set_amount ("$2,500")
        res_fmt_amt = client.post(
            "/api/leads/bulk",
            json={"lead_ids": [c1["id"]], "action": "set_amount", "amount": "$2,500"},
        )
        assert res_fmt_amt.status_code == 200
        assert res_fmt_amt.json()["amount"] == 2500


