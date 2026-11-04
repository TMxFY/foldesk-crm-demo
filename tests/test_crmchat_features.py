import base64
import io
import json
import zipfile

from fastapi.testclient import TestClient

from app.db import connect, init_db, resolve_session
from app.main import create_app
from app.telegram_gateway import FakeGateway


def test_import_sessions_json_and_zip_plus_account_settings(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    gateway = FakeGateway(code="11111")
    app = create_app(conn=conn, gateway=gateway)

    with TestClient(app) as client:
        # 1. Start login with proxy_url and access_scope
        start_resp = client.post(
            "/api/accounts/start",
            json={
                "phone": "+79990001122",
                "proxy_url": "socks5://127.0.0.1:1080",
                "access_scope": "personal",
            },
        )
        assert start_resp.status_code == 200
        started = start_resp.json()
        assert started["proxy_url"] == "socks5://127.0.0.1:1080"
        assert started["access_scope"] == "personal"

        # 2. Import session via JSON sessions list
        imp_resp = client.post(
            "/api/accounts/import-session",
            json={
                "sessions": [
                    {
                        "phone": "+79991112233",
                        "username": "alex_outreach",
                        "first_name": "Alex",
                        "last_name": "SoftUGC",
                        "telegram_user_id": 77001122,
                        "session_string": "1BVtsOHwButest_session_string",
                        "filename": "test_79991112233.session",
                    }
                ],
                "proxy_url": "socks5://user:pass@10.0.0.1:1080",
                "access_scope": "workspace",
                "daily_limit": 35,
            },
        )
        assert imp_resp.status_code == 200
        imp_data = imp_resp.json()
        assert imp_data["count"] == 1
        acc1 = imp_data["imported"][0]
        assert acc1["phone"] == "+79991112233"
        assert acc1["telegram_username"] == "alex_outreach"
        assert acc1["telegram_name"] == "Alex SoftUGC"
        assert acc1["telegram_user_id"] == 77001122
        assert acc1["status"] == "connected"
        assert acc1["health"] == "active"
        assert acc1["proxy_url"] == "socks5://user:pass@10.0.0.1:1080"
        assert acc1["access_scope"] == "workspace"
        assert acc1["daily_limit"] == 35
        assert acc1["session_exists"] is True

        # 3. Import session via ZIP archive (phone.json + phone.session)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "79994445566.json",
                json.dumps(
                    {
                        "phone": "79994445566",
                        "id": 77004455,
                        "username": "maria_zip_acc",
                        "first_name": "Maria",
                        "last_name": "Growth",
                        "app_id": 2040,
                        "app_hash": "b18441a1ff607e10a989891a5462e627",
                        "proxy": [2, "192.168.1.10", 1080, True, "tguser", "tgpass"],
                    }
                ),
            )
            zf.writestr("79994445566.session", b"SQLite format 3\x00zip_session_bytes")
        zip_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        zip_resp = client.post(
            "/api/accounts/import-session",
            json={
                "zip_base64": zip_b64,
                "access_scope": "workspace",
                "daily_limit": 40,
            },
        )
        assert zip_resp.status_code == 200
        zip_data = zip_resp.json()
        assert zip_data["count"] == 1
        acc2 = zip_data["imported"][0]
        assert acc2["phone"] == "+79994445566"
        assert acc2["telegram_username"] == "maria_zip_acc"
        assert acc2["telegram_name"] == "Maria Growth"
        assert acc2["telegram_user_id"] == 77004455
        assert acc2["status"] == "connected"
        assert acc2["health"] == "active"
        assert acc2["proxy_url"] == "socks5://tguser:tgpass@192.168.1.10:1080"
        assert acc2["daily_limit"] == 40

        # 4. PATCH /api/accounts/{account_id}/settings
        patch_resp = client.patch(
            f"/api/accounts/{acc2['id']}/settings",
            json={
                "proxy_url": "http://proxy.foldesk.io:8080",
                "access_scope": "personal",
                "daily_limit": 45,
            },
        )
        assert patch_resp.status_code == 200
        patched = patch_resp.json()
        assert patched["proxy_url"] == "http://proxy.foldesk.io:8080"
        assert patched["access_scope"] == "personal"
        assert patched["daily_limit"] == 45

        all_accs = client.get("/api/accounts").json()
        assert len(all_accs) == 3

        # Cleanup test session files created on disk
        for stem in ("test_79991112233", "79994445566"):
            p = resolve_session(f"data/sessions/{stem}.session")
            if p.exists():
                p.unlink()


def test_outreach_sending_schedule_get_and_put(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        get_resp = client.get("/api/outreach/schedule")
        assert get_resp.status_code == 200
        sched = get_resp.json()
        assert sched["timezone"] == "UTC"
        assert sched["days"]["mon"]["enabled"] is True
        assert sched["days"]["mon"]["start"] == "09:00"
        assert sched["days"]["sat"]["enabled"] is False
        assert sched["days"]["sun"]["enabled"] is False

        put_resp = client.put(
            "/api/outreach/schedule",
            json={
                "timezone": "Europe/Moscow",
                "days": {
                    "mon": {"enabled": True, "start": "10:00", "end": "19:00"},
                    "sat": {"enabled": True, "start": "11:00", "end": "15:00"},
                },
            },
        )
        assert put_resp.status_code == 200
        updated = put_resp.json()
        assert updated["timezone"] == "Europe/Moscow"
        assert updated["days"]["mon"] == {"enabled": True, "start": "10:00", "end": "19:00"}
        assert updated["days"]["sat"] == {"enabled": True, "start": "11:00", "end": "15:00"}
        assert updated["days"]["tue"]["enabled"] is True


def test_preview_csv_column_mapping_and_enroll_leads_from_crm(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        client.post("/api/demo/seed")
        accounts = client.get("/api/accounts").json()
        a1 = accounts[0]["id"]

        # 1. Preview SoftUGC sample CSV (39 rows)
        sample_csv = client.get("/api/campaigns/sample-csv").json()["csv_text"]
        prev_resp = client.post("/api/campaigns/preview-csv", json={"csv_text": sample_csv})
        assert prev_resp.status_code == 200
        prev = prev_resp.json()
        assert prev["total_rows"] == 39
        assert len(prev["sample_rows"]) == 5
        assert prev["detected_mapping"]["username"] == "Контакт"
        assert prev["detected_mapping"]["company"] == "Название"
        assert prev["detected_mapping"]["step1_text"] == "Первое касание"
        assert prev["detected_mapping"]["step2_text"] == "Фоллоуап 1"
        assert prev["detected_mapping"]["step3_text"] == "Фоллоуап 2"

        # 2. Custom headers CSV with manual column_mapping
        custom_csv = (
            "tg_handle,founder_name,brand_title,mobile_num,custom_pitch\n"
            "@founder_one,Алексей Один,AlphaTech,+79998887766,Персональный оффер для AlphaTech\n"
            "@founder_two,Борис Два,BetaCloud,+79998887755,Персональный оффер для BetaCloud\n"
        )
        camp_resp = client.post(
            "/api/campaigns",
            json={
                "name": "Кастомный маппинг CSV",
                "account_ids": [a1],
                "daily_limit": 25,
            },
        )
        camp_id = camp_resp.json()["id"]

        imp_custom = client.post(
            f"/api/campaigns/{camp_id}/csv",
            json={
                "csv_text": custom_csv,
                "column_mapping": {
                    "username": "tg_handle",
                    "name": "founder_name",
                    "company": "brand_title",
                    "phone": "mobile_num",
                    "step1_text": "custom_pitch",
                },
            },
        )
        assert imp_custom.status_code == 200
        assert imp_custom.json()["imported"] == 2
        recs = imp_custom.json()["campaign"]["recipients"]
        assert recs[0]["username"] == "founder_one"
        assert recs[0]["company"] == "AlphaTech"
        assert recs[0]["step1_text"] == "Персональный оффер для AlphaTech"

        # 3. Enroll leads directly from CRM (by status or lead_ids)
        talking_enroll = client.post(
            f"/api/campaigns/{camp_id}/from-leads",
            json={"status": "talking"},
        )
        assert talking_enroll.status_code == 200
        en_data = talking_enroll.json()
        assert en_data["imported_count"] == 2
        assert en_data["skipped_count"] == 0
        assert en_data["campaign"]["total_recipients"] == 4

        # Re-enrolling same leads skips duplicates
        dup_enroll = client.post(
            f"/api/campaigns/{camp_id}/from-leads",
            json={"status": "talking"},
        )
        assert dup_enroll.status_code == 200
        assert dup_enroll.json()["imported_count"] == 0
        assert dup_enroll.json()["skipped_count"] == 2


def test_live_tools_phone_converter_and_group_parser(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        client.post("/api/demo/seed")
        accounts = client.get("/api/accounts").json()
        a1 = accounts[0]["id"]

        camp = client.post(
            "/api/campaigns",
            json={"name": "Tools Outreach", "account_ids": [a1], "daily_limit": 30},
        ).json()
        camp_id = camp["id"]

        # 1. Phone converter with normalization, deduplication, CRM leads + campaign enrollment
        phones_resp = client.post(
            "/api/tools/phone-converter",
            json={
                "phones_text": "+1 (202) 555-0100\n8 (999) 123-45-67, +7 999 123-45-67\n+44 20 7946 0958",
                "account_id": a1,
                "add_to_leads": True,
                "campaign_id": camp_id,
            },
        )
        assert phones_resp.status_code == 200
        pdata = phones_resp.json()
        assert pdata["total_input"] == 4
        assert pdata["unique_valid"] == 3
        assert pdata["found_count"] == 3
        assert pdata["added_to_leads"] == 3
        assert pdata["added_to_campaign"] == 3
        norm_set = {r["phone"] for r in pdata["results"]}
        assert norm_set == {"+12025550100", "+79991234567", "+442079460958"}
        assert all(r["username"] and r["telegram_user_id"] for r in pdata["results"])

        # 2. Group parser with limit, CRM leads + campaign enrollment
        grp_resp = client.post(
            "/api/tools/group-parser",
            json={
                "group_link": "https://t.me/b2b_saas_founders",
                "account_id": a1,
                "limit": 8,
                "add_to_leads": True,
                "campaign_id": camp_id,
            },
        )
        assert grp_resp.status_code == 200
        gdata = grp_resp.json()
        assert gdata["participants_count"] == 8
        assert gdata["added_to_leads"] == 8
        assert gdata["added_to_campaign"] == 8
        assert "B2b Saas Founders" in gdata["group_title"]
        assert all(
            p.get("username") and p.get("name") and p.get("role") in ("founder", "admin", "member")
            for p in gdata["participants"]
        )

        updated_camp = client.get(f"/api/campaigns/{camp_id}").json()
        assert updated_camp["total_recipients"] == 11


def test_multi_session_inbox_open_by_lead_and_property_defs(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        client.post("/api/demo/seed")
        accounts = client.get("/api/accounts").json()
        a1, a2 = accounts[0]["id"], accounts[1]["id"]

        # Create a lead without an existing dialog (e.g. from CSV/Parser)
        new_lead = client.post(
            "/api/leads",
            json={
                "name": "Николай Ветров · AI Studio",
                "username": "@nikolay_ai_studio",
                "phone": "+79995559988",
                "note": "Интересует тест рассылки",
            },
        ).json()

        # Open dialog for this lead on second Telegram session (a2)
        open_resp = client.post(
            "/api/dialogs/open-by-lead",
            json={"lead_id": new_lead["id"], "account_id": a2},
        )
        assert open_resp.status_code == 200
        dlg = open_resp.json()
        assert dlg["account_id"] == a2
        assert dlg["lead_id"] == new_lead["id"]
        assert dlg["username"] == "nikolay_ai_studio"
        assert dlg["account_username"] == "anna_outreach"

        # Send a message directly from the platform on session a2
        send_resp = client.post(
            f"/api/dialogs/{dlg['id']}/messages",
            json={"text": "Николай, привет! Пишу прямо из Foldesk со второй сессии."},
        )
        assert send_resp.status_code == 200
        assert send_resp.json()["direction"] == "out"

        # Query multi-session inbox (account_id=0 -> All sessions)
        all_inbox = client.get("/api/inbox", params={"account_id": 0}).json()
        assert len(all_inbox) == 10  # 6 seeded on a1 + 3 WhatsApp seeded + 1 created on a2
        account_ids_in_inbox = {d["account_id"] for d in all_inbox}
        assert {a1, a2}.issubset(account_ids_in_inbox)
        assert all(d.get("account_phone") and d.get("account_name") for d in all_inbox)

        # Query inbox for session a2 specifically
        a2_inbox = client.get("/api/inbox", params={"account_id": a2}).json()
        assert len(a2_inbox) == 1
        assert a2_inbox[0]["id"] == dlg["id"]

        # Property definitions GET and DELETE
        props = client.get("/api/property-defs").json()
        assert len(props) >= 2
        created_prop = client.post(
            "/api/property-defs",
            json={"name": "Источник трафика", "type": "text", "object_type": "lead"},
        ).json()
        props_after_create = client.get("/api/property-defs").json()
        assert any(p["id"] == created_prop["id"] for p in props_after_create)

        del_resp = client.delete(f"/api/property-defs/{created_prop['id']}")
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted"] is True

        props_after_del = client.get("/api/property-defs").json()
        assert not any(p["id"] == created_prop["id"] for p in props_after_del)
