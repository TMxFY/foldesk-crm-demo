import json
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.leads import create_lead
from app.main import create_app
from app.outreach import infer_recipient_audience, render_template

VLAD_FOLLOWUP_2 = (
    "Hey, regarding last message, we could bring you 500K-2M reach to your targeted audience "
    "(which equals highly qualified {audience})\n\n"
    "So if you’re down just lmk if you wanna see the demo reels and I’ll send them to you, "
    "so we could break the ice here"
)


def _seed_connected_account(conn, phone="+79990005566", daily_limit=50):
    now = "2026-11-09T12:00:00Z"
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, telegram_user_id, telegram_username, telegram_name,
            status, health, daily_limit, sent_today, sent_date, created_at, updated_at
        ) VALUES (?, 'data/sessions/demo_aud.session', 777001, 'vlad_outreach', 'Vlad Outreach',
                  'connected', 'active', ?, 0, NULL, ?, ?)
        """,
        (phone, daily_limit, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_infer_recipient_audience_across_all_five_niches_and_false_positive_guards():
    # 1. buyers: E-com, Shopify, physical goods, agencies
    assert infer_recipient_audience({
        "company": "GlowSkin",
        "category": "E-com",
        "their_angle": "D2C skincare store on Shopify",
    }) == "buyers"
    assert infer_recipient_audience({
        "company": "Verto Studio",
        "category": "Агентство",
        "their_angle": "Помогает бизнесам увеличить поток клиентов",
    }) == "buyers"

    # 2. students: EdTech, courses, online schools
    assert infer_recipient_audience({
        "company": "Threads Academy",
        "category": "Медиа",
        "extra_json": json.dumps({"niche": "EdTech / Online Education"}),
    }) == "students"
    assert infer_recipient_audience({
        "company": "Константин",
        "lead_niche": "Инфопродукт: курс 30 000 ₽ (позже 50 000 ₽)",
    }) == "students"

    # 3. players: Gaming, iGaming, browser .io shooter
    assert infer_recipient_audience({
        "company": "pewpewpew",
        "category": "AI",
        "their_angle": "Free multiplayer space shooter .io game in your browser.",
    }) == "players"
    assert infer_recipient_audience({
        "company": "Роблокс",
        "lead_niche": "Gaming",
    }) == "players"

    # 4. listeners: Media, podcasts, music, audio
    assert infer_recipient_audience({
        "company": "hackerbooks",
        "category": "Медиа",
        "their_angle": "Podcast episodes and daily audio AMAs",
    }) == "listeners"
    assert infer_recipient_audience({
        "company": "SoundWave",
        "their_angle": "Independent music label and Spotify playlists",
    }) == "listeners"

    # 5. users: SaaS, AI, Web3, DeFi, Fintech + false-positive guards
    # Guard A: LLM fine-tuning ("дообучения") and "машинного обучения" must NOT become "students"
    assert infer_recipient_audience({
        "company": "Orchestra",
        "category": "SaaS",
        "their_angle": "Платформа для маршрутизации и дообучения LLM-моделей",
    }) == "users"
    assert infer_recipient_audience({
        "company": "Xulux",
        "category": "SaaS",
        "note": (
            "Их угол: Платформа тестирования devtools для AI-агентов.\n"
            "Наш угол: Подойдёт запуск серии образовательных клипов про тестирование AI-агентов."
        ),
    }) == "users"
    # Guard B: Non-gaming CEX/SaaS with noisy extra_json niche "Gaming / iGaming / GameDev" due to .io domain
    assert infer_recipient_audience({
        "company": "GoModel",
        "category": "SaaS",
        "their_angle": "Открытый шлюз API для 31 AI-провайдера",
        "extra_json": json.dumps({"niche": "Gaming / iGaming / GameDev"}),
    }) == "users"
    # Guard C: SaaS creator studio mentioning Spotify in passing stays "users"
    assert infer_recipient_audience({
        "company": "Riverside Agents",
        "category": "SaaS",
        "their_angle": "Онлайн-студия для записи видео и публикации на YouTube/Spotify",
    }) == "users"
    # Guard D: Non-audio "Медиа" (news portals, Mac apps) stays "users" instead of "listeners"
    assert infer_recipient_audience({
        "company": "Cryptopolitan",
        "category": "Медиа",
        "their_angle": "Crypto news portal covering exchanges and regulation",
    }) == "users"
    # Guard E: AI startups with noisy .io extra_json gaming tag, migration substring, FX rates, "of course", AI coach
    assert infer_recipient_audience({
        "company": "BukProtocol",
        "category": "AI",
        "their_angle": "Travel+AI Web3 agents",
        "extra_json": json.dumps({"niche": "Gaming / iGaming / GameDev"}),
    }) == "users"
    assert infer_recipient_audience({
        "company": "RelocatePro",
        "lead_niche": "Иммиграция и релокация IT-специалистов",
    }) == "users"
    assert infer_recipient_audience({
        "company": "Crypto.ru",
        "category": "Медиа",
        "their_angle": "Медиа о биржах, курсах валют и прогнозах. Of course free AI-коуч и тонкая настройка.",
    }) == "users"
    assert infer_recipient_audience({
        "company": "ScaleStudio",
        "lead_niche": "B2B Marketing Agency",
    }) == "buyers"

    # 6. Manual CSV override wins over automatic inference
    rendered_override = render_template(
        VLAD_FOLLOWUP_2,
        {
            "company": "Acme SaaS",
            "category": "SaaS",
            "extra_json": json.dumps({"audience": "buyers"}),
        },
    )
    assert "(which equals highly qualified buyers)" in rendered_override
    assert "{audience}" not in rendered_override


def test_custom_followup2_template_with_audience_overrides_stale_csv_step3_text(tmp_path):
    conn = connect(tmp_path / "crm_aud.db")
    init_db(conn)
    acc_id = _seed_connected_account(conn)
    app = create_app(conn=conn, gateway=None)

    csv_payload = (
        "Категория,Название,Контакт,Их угол,Первое касание,Фоллоуап 1,Фоллоуап 2\n"
        "AI,pewpewpew,@game_founder,Free multiplayer space shooter game in browser,Hi game founder!,Step 2 old,Old generic step 3 text\n"
        "Агентство,Verto Studio,@agency_founder,Помогает бизнесам увеличить поток клиентов,Hi agency!,Step 2 old,Old generic step 3 text\n"
        "Медиа,PodCastHub,@pod_founder,Daily tech podcast episodes,Hi podcaster!,Step 2 old,Old generic step 3 text\n"
    )

    with TestClient(app) as client:
        camp_resp = client.post(
            "/api/campaigns",
            json={
                "name": "Vlad Followup 2 Audience Test",
                "account_ids": [acc_id],
                "daily_limit": 50,
                "steps": [
                    {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
                    {"step_order": 2, "delay_hours": 24, "template_text": "{followup_1}"},
                    {"step_order": 3, "delay_hours": 48, "template_text": "{followup_2}"},
                ],
            },
        )
        assert camp_resp.status_code == 200
        cid = camp_resp.json()["id"]

        imp = client.post(f"/api/campaigns/{cid}/csv", json={"csv_text": csv_payload})
        assert imp.status_code == 200
        assert imp.json()["imported"] == 3

        # Vlad updates Step 3 (2nd follow-up) in UI settings to the new {audience} template
        upd = client.put(
            f"/api/campaigns/{cid}",
            json={
                "steps": [
                    {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
                    {"step_order": 2, "delay_hours": 24, "template_text": "{followup_1}"},
                    {"step_order": 3, "delay_hours": 48, "template_text": VLAD_FOLLOWUP_2},
                ]
            },
        )
        assert upd.status_code == 200

        # Run step 1, step 2, and step 3
        for _ in range(3):
            r = client.post(f"/api/campaigns/{cid}/run", json={})
            assert r.status_code == 200
            assert r.json()["sent"] == 3

        inbox = {d["username"]: d["id"] for d in client.get("/api/inbox", params={"account_id": acc_id}).json()}
        game_msgs = client.get(f"/api/dialogs/{inbox['game_founder']}/messages").json()
        agency_msgs = client.get(f"/api/dialogs/{inbox['agency_founder']}/messages").json()
        pod_msgs = client.get(f"/api/dialogs/{inbox['pod_founder']}/messages").json()

        assert "(which equals highly qualified players)" in game_msgs[-1]["text"]
        assert "Old generic step 3 text" not in game_msgs[-1]["text"]
        assert "(which equals highly qualified buyers)" in agency_msgs[-1]["text"]
        assert "(which equals highly qualified listeners)" in pod_msgs[-1]["text"]


def test_enroll_from_crm_leads_propagates_niche_into_audience_placeholder(tmp_path):
    conn = connect(tmp_path / "crm_leads_aud.db")
    init_db(conn)
    acc_id = _seed_connected_account(conn)
    l_course = create_lead(
        conn,
        account_id=acc_id,
        username="course_creator",
        name="Константин",
        niche="Инфопродукт: курс 30 000 ₽",
    )
    l_saas = create_lead(
        conn,
        account_id=acc_id,
        username="saas_founder",
        name="Alex",
        niche="B2B SaaS",
    )
    l_ecom = create_lead(
        conn,
        account_id=acc_id,
        username="shop_owner",
        name="Андрей",
        niche="Дропшиппинг на Shopify",
    )
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        # Check UI exposes {audience} variable chip
        html = client.get("/").text
        assert 'data-insert-var="{audience}"' in html

        camp_resp = client.post(
            "/api/campaigns",
            json={
                "name": "CRM Leads Audience Test",
                "account_ids": [acc_id],
                "daily_limit": 50,
                "steps": [
                    {"step_order": 1, "delay_hours": 0, "template_text": VLAD_FOLLOWUP_2},
                ],
            },
        )
        cid = camp_resp.json()["id"]

        enr = client.post(
            f"/api/campaigns/{cid}/from-leads",
            json={"lead_ids": [l_course["id"], l_saas["id"], l_ecom["id"]]},
        )
        assert enr.status_code == 200
        assert enr.json()["imported_count"] == 3

        run = client.post(f"/api/campaigns/{cid}/run", json={})
        assert run.status_code == 200
        assert run.json()["sent"] == 3

        inbox = {d["username"]: d["id"] for d in client.get("/api/inbox", params={"account_id": acc_id}).json()}
        assert "(which equals highly qualified students)" in client.get(f"/api/dialogs/{inbox['course_creator']}/messages").json()[-1]["text"]
        assert "(which equals highly qualified users)" in client.get(f"/api/dialogs/{inbox['saas_founder']}/messages").json()[-1]["text"]
        assert "(which equals highly qualified buyers)" in client.get(f"/api/dialogs/{inbox['shop_owner']}/messages").json()[-1]["text"]


def test_csv_column_mapping_and_mcp_ingest_preserve_explicit_audience_override(tmp_path):
    from app.mcp_server import call_mcp_tool_handler

    conn = connect(tmp_path / "crm_map_aud.db")
    init_db(conn)
    acc_id = _seed_connected_account(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        camp = client.post(
            "/api/campaigns",
            json={
                "name": "Mapped & MCP Audience",
                "account_ids": [acc_id],
                "daily_limit": 50,
                "steps": [{"step_order": 1, "delay_hours": 0, "template_text": VLAD_FOLLOWUP_2}],
            },
        ).json()
        cid = camp["id"]

        imp = client.post(
            f"/api/campaigns/{cid}/csv",
            json={
                "csv_text": "handle,brand,who_buys\n@mapped_lead,SaaS Tool,listeners\n",
                "column_mapping": {"username": "handle", "company": "brand", "audience": "who_buys"},
            },
        )
        assert imp.status_code == 200
        assert imp.json()["imported"] == 1

        mcp_res = call_mcp_tool_handler(
            conn,
            "ingest_discovered_leads",
            {
                "campaign_id": cid,
                "enroll_in_campaign": True,
                "leads": [
                    {
                        "username": "mcp_lead",
                        "company": "SaaS Cloud",
                        "category": "SaaS",
                        "audience": "players",
                    }
                ],
            },
        )
        assert mcp_res["enrolled_in_campaign"] == 1

        run = client.post(f"/api/campaigns/{cid}/run", json={})
        assert run.status_code == 200
        assert run.json()["sent"] == 2

        inbox = {d["username"]: d["id"] for d in client.get("/api/inbox", params={"account_id": acc_id}).json()}
        assert "(which equals highly qualified listeners)" in client.get(f"/api/dialogs/{inbox['mapped_lead']}/messages").json()[-1]["text"]
        assert "(which equals highly qualified players)" in client.get(f"/api/dialogs/{inbox['mcp_lead']}/messages").json()[-1]["text"]

