from fastapi.testclient import TestClient

from app.db import connect, init_db, utc_now
from app.main import create_app
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
    account_id = cur.lastrowid
    dcur = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title,
            is_group, unread_count, last_from_peer, muted,
            created_at, updated_at
        ) VALUES (?, 'user', 810001, 'client_one', 'Клиент Один', 0, 0, 1, 0, ?, ?)
        """,
        (account_id, now, now),
    )
    conn.commit()
    return account_id, dcur.lastrowid


def test_account_daily_limit_patch_and_send_enforcement(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    account_id, dialog_id = _seed_connected_account(conn, daily_limit=20)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        r20 = client.patch(f"/api/accounts/{account_id}/limit", json={"daily_limit": 20})
        assert r20.status_code == 200
        assert r20.json()["daily_limit"] == 20

        r10 = client.patch(f"/api/accounts/{account_id}/limit", json={"daily_limit": 10})
        assert r10.status_code == 200
        assert r10.json()["daily_limit"] == 10

        bad = client.patch(f"/api/accounts/{account_id}/limit", json={"daily_limit": 0})
        assert bad.status_code == 400
        assert "Лимит должен быть от 1 до 500" in bad.json()["detail"]

        for i in range(1, 11):
            resp = client.post(
                f"/api/dialogs/{dialog_id}/messages",
                json={"text": f"Тестовое сообщение #{i}"},
            )
            assert resp.status_code == 200, resp.text

        accounts = client.get("/api/accounts").json()
        assert accounts[0]["sent_today"] == 10
        assert accounts[0]["daily_limit"] == 10

        blocked = client.post(
            f"/api/dialogs/{dialog_id}/messages",
            json={"text": "Одиннадцатое сообщение сверх лимита"},
        )
        assert blocked.status_code == 400
        assert "Достигнут суточный лимит" in blocked.json()["detail"]


def test_outreach_campaign_csv_import_and_multistep_sequence_respects_limits(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    account_id, _ = _seed_connected_account(conn, daily_limit=2)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        create_resp = client.post(
            "/api/campaigns",
            json={
                "name": "Холодный аутрич SaaS",
                "account_id": account_id,
                "daily_limit": 10,
                "steps": [
                    {
                        "step_order": 1,
                        "delay_hours": 0,
                        "template_text": "Привет, {name}! Видел проект {company}. Пообщаемся?",
                    },
                    {
                        "step_order": 2,
                        "delay_hours": 24,
                        "template_text": "{name}, возвращаюсь с коротким фоллоу-апом по {company}.",
                    },
                ],
            },
        )
        assert create_resp.status_code == 200
        camp = create_resp.json()
        camp_id = camp["id"]
        assert len(camp["steps"]) == 2

        csv_payload = (
            "username,name,phone,company,note\n"
            "@ivan_saas,Иван Петров,+79991112233,SaaSify,Фаундер\n"
            "https://t.me/olga_cloud,Ольга Сидорова,+79991112244,CloudBase,CEO\n"
            "dmitry_ai,Дмитрий Козлов,+79991112255,NeuroStack,CTO\n"
        )
        imp_resp = client.post(
            f"/api/campaigns/{camp_id}/csv",
            json={"csv_text": csv_payload},
        )
        assert imp_resp.status_code == 200
        imp_data = imp_resp.json()
        assert imp_data["imported"] == 3
        assert imp_data["total_recipients"] == 3

        leads = client.get("/api/leads").json()
        assert len(leads) == 3
        assert {l["username"] for l in leads} == {"ivan_saas", "olga_cloud", "dmitry_ai"}

        run1 = client.post(f"/api/campaigns/{camp_id}/run", json={})
        assert run1.status_code == 200
        run1_data = run1.json()
        assert run1_data["sent"] == 2
        assert run1_data["limit_reached"] is True
        assert run1_data["account"]["sent_today"] == 2
        assert run1_data["campaign"]["in_progress_count"] == 2
        assert run1_data["campaign"]["pending_count"] == 1

        run_blocked = client.post(f"/api/campaigns/{camp_id}/run", json={})
        assert run_blocked.status_code == 400
        assert "Достигнут суточный лимит аккаунта" in run_blocked.json()["detail"]

        client.patch(f"/api/accounts/{account_id}/limit", json={"daily_limit": 10})

        inbox = client.get("/api/inbox", params={"account_id": account_id}).json()
        ivan_dialog = next(d for d in inbox if d["username"] == "ivan_saas")
        msgs = client.get(f"/api/dialogs/{ivan_dialog['id']}/messages").json()
        assert len(msgs) == 1
        assert msgs[0]["text"] == "Привет, Иван Петров! Видел проект SaaSify. Пообщаемся?"

        conn.execute(
            "UPDATE dialogs SET last_from_peer = 1 WHERE id = ?",
            (ivan_dialog["id"],),
        )
        conn.commit()

        run2 = client.post(f"/api/campaigns/{camp_id}/run", json={})
        assert run2.status_code == 200
        run2_data = run2.json()
        assert run2_data["sent"] == 2
        recs = {r["username"]: r for r in run2_data["campaign"]["recipients"]}
        assert recs["ivan_saas"]["status"] == "replied"
        assert recs["ivan_saas"]["current_step"] == 1
        assert recs["olga_cloud"]["status"] == "completed"
        assert recs["olga_cloud"]["current_step"] == 2
        assert recs["dmitry_ai"]["status"] == "in_progress"
        assert recs["dmitry_ai"]["current_step"] == 1


def test_outreach_campaign_uses_real_gateway_for_connected_account(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    gateway = FakeGateway(
        code="11111",
        dialogs=[{"telegram_user_id": 42, "username": "ivan", "name": "Иван Петров"}],
    )
    app = create_app(conn=conn, gateway=gateway)

    with TestClient(app) as client:
        start_resp = client.post("/api/accounts/start", json={"phone": "+79990001122"})
        assert start_resp.status_code == 200
        account_id = start_resp.json()["id"]

        code_resp = client.post(
            f"/api/accounts/{account_id}/code", json={"code": "11111"}
        )
        assert code_resp.status_code == 200
        assert code_resp.json()["status"] == "connected"

        limit_resp = client.patch(
            f"/api/accounts/{account_id}/limit", json={"daily_limit": 10}
        )
        assert limit_resp.status_code == 200
        assert limit_resp.json()["daily_limit"] == 10

        camp_resp = client.post(
            "/api/campaigns",
            json={
                "name": "Реальная рассылка",
                "account_id": account_id,
                "daily_limit": 10,
                "steps": [
                    {
                        "step_order": 1,
                        "delay_hours": 0,
                        "template_text": "Привет, {name}! Пишу из {company}.",
                    }
                ],
            },
        )
        assert camp_resp.status_code == 200
        camp_id = camp_resp.json()["id"]

        csv_resp = client.post(
            f"/api/campaigns/{camp_id}/csv",
            json={"csv_text": "username,name,phone,company,note\n@ivan,Иван Петров,+79990001111,TechCorp,CEO"},
        )
        assert csv_resp.status_code == 200
        assert csv_resp.json()["imported"] == 1

        run_resp = client.post(f"/api/campaigns/{camp_id}/run", json={})
        assert run_resp.status_code == 200
        assert run_resp.json()["sent"] == 1
        assert len(gateway.sent) == 1
        assert gateway.sent[0]["peer_id"] == 42
        assert gateway.sent[0]["text"] == "Привет, Иван Петров! Пишу из TechCorp."

        inbox = client.get("/api/inbox", params={"account_id": account_id}).json()
        ivan_dialog = next(d for d in inbox if d["username"] == "ivan")
        assert ivan_dialog["telegram_user_id"] == 42

        msgs = client.get(f"/api/dialogs/{ivan_dialog['id']}/messages").json()
        assert len(msgs) == 1
        assert msgs[0]["text"] == "Привет, Иван Петров! Пишу из TechCorp."


def test_multi_account_campaign_round_robin_and_individual_limits(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    a1, _ = _seed_connected_account(conn, phone="+79990001001", daily_limit=2)
    a2, _ = _seed_connected_account(conn, phone="+79990001002", daily_limit=3)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        camp_resp = client.post(
            "/api/campaigns",
            json={
                "name": "Мульти-аккаунт рассылка",
                "account_ids": [a1, a2],
                "daily_limit": 20,
                "steps": [
                    {
                        "step_order": 1,
                        "delay_hours": 0,
                        "template_text": "Привет, {name}!",
                    },
                    {
                        "step_order": 2,
                        "delay_hours": 24,
                        "template_text": "Фоллоу-ап для {name}!",
                    },
                ],
            },
        )
        assert camp_resp.status_code == 200
        camp = camp_resp.json()
        assert camp["account_ids"] == [a1, a2]
        assert len(camp["accounts"]) == 2
        camp_id = camp["id"]

        csv_text = "\n".join(
            ["username,name,company"]
            + [f"@lead_{i},Лид {i},Comp {i}" for i in range(1, 7)]
        )
        imp = client.post(f"/api/campaigns/{camp_id}/csv", json={"csv_text": csv_text})
        assert imp.status_code == 200
        assert imp.json()["imported"] == 6

        run_resp = client.post(f"/api/campaigns/{camp_id}/run", json={})
        assert run_resp.status_code == 200
        run_data = run_resp.json()
        # Total quota across a1 (2) + a2 (3) = 5
        assert run_data["sent"] == 5
        assert run_data["limit_reached"] is True
        assert run_data["sent_by_account"] == {str(a1): 2, str(a2): 3}

        recs = run_data["campaign"]["recipients"]
        assigned_counts = {}
        for r in recs:
            if r["assigned_account_id"]:
                assigned_counts[r["assigned_account_id"]] = assigned_counts.get(r["assigned_account_id"], 0) + 1
        assert assigned_counts == {a1: 2, a2: 3}


def test_softugc_sample_csv_import_and_personalized_steps(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    a1, _ = _seed_connected_account(conn, phone="+79990001010", daily_limit=25)
    a2, _ = _seed_connected_account(conn, phone="+79990001020", daily_limit=25)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        sample_resp = client.get("/api/campaigns/sample-csv")
        assert sample_resp.status_code == 200
        csv_text = sample_resp.json()["csv_text"]
        assert "yorichen" in csv_text

        camp_resp = client.post(
            "/api/campaigns",
            json={
                "name": "SoftUGC B2B Outreach",
                "account_ids": [a1, a2],
                "daily_limit": 50,
                "steps": [
                    {"step_order": 1, "delay_hours": 0, "template_text": "{first_touch}"},
                    {"step_order": 2, "delay_hours": 24, "template_text": "{followup_1}"},
                ],
            },
        )
        assert camp_resp.status_code == 200
        camp_id = camp_resp.json()["id"]

        imp_resp = client.post(
            f"/api/campaigns/{camp_id}/csv",
            json={"csv_text": csv_text},
        )
        assert imp_resp.status_code == 200
        imp_data = imp_resp.json()
        assert imp_data["imported"] == 39
        updated_camp = imp_data["campaign"]
        # Automatically expanded to 3 steps because CSV has Фоллоуап 2
        assert len(updated_camp["steps"]) == 3

        by_user = {r["username"]: r for r in updated_camp["recipients"]}
        assert "yorichen" in by_user
        assert by_user["yorichen"]["company"] == "ClawsGO Science"
        assert "Getting your research AI tool seen" in by_user["yorichen"]["step1_text"]
        assert bool(by_user["yorichen"]["step2_text"])
        assert bool(by_user["yorichen"]["step3_text"])

        # Steno preamble stripped
        steno_recs = [
            r for r in updated_camp["recipients"] if "Steno" in (r.get("company") or "")
        ]
        assert len(steno_recs) == 1
        assert "Actually, here's the rewrite" not in (steno_recs[0]["step1_text"] or "")
        assert (steno_recs[0]["step1_text"] or "").startswith(
            "Hey,\n\nMarkdown editors are a crowded space"
        )

        # npmjs refusal row marked failed
        failed_recs = [r for r in updated_camp["recipients"] if r["status"] == "failed"]
        assert len(failed_recs) >= 1
        assert any("npmjs" in (r.get("company") or "").lower() or "ошибка генерации" in (r.get("error_text") or "") for r in failed_recs)

        # Run batch with max_send=10: 10 valid recipients sent, split 5/5 across a1 and a2
        run1 = client.post(f"/api/campaigns/{camp_id}/run", json={"max_send": 10})
        assert run1.status_code == 200
        r1_data = run1.json()
        assert r1_data["sent"] == 10
        assert r1_data["sent_by_account"] == {str(a1): 5, str(a2): 5}

        # Verify actual sent message uses recipient's step1_text
        yori_after = next(
            r for r in r1_data["campaign"]["recipients"] if r["username"] == "yorichen"
        )
        yori_acc = yori_after["assigned_account_id"]
        inbox = client.get("/api/inbox", params={"account_id": yori_acc}).json()
        yori_dialog = next(d for d in inbox if d["username"] == "yorichen")
        msgs = client.get(f"/api/dialogs/{yori_dialog['id']}/messages").json()
        assert len(msgs) == 1
        assert "Getting your research AI tool seen" in msgs[0]["text"]
